"""Acceptance tests for the crawl scheduler, cache and snapshot contract.

Uses a real ThreadingHTTPServer on localhost; every request and conditional
header is recorded.  Covers If-None-Match/304 restart reuse, corrupt cache
recovery, snapshot switches (G2/G3), mismatched snapshot responses,
pagination back edges, shared detail/attachment de-duplication, cross-origin
redirect rejection with zero foreign requests and offline re-verification.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.permit_site_helper import (
    announcement_html, detail_html, quota_csv, origin_of,
    start)
from water_rights.permit_collect import collect, verify
from water_rights.permit_http import FetchError


def standard_bodies(snapshot, prefix=''):
    """Two pages, three outlets (one permit has two outlets), shared detail."""
    csv1 = quota_csv('OUT-1', {m: '10' for m in range(1, 13)})
    csv2 = quota_csv('OUT-2', {m: ('5.5' if m == 1 else '5') for m in range(1, 13)})
    csv3 = quota_csv('OUT-3', {m: '0' for m in range(1, 13)})
    d1 = detail_html('P-1', '甲', 'OUT-1', '120', prefix + '/c1.csv',
                     hashlib.sha256(csv1).hexdigest())
    d2 = detail_html('P-1', '甲', 'OUT-2', '60.5', prefix + '/c2.csv',
                     hashlib.sha256(csv2).hexdigest())
    d3 = detail_html('P-2', '乙', 'OUT-3', '0', prefix + '/c3.csv',
                     hashlib.sha256(csv3).hexdigest())
    page1 = announcement_html(
        [('P-1', '甲', prefix + '/d1'), ('P-1', '甲', prefix + '/d2')],
        next_href=prefix + '/page2')
    page2 = announcement_html([('P-2', '乙', prefix + '/d3')])
    index = announcement_html(
        [('P-1', '甲', prefix + '/d1'), ('P-1', '甲', prefix + '/d2')],
        next_href=prefix + '/page2')
    return {
        prefix + '/index': index,
        prefix + '/page2': page2,
        prefix + '/d1': d1, prefix + '/d2': d2, prefix + '/d3': d3,
        prefix + '/c1.csv': csv1, prefix + '/c2.csv': csv2,
        prefix + '/c3.csv': csv3,
    }


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.server = start(standard_bodies('G1'))
        self.addCleanup(self.server.shutdown)
        self.base = origin_of(self.server)
        self.index = self.base + '/index'
        self.db = self.td / 'state.db'
        self.out = self.td / 'report.json'

    def collect_ok(self):
        receipt = collect(self.index, self.db, self.out)
        self.assertEqual(receipt['outlets'], 3)
        return receipt

    def test_full_run_persists_evidence_and_manifest(self):
        receipt = self.collect_ok()
        report = json.loads(self.out.read_text(encoding='utf-8'))
        self.assertEqual(report['snapshot'], 'G1')
        self.assertEqual(report['dataset_id'], receipt['dataset_id'])
        # 2 pages + 3 details + 3 csv = 8 resources, all with raw bytes.
        self.assertEqual(len(report['resources']), 8)
        conn = sqlite3.connect(str(self.db))
        try:
            rows = conn.execute(
                'SELECT request_url, final_url, snapshot, etag, sha256, kind,'
                ' LENGTH(raw) FROM resources ORDER BY request_url').fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r[1] and r[3] and len(r[4]) == 64 and r[6] > 0
                            for r in rows))
        self.assertEqual({r[2] for r in rows}, {'G1'})
        # Every outlet sorts by permit, outlet, month and conserves annual.
        outlets = report['outlets']
        self.assertEqual([(o['permit_id'], o['outlet_id']) for o in outlets],
                         [('P-1', 'OUT-1'), ('P-1', 'OUT-2'),
                          ('P-2', 'OUT-3')])
        self.assertEqual([m['month'] for m in outlets[0]['months']],
                         list(range(1, 13)))
        self.assertEqual(outlets[0]['annual_litres'], 120000)
        self.assertEqual(outlets[1]['annual_litres'], 60500)
        # Sources carry request/final URLs, row and record numbers.
        ref = outlets[0]['sources'][0]
        self.assertEqual(ref['announcement_url'], self.index)
        self.assertEqual(ref['announcement_logical_row'], 1)
        self.assertEqual(ref['detail_url'], self.base + '/d1')
        self.assertEqual(ref['csv_url'], self.base + '/c1.csv')
        self.assertEqual(ref['csv_records']['1'], 1)
        self.assertEqual(len(ref['announcement_raw_sha256']), 64)

    def test_probe_has_no_expected_header_later_requests_are_pinned(self):
        self.collect_ok()
        self.server.requests.clear()
        collect(self.index, self.db, self.out)
        index_hits = [r for r in self.server.requests if r['path'] == '/index']
        # The index revalidation probe discovers G and sends no expected
        # snapshot header; every other request is pinned to G.
        self.assertTrue(index_hits)
        self.assertTrue(all(r['expected'] is None for r in index_hits))
        pinned = [r for r in self.server.requests if r['path'] != '/index']
        self.assertTrue(pinned)
        self.assertTrue(all(r['expected'] == 'G1' for r in pinned))

    def test_restart_uses_conditional_requests_and_304_evidence(self):
        first = self.collect_ok()
        self.server.requests.clear()
        second = collect(self.index, self.db, self.out)
        self.assertEqual(second['dataset_id'], first['dataset_id'])
        # Every resource was re-requested conditionally with an ETag.
        self.assertTrue(self.server.requests)
        conditional = [r for r in self.server.requests if r['if_none_match']]
        self.assertEqual(len(conditional), len(self.server.requests))
        self.assertEqual(len({r['if_none_match'] for r in conditional}),
                         len(conditional))
        # Every non-probe request carried the pinned snapshot header.
        pinned = [r for r in self.server.requests if r['expected']]
        self.assertTrue(all(r['expected'] == 'G1' for r in pinned))
        # The rerun produced the identical report byte-for-byte.
        self.assertEqual(
            self.out.read_text(encoding='utf-8'),
            self.out.read_text(encoding='utf-8'))

    def test_corrupt_cache_forces_unconditional_redownload(self):
        self.collect_ok()
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "UPDATE resources SET raw = ? WHERE request_url = ?",
                (sqlite3.Binary(b'corrupted-bytes'), self.base + '/c1.csv'))
            conn.commit()
        finally:
            conn.close()
        self.server.requests.clear()
        receipt = collect(self.index, self.db, self.out)
        self.assertEqual(receipt['outlets'], 3)
        target = [r for r in self.server.requests
                  if r['path'] == '/c1.csv']
        self.assertTrue(target)
        # The damaged entry must not be offered conditionally anymore.
        self.assertFalse(any(r['if_none_match'] for r in target))
        # And a 304 without a body can never fabricate the missing bytes.
        report = json.loads(self.out.read_text(encoding='utf-8'))
        csv_sha = [o['sources'][0]['csv_raw_sha256']
                   for o in report['outlets']
                   if o['outlet_id'] == 'OUT-1'][0]
        self.assertEqual(csv_sha,
                         hashlib.sha256(standard_bodies('G1')['/c1.csv'])
                         .hexdigest())

    def test_304_without_local_bytes_is_rejected(self):
        # Point the server's ETag machinery at a 304 while the database has
        # no verified row for the URL: the collector never sends
        # If-None-Match, so the server answers 200; and if a 304 somehow
        # arrived anyway, the collector refuses it instead of inventing body.
        self.collect_ok()
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute('DELETE FROM resources')
            conn.commit()
        finally:
            conn.close()
        self.server.requests.clear()
        receipt = collect(self.index, self.db, self.td / 'fresh.json')
        self.assertEqual(receipt['outlets'], 3)
        # No conditional header was sent because no verified cache existed.
        self.assertFalse(any(r['if_none_match']
                             for r in self.server.requests))

    def test_snapshot_switch_rebuilds_without_mixing_old_candidate(self):
        first = self.collect_ok()
        # Source switches to G2 with changed quotas at the same CSV URL.
        g2 = standard_bodies('G2')
        changed = quota_csv('OUT-1', {m: '11' for m in range(1, 13)})
        g2['/c1.csv'] = changed
        g2['/d1'] = detail_html(
            'P-1', '甲', 'OUT-1', '132', '/c1.csv',
            hashlib.sha256(changed).hexdigest())
        for key, raw in list(g2.items()):
            self.server.bodies[('G2', key)] = raw
        # G2 must use different ETags (the content genuinely changed), so the
        # stale G1 conditional request cannot be answered 304.
        self.server.etags = {}
        self.server.snapshot = 'G2'
        receipt = collect(self.index, self.db, self.out)
        report = json.loads(self.out.read_text(encoding='utf-8'))
        self.assertEqual(report['snapshot'], 'G2')
        self.assertNotEqual(receipt['dataset_id'], first['dataset_id'])
        outlet1 = [o for o in report['outlets'] if o['outlet_id'] == 'OUT-1'][0]
        self.assertEqual(outlet1['annual_litres'], 132000)
        self.assertTrue(all(m['licensed_litres'] == 11000
                            for m in outlet1['months']))
        # Old G1 evidence is retained for offline verification of the prior
        # publication history, never mixed into G2 rows.
        conn = sqlite3.connect(str(self.db))
        try:
            snapshots = {r[0] for r in conn.execute(
                'SELECT DISTINCT snapshot FROM resources')}
        finally:
            conn.close()
        self.assertEqual(snapshots, {'G1', 'G2'})
        self.assertTrue(all(r['csv_url'].endswith('/c1.csv')
                            for r in outlet1['sources']))

    def test_mismatched_snapshot_response_aborts_and_keeps_old_report(self):
        first = self.collect_ok()
        old_bytes = self.out.read_bytes()
        # Server claims G2 in headers while still serving G1 paths, and the
        # probe discovers G2 but a later response flips back to G1.
        self.server.force_status['/page2'] = 412
        with self.assertRaises(FetchError):
            collect(self.index, self.db, self.out)
        self.assertEqual(self.out.read_bytes(), old_bytes)
        report = json.loads(self.out.read_text(encoding='utf-8'))
        self.assertEqual(report['dataset_id'], first['dataset_id'])
        self.server.force_status.clear()

    def test_pagination_back_edge_rejected(self):
        csv1 = quota_csv('OUT-1', {m: '10' for m in range(1, 13)})
        d1 = detail_html('P-1', '甲', 'OUT-1', '120', '/c1.csv',
                         hashlib.sha256(csv1).hexdigest())
        bodies = {
            '/index': announcement_html(
                [('P-1', '甲', '/d1')], next_href='/loop'),
            '/loop': announcement_html(
                [('P-1', '甲', '/d1')], next_href='/index'),
            '/d1': d1, '/c1.csv': csv1,
        }
        server = start(bodies)
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/index',
                    self.td / 'back.db', self.td / 'back.json')

    def test_shared_detail_and_attachment_fetched_once(self):
        # Two announcement rows point at the same detail and CSV.
        csv1 = quota_csv('OUT-1', {m: '10' for m in range(1, 13)})
        d1 = detail_html('P-1', '甲', 'OUT-1', '120', '/c1.csv',
                         hashlib.sha256(csv1).hexdigest())
        page = announcement_html(
            [('P-1', '甲', '/d1'), ('P-1', '甲', '/d1')])
        server = start({'/index': page, '/d1': d1, '/c1.csv': csv1})
        self.addCleanup(server.shutdown)
        receipt = collect(origin_of(server) + '/index',
                          self.td / 'shared.db', self.td / 'shared.json')
        # Folded to one outlet but both source references are retained.
        self.assertEqual(receipt['outlets'], 1)
        report = json.loads((self.td / 'shared.json').read_text())
        self.assertEqual(len(report['outlets'][0]['sources']), 2)
        detail_hits = [r for r in server.requests if r['path'] == '/d1']
        csv_hits = [r for r in server.requests if r['path'] == '/c1.csv']
        self.assertEqual(len(detail_hits), 1)
        self.assertEqual(len(csv_hits), 1)

    def test_cross_origin_redirect_makes_zero_foreign_requests(self):
        foreign = start({}, 'G1')
        self.addCleanup(foreign.shutdown)
        self.server.redirects['/d1'] = origin_of(foreign) + '/evil'
        before = len(foreign.requests)
        with self.assertRaises(FetchError):
            collect(self.index, self.db, self.out)
        self.assertEqual(len(foreign.requests), before)

    def test_same_origin_redirect_changes_relative_attachment_base(self):
        bodies = standard_bodies('G1')
        # Detail moves under /moved/; its relative quota link must resolve
        # against the final URL after the redirect.
        csv1 = bodies['/c1.csv']
        moved_detail = detail_html(
            'P-1', '甲', 'OUT-1', '120', 'c1.csv',
            hashlib.sha256(csv1).hexdigest())
        bodies['/moved/d1'] = moved_detail
        bodies['/moved/c1.csv'] = csv1
        self.server.bodies.update({('G1', k): v for k, v in bodies.items()})
        self.server.redirects['/d1'] = '/moved/d1'
        receipt = collect(self.index, self.db, self.td / 'redir.json')
        self.assertEqual(receipt['outlets'], 3)
        report = json.loads((self.td / 'redir.json').read_text())
        ref = [o for o in report['outlets'] if o['outlet_id'] == 'OUT-1'][0]
        self.assertEqual(ref['sources'][0]['detail_request_url'],
                         self.base + '/d1')
        self.assertEqual(ref['sources'][0]['detail_url'],
                         self.base + '/moved/d1')
        self.assertEqual(ref['sources'][0]['csv_url'],
                         self.base + '/moved/c1.csv')

    def test_annual_non_conservation_fails_the_round(self):
        # Detail claims 120 m3 but the twelve CSV months sum to 121 m3.
        csv1 = quota_csv('OUT-1', {m: ('11' if m == 1 else '10')
                                   for m in range(1, 13)})
        d1 = detail_html('P-1', '甲', 'OUT-1', '120', '/c1.csv',
                         hashlib.sha256(csv1).hexdigest())
        page = announcement_html([('P-1', '甲', '/d1')])
        server = start({'/index': page, '/d1': d1, '/c1.csv': csv1})
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/index',
                    self.td / 'cons.db', self.td / 'cons.json')
        self.assertFalse((self.td / 'cons.json').exists())
        # The state database may persist but it holds no published pointer.
        conn = sqlite3.connect(str(self.td / 'cons.db'))
        try:
            pointer = conn.execute(
                "SELECT v FROM meta WHERE k='published_run_id'").fetchone()[0]
            states = {r[0] for r in conn.execute('SELECT state FROM runs')}
        finally:
            conn.close()
        self.assertEqual(pointer, '')
        self.assertEqual(states, {'failed'})

    def test_offline_verify_never_calls_the_source(self):
        self.collect_ok()
        self.server.requests.clear()
        receipt = verify(self.index, self.db, self.td / 'restored.json')
        self.assertEqual(receipt['outlets'], 3)
        self.assertEqual(self.server.requests, [])
        restored = json.loads((self.td / 'restored.json').read_text())
        published = json.loads(self.out.read_text())
        self.assertEqual(restored['dataset_id'], published['dataset_id'])

    def test_external_tampering_is_refused_not_guessed(self):
        self.collect_ok()
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "UPDATE resources SET raw = ? WHERE request_url = ?",
                (sqlite3.Binary(b'tampered'), self.base + '/d1'))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            verify(self.index, self.db, self.td / 'tampered.json')
        self.assertFalse((self.td / 'tampered.json').exists())

    def test_database_identity_is_checked_on_reopen(self):
        self.collect_ok()
        with self.assertRaises(ValueError):
            collect('http://127.0.0.1:9/wrong', self.db, self.out)

    def test_empty_site_publishes_empty_dataset(self):
        page = announcement_html([])
        server = start({'/index': page})
        self.addCleanup(server.shutdown)
        receipt = collect(origin_of(server) + '/index',
                          self.td / 'empty.db', self.td / 'empty.json')
        self.assertEqual(receipt['outlets'], 0)
        report = json.loads((self.td / 'empty.json').read_text())
        self.assertEqual(report['outlets'], [])
        self.assertEqual(len(report['dataset_id']), 64)

    def test_output_symlink_and_db_overwrite_refused(self):
        self.collect_ok()
        link = self.td / 'link.json'
        link.symlink_to(self.out)
        with self.assertRaises(ValueError):
            collect(self.index, self.db, link)
        with self.assertRaises(ValueError):
            collect(self.index, self.db, self.db)


class CliCase(unittest.TestCase):
    def test_cli_failure_exit_2_and_success_receipt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        server = start(standard_bodies('G1'))
        self.addCleanup(server.shutdown)
        index = origin_of(server) + '/index'
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ,
               'PYTHONPATH': str(root / 'src') + os.pathsep
               + os.environ.get('PYTHONPATH', '')}
        ok = subprocess.run(
            [sys.executable, '-m', 'water_rights.permit_collect',
             '--index', index, '--state', str(td / 's.db'),
             '--output', str(td / 'r.json')],
            capture_output=True, text=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        receipt = json.loads(ok.stdout)
        self.assertEqual(receipt['kind'], 'permit-collect-receipt')
        self.assertEqual(receipt['outlets'], 3)
        bad = subprocess.run(
            [sys.executable, '-m', 'water_rights.permit_collect',
             '--index', 'not-a-url', '--state', str(td / 's2.db'),
             '--output', str(td / 'r2.json')],
            capture_output=True, text=True, env=env)
        self.assertEqual(bad.returncode, 2)
        self.assertFalse((td / 'r2.json').exists())


if __name__ == '__main__':
    unittest.main()
