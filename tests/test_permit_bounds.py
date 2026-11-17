"""Tests for the hard resource bounds declared by the source contract."""
import hashlib
import unittest

from tests.permit_site_helper import (
    announcement_html, detail_html, quota_csv, origin_of, start)
from water_rights.permit_collect import collect
from water_rights.permit_http import MAX_BODY_BYTES, FetchError, HttpFetcher


def csv_for(outlet):
    return quota_csv(outlet, {m: '1' for m in range(1, 13)})


class BoundsCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_document_over_2mb_rejected_at_http_layer(self):
        big = b'<html>' + b'a' * (MAX_BODY_BYTES + 10) + b'</html>'
        server = start({'/index': big})
        self.addCleanup(server.shutdown)
        fetcher = HttpFetcher(origin_of(server) + '/index')
        with self.assertRaises(FetchError):
            fetcher.get(fetcher.entry_url)

    def test_more_than_100_pages_rejected(self):
        # Chain 101 pages via a#next.
        bodies = {}
        for i in range(101):
            nxt = '/p%d' % (i + 1) if i < 100 else None
            bodies['/p%d' % i] = announcement_html([], next_href=nxt)
        server = start(bodies)
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/p0',
                    self.td / 'b.db', self.td / 'b.json')

    def test_more_than_10000_logical_rows_rejected(self):
        # One page with 10001 rows pointing at distinct details would exceed
        # detail limits first; instead build 6 pages x ~1700 rows sharing one
        # detail so the row cap, not the detail cap, is the binding limit.
        bodies = {}
        row_count = 0
        for page in range(6):
            rows = []
            for _ in range(1700):
                rows.append(('P', 'h', '/d1'))
                row_count += 1
            nxt = '/pg%d' % (page + 1) if page < 5 else None
            bodies['/pg%d' % page] = announcement_html(rows, next_href=nxt)
        csv1 = csv_for('OUT-1')
        bodies['/d1'] = detail_html(
            'P', 'h', 'OUT-1', '12', '/c1.csv',
            hashlib.sha256(csv1).hexdigest())
        bodies['/c1.csv'] = csv1
        server = start(bodies)
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/pg0',
                    self.td / 'rows.db', self.td / 'rows.json')
        self.assertGreater(row_count, 10000)

    def test_more_than_2000_details_rejected(self):
        rows = [('P%d' % i, 'h', '/d%d' % i) for i in range(2001)]
        csv1 = csv_for('OUT-1')
        bodies = {'/index': announcement_html(rows)}
        for i in range(2001):
            bodies['/d%d' % i] = detail_html(
                'P%d' % i, 'h', 'OUT-1', '12', '/c1.csv',
                hashlib.sha256(csv1).hexdigest())
        bodies['/c1.csv'] = csv1
        server = start(bodies)
        self.addCleanup(server.shutdown)
        with self.assertRaises(ValueError):
            collect(origin_of(server) + '/index',
                    self.td / 'd.db', self.td / 'd.json')


if __name__ == '__main__':
    unittest.main()
