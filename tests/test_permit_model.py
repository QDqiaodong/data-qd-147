"""Unit tests for candidate folding, report identity and ordering."""
import json
import unittest

from water_rights.permit_model import (build_report, canonical_json,
                                       dataset_identity, fold_outlets)


def entry(permit, outlet, holder='甲', litres=1000, ref=1):
    source = {
        'announcement_request_url': 'http://h/p%d' % ref,
        'announcement_url': 'http://h/p%d' % ref,
        'announcement_logical_row': ref,
        'announcement_raw_sha256': 'a' * 64,
        'detail_request_url': 'http://h/d%d' % ref,
        'detail_url': 'http://h/d%d' % ref,
        'detail_raw_sha256': 'b' * 64,
        'csv_request_url': 'http://h/c%d' % ref,
        'csv_url': 'http://h/c%d' % ref,
        'csv_raw_sha256': 'c' * 64,
        'csv_records': {str(m): m for m in range(1, 13)},
    }
    return {
        'permit_id': permit, 'outlet_id': outlet, 'holder': holder,
        'annual_litres': litres * 12,
        'months': [(m, litres, m) for m in range(1, 13)],
        'refs': [source],
    }


class FoldCase(unittest.TestCase):
    def test_identical_repeats_fold_keeping_every_reference(self):
        merged = fold_outlets([entry('P1', 'O1', ref=1),
                               entry('P1', 'O1', ref=2)])
        self.assertEqual(len(merged), 1)
        rows = [r['announcement_logical_row']
                for r in merged[('P1', 'O1')]['refs']]
        self.assertEqual(rows, [1, 2])

    def test_holder_conflict_rejects_whole_candidate(self):
        with self.assertRaises(ValueError):
            fold_outlets([entry('P1', 'O1', holder='甲'),
                          entry('P1', 'O1', holder='乙')])

    def test_quota_conflict_rejects_whole_candidate(self):
        with self.assertRaises(ValueError):
            fold_outlets([entry('P1', 'O1', litres=1000),
                          entry('P1', 'O1', litres=2000)])


class ReportCase(unittest.TestCase):
    def _manifest(self):
        return [{'request_url': 'http://h/a', 'final_url': 'http://h/a',
                 'snapshot': 'G1', 'etag': '"e"', 'kind': 'index',
                 'raw_sha256': 'a' * 64}]

    def test_sorting_and_deterministic_identity(self):
        entries = [entry('P2', 'O2', ref=2), entry('P1', 'O1', ref=1),
                   entry('P1', 'O0', ref=3)]
        report1, identity1 = build_report('G1', entries, self._manifest())
        report2, identity2 = build_report('G1', list(reversed(entries)),
                                          list(reversed(self._manifest())))
        self.assertEqual(identity1, identity2)
        self.assertEqual(report1, report2)
        keys = [(o['permit_id'], o['outlet_id']) for o in report1['outlets']]
        self.assertEqual(keys, [('P1', 'O0'), ('P1', 'O1'), ('P2', 'O2')])
        # Months are 1..12 in order with integer litres.
        self.assertEqual([m['month'] for m in report1['outlets'][0]['months']],
                         list(range(1, 13)))
        self.assertTrue(all(
            isinstance(m['licensed_litres'], int)
            for o in report1['outlets'] for m in o['months']))

    def test_identity_excludes_itself_and_uses_canonical_encoding(self):
        report, identity = build_report('G1', [entry('P1', 'O1')],
                                        self._manifest())
        core = {k: v for k, v in report.items() if k != 'dataset_id'}
        self.assertEqual(dataset_identity(core), identity)
        expected = hashlib_sha(canonical_json(core).encode('utf-8'))
        self.assertEqual(expected, identity)
        # The identity field itself must not feed back into the digest.
        self.assertNotIn('dataset_id', canonical_json(core))

    def test_empty_dataset_has_identity(self):
        report, identity = build_report('G1', [], [])
        self.assertEqual(report['outlets'], [])
        self.assertEqual(len(identity), 64)
        self.assertEqual(report['dataset_id'], identity)

    def test_report_has_no_runtime_or_random_fields(self):
        report, _ = build_report('G1', [entry('P1', 'O1')], self._manifest())
        forbidden = {'time', 'timestamp', 'random', 'nonce', 'run_time'}
        self.assertFalse(forbidden & set(report))
        # Re-encoding is stable byte-for-byte.
        once = canonical_json(report)
        twice = canonical_json(json.loads(once))
        self.assertEqual(once, twice)


def hashlib_sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


if __name__ == '__main__':
    unittest.main()
