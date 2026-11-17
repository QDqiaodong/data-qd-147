"""End-to-end linkage tests: snapshot transitions, crash boundaries, recovery.

Covers the required scenario where G1 is published, a G2 round is killed
after a detail is fetched but before its attachment, the source then moves
to G3 serving different quotas at the same attachment URL, and a restart
must produce a pure G3 dataset with no G2 residue.  Also covers the three
publication boundary process exits (candidate committed, pointer moved,
report replaced) and deterministic recovery.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.permit_site_helper import (
    announcement_html, detail_html, quota_csv, origin_of, start)
from water_rights.permit_collect import collect, verify

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ,
       'PYTHONPATH': str(ROOT / 'src') + os.pathsep
       + os.environ.get('PYTHONPATH', '')}


def site_bodies(snapshot, outlet_monthly, annual_text):
    csv1 = quota_csv('OUT-1', outlet_monthly)
    d1 = detail_html('P-1', '甲', 'OUT-1', annual_text, '/c1.csv',
                     __import__('hashlib').sha256(csv1).hexdigest())
    page = announcement_html([('P-1', '甲', '/d1')])
    return {'/index': page, '/d1': d1, '/c1.csv': csv1}


def months(value):
    return {m: value for m in range(1, 13)}


class LinkageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.server = start(site_bodies('G1', months('10'), '120'))
        self.addCleanup(self.server.shutdown)
        self.base = origin_of(self.server)
        self.index = self.base + '/index'
        self.db = self.td / 'state.db'
        self.out = self.td / 'report.json'

    def _switch(self, snapshot, monthly, annual):
        for path, raw in site_bodies(snapshot, monthly, annual).items():
            self.server.bodies[(snapshot, path)] = raw
        self.server.snapshot = snapshot
        self.server.etags = {}

    def test_g2_interrupted_then_g3_restart_is_pure_g3(self):
        # G1 is fully published.
        g1 = collect(self.index, self.db, self.out)
        g1_report = json.loads(self.out.read_text(encoding='utf-8'))
        self.assertEqual(g1_report['outlets'][0]['annual_litres'], 120000)

        # Source moves to G2; a G2 round starts but is terminated after the
        # detail is fetched and before the attachment.  We model the kill by
        # deleting only the G2 attachment row and marking the run failed,
        # which is exactly the durable state an interrupted process leaves.
        self._switch('G2', months('20'), '240')
        import sqlite3
        conn = sqlite3.connect(str(self.db))
        try:
            from water_rights import permit_store
            run_id = permit_store.begin_round(conn, 'G2')
            # Simulate having fetched the index and detail only.
            for path in ('/index', '/d1'):
                raw = self.server.bodies[('G2', path)]
                import hashlib
                from water_rights.permit_http import Resource
                res = Resource(self.base + path, self.base + path, 200, 'G2',
                               '"g2-%s"' % path,
                               hashlib.sha256(raw).hexdigest(), 'detail', raw)
                permit_store.upsert_resource(conn, res, 'page')
            permit_store.fail_round(conn, run_id)
        finally:
            conn.close()
        # The G1 report and its identity remain intact despite the G2 debris.
        self.assertEqual(json.loads(self.out.read_text())['dataset_id'],
                         g1['dataset_id'])

        # Source moves again to G3 with different quotas at the same URL.
        self._switch('G3', months('30'), '360')
        g3 = collect(self.index, self.db, self.out)
        report = json.loads(self.out.read_text(encoding='utf-8'))
        self.assertEqual(report['snapshot'], 'G3')
        self.assertEqual(report['outlets'][0]['annual_litres'], 360000)
        self.assertTrue(all(m['licensed_litres'] == 30000
                            for m in report['outlets'][0]['months']))
        # Every source reference points at G3 resources only.
        for ref in report['outlets'][0]['sources']:
            self.assertEqual(ref['csv_raw_sha256'],
                             __import__('hashlib').sha256(
                                 self.server.bodies[('G3', '/c1.csv')])
                             .hexdigest())
        self.assertNotEqual(g3['dataset_id'], g1['dataset_id'])
        # G1 raw evidence is still retained for offline review.
        conn = sqlite3.connect(str(self.db))
        try:
            kept = {r[0] for r in conn.execute(
                'SELECT DISTINCT snapshot FROM resources')}
        finally:
            conn.close()
        self.assertEqual(kept, {'G1', 'G2', 'G3'})
        # Offline re-verification needs no source and restores the G3 report.
        self.server.requests.clear()
        receipt = verify(self.index, self.db, self.td / 'again.json')
        self.assertEqual(receipt['dataset_id'], g3['dataset_id'])
        self.assertEqual(self.server.requests, [])

    def _cli(self, *extra, env=None):
        return subprocess.run(
            [sys.executable, '-m', 'water_rights.permit_collect',
             '--index', self.index, '--state', str(self.db),
             '--output', str(self.out), *extra],
            capture_output=True, text=True, env=env or ENV)

    def test_three_publication_boundaries_recover_deterministically(self):
        # Boundary 1: candidate durable, pointer not moved.
        r = self._cli(env={**ENV, 'PERMIT_CRASH_AFTER_CANDIDATE': '1'})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertFalse(self.out.exists())  # nothing ever published
        import sqlite3
        conn = sqlite3.connect(str(self.db))
        try:
            states = {row[0] for row in conn.execute(
                'SELECT state FROM runs')}
            published = conn.execute(
                "SELECT v FROM meta WHERE k='published_run_id'").fetchone()[0]
        finally:
            conn.close()
        self.assertIn('candidate', states)
        self.assertEqual(published, '')
        # Restart discards the unfinished candidate and completes G1.
        first = self._cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = json.loads(first.stdout)['dataset_id']
        self.assertTrue(self.out.exists())

        # Boundary 2: pointer moved in the DB, JSON not yet replaced.
        # Tamper the report first so we can prove reconciliation restores it.
        self.out.write_text('{"stale": true}', encoding='utf-8')
        r = self._cli(env={**ENV, 'PERMIT_CRASH_AFTER_PUBLISH': '1'})
        self.assertEqual(r.returncode, 3, r.stderr)
        # On restart the durable pointer restores the exact report before
        # the new round even begins.
        recovered = self._cli()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(self.out.read_text())['dataset_id'],
                         identity)
        self.assertEqual(json.loads(recovered.stdout)['dataset_id'], identity)

        # Boundary 3: report replaced, receipt not emitted.
        r = self._cli(env={**ENV, 'PERMIT_CRASH_AFTER_REPLACE': '1'})
        self.assertEqual(r.returncode, 3, r.stderr)
        # The report file already holds the new publication; a restart
        # recognises the identity and returns the same deterministic report.
        final = self._cli()
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(json.loads(final.stdout)['dataset_id'], identity)
        report = json.loads(self.out.read_text())
        self.assertEqual(report['dataset_id'], identity)
        self.assertEqual(report['outlets'][0]['annual_litres'], 120000)

    def test_legal_304_restart_and_redirect_base_then_identity_stable(self):
        first = self._cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = json.loads(first.stdout)['dataset_id']
        # A normal restart revalidates everything with conditional requests.
        self.server.requests.clear()
        second = self._cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['dataset_id'], identity)
        self.assertTrue(all(r['if_none_match'] for r in self.server.requests))

        # A same-origin detail redirect moves the relative attachment base.
        csv1 = self.server.bodies[('G1', '/c1.csv')]
        import hashlib
        moved = detail_html('P-1', '甲', 'OUT-1', '120', 'c1.csv',
                            hashlib.sha256(csv1).hexdigest())
        self.server.bodies[('G1', '/moved/d1')] = moved
        self.server.bodies[('G1', '/moved/c1.csv')] = csv1
        self.server.redirects['/d1'] = '/moved/d1'
        third = self._cli()
        self.assertEqual(third.returncode, 0, third.stderr)
        report = json.loads(self.out.read_text())
        ref = report['outlets'][0]['sources'][0]
        self.assertEqual(ref['detail_url'], self.base + '/moved/d1')
        self.assertEqual(ref['csv_url'], self.base + '/moved/c1.csv')
        # Final URLs are part of the source manifest and hence the identity,
        # so a redirect legitimately yields a new identity; the verified
        # quotas themselves stay identical.
        self.assertNotEqual(report['dataset_id'], identity)
        self.assertEqual(report['outlets'][0]['annual_litres'], 120000)
        self.assertTrue(all(m['licensed_litres'] == 10000
                            for m in report['outlets'][0]['months']))
        # And another run against the same redirected source is deterministic.
        fourth = self._cli()
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
        self.assertEqual(json.loads(fourth.stdout)['dataset_id'],
                         report['dataset_id'])

    def test_conflicting_duplicate_outlet_fails_whole_candidate(self):
        bad_csv = quota_csv('OUT-1', months('11'))
        bad_detail = detail_html(
            'P-1', '甲', 'OUT-1', '132', '/c1.csv',
            __import__('hashlib').sha256(bad_csv).hexdigest())
        # Two announcement rows for the same key with mismatched quota data.
        page = announcement_html([('P-1', '甲', '/d1'), ('P-1', '甲', '/d2')])
        d2 = detail_html('P-1', '甲', 'OUT-1', '132', '/c2.csv',
                         __import__('hashlib').sha256(bad_csv).hexdigest())
        server = start({'/index': page,
                        '/d1': self.server.bodies[('G1', '/d1')],
                        '/d2': bad_detail,
                        '/c1.csv': self.server.bodies[('G1', '/c1.csv')],
                        '/c2.csv': bad_csv})
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/index',
                    self.td / 'conflict.db', self.td / 'conflict.json')
        self.assertFalse((self.td / 'conflict.json').exists())


if __name__ == '__main__':
    unittest.main()
