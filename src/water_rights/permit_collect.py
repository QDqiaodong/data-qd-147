"""Permit announcement collector command line and crawl orchestration.

Usage::

    python -m water_rights.permit_collect --index URL --state DB --output REPORT
    python -m water_rights.permit_collect verify --index URL --state DB --output REPORT

The first form runs one bounded collection round against the source.  The
second form re-verifies the published report purely from the SQLite evidence
without any network access and restores the JSON report when needed.

Each round revalidates the index page first to obtain snapshot G.  Every
subsequent request carries ``X-Expected-Snapshot: G``; any mismatched
response, 409/412 or transport/contract failure aborts the round (exit 2)
and preserves the previously published report and all raw evidence.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import permit_store, permit_model
from .common import save, ident
from .permit_csv import parse_quota_csv, parse_decimal_litres
from .permit_html import parse_announcement, parse_detail
from .permit_http import (HttpFetcher, SnapshotConflict, Resource,
                          resolve, MAX_BODY_BYTES)

MAX_PAGES = 100
MAX_DETAILS = 2000
MAX_CSVS = 2000
MAX_ROWS = 10000
MAX_TOTAL_BYTES = 64 * 1024 * 1024

# Crash injection points for the three publication boundary tests.
_CRASH_AFTER_CANDIDATE = 'PERMIT_CRASH_AFTER_CANDIDATE'
_CRASH_AFTER_PUBLISH = 'PERMIT_CRASH_AFTER_PUBLISH'
_CRASH_AFTER_REPLACE = 'PERMIT_CRASH_AFTER_REPLACE'


class CollectionError(ValueError):
    pass


def _crash_hook(env_name):
    if os.environ.get(env_name):
        os._exit(3)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class Collector:
    """One bounded crawl round, all requests pinned to one snapshot G."""

    def __init__(self, fetcher, conn, snapshot):
        self.fetcher = fetcher
        self.conn = conn
        self.snapshot = snapshot
        self.pages = 0
        self.detail_count = 0
        self.csv_count = 0
        self.logical_rows = 0
        self.entries = []
        self.manifest = []
        self._seen_pages = set()
        self._seen_details = set()
        self._seen_csv = set()
        self._manifest_keys = set()
        self.round_bytes = 0

    def _check_budget(self, raw):
        if len(raw) > MAX_BODY_BYTES:
            raise CollectionError('document exceeds the 2 MB per-document limit')
        self.round_bytes += len(raw)
        if self.round_bytes > MAX_TOTAL_BYTES:
            raise CollectionError('total raw bytes this round exceed 64 MB')

    def _record_manifest(self, resource, kind):
        item = {
            'request_url': resource.request_url,
            'final_url': resource.final_url,
            'snapshot': resource.snapshot,
            'etag': resource.etag,
            'kind': kind,
            'raw_sha256': resource.sha256,
        }
        key = tuple(sorted(item.items()))
        if key not in self._manifest_keys:
            self._manifest_keys.add(key)
            self.manifest.append(item)

    def _fetch(self, request_url, kind):
        """Conditional fetch backed by durable SHA-verified raw evidence."""
        cached = permit_store.lookup_resource(
            self.conn, request_url, self.snapshot)
        conditional_etag = None
        if cached is not None and not cached.get('corrupt'):
            # A 304 is only offered when verified bytes exist for URL + G.
            conditional_etag = cached['etag']
        resource = self.fetcher.get(request_url, self.snapshot, conditional_etag)
        if resource.status == 304:
            if cached is None or cached.get('corrupt'):
                raise CollectionError(
                    '304 without verified local bytes for ' + request_url)
            if resource.snapshot != self.snapshot:
                raise CollectionError('304 snapshot does not match the round')
            resource = Resource(request_url, cached['final_url'], 304,
                                self.snapshot, cached['etag'],
                                cached['sha256'], kind, cached['raw'])
        else:
            if _sha256(resource.raw) != resource.sha256:
                raise CollectionError('internal digest mismatch')
            self._check_budget(resource.raw)
            permit_store.upsert_resource(self.conn, resource, kind)
        self._record_manifest(resource, kind)
        return resource

    def _reuse(self, url, kind):
        cached = permit_store.lookup_resource(self.conn, url, self.snapshot)
        if cached is None or cached.get('corrupt'):
            raise CollectionError('reused reference lost its evidence: ' + url)
        resource = Resource(url, cached['final_url'], 200, self.snapshot,
                            cached['etag'], cached['sha256'], kind, cached['raw'])
        self._record_manifest(resource, kind)
        return resource

    def _fetch_page(self, url, is_index):
        if url in self._seen_pages:
            raise CollectionError('pagination back edge rejected: ' + url)
        self._seen_pages.add(url)
        self.pages += 1
        if self.pages > MAX_PAGES:
            raise CollectionError('more than 100 announcement pages')
        resource = self._fetch(url, 'index' if is_index else 'page')
        try:
            next_href, rows = parse_announcement(resource.raw)
        except ValueError as error:
            raise CollectionError('announcement parse failure: ' + str(error)) from error
        self.logical_rows += len(rows)
        if self.logical_rows > MAX_ROWS:
            raise CollectionError('more than 10000 announcement logical rows')
        return resource, next_href, rows

    def _fetch_detail(self, detail_url):
        if detail_url in self._seen_details:
            return self._reuse(detail_url, 'detail')
        self._seen_details.add(detail_url)
        self.detail_count += 1
        if self.detail_count > MAX_DETAILS:
            raise CollectionError('more than 2000 detail pages')
        return self._fetch(detail_url, 'detail')

    def _fetch_csv(self, csv_url):
        if csv_url in self._seen_csv:
            return self._reuse(csv_url, 'csv')
        self._seen_csv.add(csv_url)
        self.csv_count += 1
        if self.csv_count > MAX_CSVS:
            raise CollectionError('more than 2000 quota CSV attachments')
        return self._fetch(csv_url, 'csv')

    def crawl(self, index_probe):
        # The mandatory index revalidation probe is also the first crawl page;
        # the index is never requested twice within a round.
        index_resource = index_probe
        self._seen_pages.add(index_resource.request_url)
        self.pages += 1
        if index_probe.status == 200:
            # A fresh index body counts toward this round's byte budget;
            # a 304 adds no network bytes because it reuses verified cache.
            self._check_budget(index_resource.raw)
        self._record_manifest(index_resource, 'index')
        try:
            next_href, rows = parse_announcement(index_resource.raw)
        except ValueError as error:
            raise CollectionError(
                'announcement parse failure: ' + str(error)) from error
        self.logical_rows += len(rows)
        if self.logical_rows > MAX_ROWS:
            raise CollectionError('more than 10000 announcement logical rows')
        pages = [(index_resource, rows)]
        while next_href is not None:
            next_url = resolve(pages[-1][0].final_url, next_href)
            page_resource, next_href, rows = self._fetch_page(next_url, False)
            pages.append((page_resource, rows))

        for page_resource, rows in pages:
            for row in rows:
                detail_url = resolve(page_resource.final_url, row['detail_href'])
                detail_resource = self._fetch_detail(detail_url)
                try:
                    detail = parse_detail(detail_resource.raw)
                except ValueError as error:
                    raise CollectionError(
                        'detail parse failure: ' + str(error)) from error
                if detail['permit_id'] != row['permit_id'] \
                        or detail['holder'] != row['holder']:
                    raise CollectionError(
                        'detail identity does not match announcement row %d'
                        % row['logical_row'])
                annual_litres = parse_decimal_litres(detail['annual_text'])
                csv_url = resolve(detail_resource.final_url, detail['quota_href'])
                csv_resource = self._fetch_csv(csv_url)
                if _sha256(csv_resource.raw) != detail['quota_sha256']:
                    raise CollectionError(
                        'attachment digest does not match data-sha256: ' + csv_url)
                try:
                    quota = parse_quota_csv(csv_resource.raw, detail['outlet_id'])
                except ValueError as error:
                    raise CollectionError(
                        'quota CSV failure: ' + str(error)) from error
                if quota['annual_litres'] != annual_litres:
                    raise CollectionError(
                        'monthly quotas do not conserve the annual amount: '
                        + detail['permit_id'] + '/' + detail['outlet_id'])
                self.entries.append({
                    'permit_id': detail['permit_id'],
                    'outlet_id': detail['outlet_id'],
                    'holder': detail['holder'],
                    'annual_litres': annual_litres,
                    'months': quota['months'],
                    'refs': [{
                        'announcement_request_url': page_resource.request_url,
                        'announcement_url': page_resource.final_url,
                        'announcement_logical_row': row['logical_row'],
                        'announcement_raw_sha256': page_resource.sha256,
                        'detail_request_url': detail_resource.request_url,
                        'detail_url': detail_resource.final_url,
                        'detail_raw_sha256': detail_resource.sha256,
                        'csv_request_url': csv_resource.request_url,
                        'csv_url': csv_resource.final_url,
                        'csv_raw_sha256': csv_resource.sha256,
                        'csv_records': {str(m): r for m, _l, r in quota['months']},
                    }],
                })
        return self.entries, self.manifest


def _reconcile_published(conn, output_path, full_verify=False):
    """Make the JSON report match the durable published pointer.

    The database pointer is the single source of truth.  A lightweight
    identity/metadata check on every start keeps the report file and the
    committed state from drifting; ``full_verify`` additionally re-checks
    every raw byte (used by the offline ``verify`` command).  Corrupt or
    missing cache rows do not block a new network round, because that round
    is allowed to re-download unconditionally.
    """
    published = permit_store.published_state(conn)
    if published is None:
        return None
    report = json.loads(published['report_json'])
    if report['dataset_id'] != published['dataset_id']:
        raise permit_store.StoreError('published dataset identity is inconsistent')
    if full_verify:
        permit_model.verify_report_evidence(conn, report)
    output_path = Path(output_path)
    needs_write = True
    if output_path.exists() and not output_path.is_symlink():
        try:
            on_disk = json.loads(output_path.read_text(encoding='utf-8'))
            needs_write = on_disk.get('dataset_id') != published['dataset_id']
        except (ValueError, OSError):
            needs_write = True
    if needs_write:
        save(output_path, report)
    return report


def _index_probe(fetcher, conn):
    """Revalidate the index to discover the current snapshot G.

    The probe intentionally sends no X-Expected-Snapshot header: its job is
    to learn whatever G the source currently serves.  A cached index is still
    offered conditionally via If-None-Match, so an unchanged index yields 304.
    A changed snapshot always comes back as a fresh 200 for the new G.
    """
    cached_snapshot = conn_cached_snapshot(conn, fetcher.entry_url)
    cached = permit_store.lookup_resource(
        conn, fetcher.entry_url, cached_snapshot) if cached_snapshot else None
    etag = None
    if cached is not None and not cached.get('corrupt'):
        etag = cached['etag']
    resource = fetcher.get(fetcher.entry_url, None, etag)
    if resource.status == 304:
        if cached is None or cached.get('corrupt'):
            raise CollectionError('index 304 without verified local bytes')
        # A 304 must carry the same G as the cached bytes; the source
        # contract guarantees resources are immutable within a snapshot.
        if resource.snapshot != cached_snapshot:
            raise SnapshotConflict(
                'index 304 answered for a different snapshot than the cache')
        resource = Resource(resource.request_url, cached['final_url'], 304,
                            resource.snapshot, cached['etag'],
                            cached['sha256'], 'index', cached['raw'])
    else:
        permit_store.upsert_resource(conn, resource, 'index')
    return resource.snapshot, resource


def conn_cached_snapshot(conn, entry_url):
    rows = conn.execute(
        'SELECT snapshot FROM resources WHERE request_url = ?',
        (entry_url,)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def collect(index_url, db_path, output_path):
    ident(index_url)
    output_path = Path(output_path)
    permit_store.ensure_not_symlink(output_path)
    permit_store._ensure_output_distinct(db_path, output_path)
    conn = permit_store.init_or_open(db_path, index_url)
    report = dataset_id = snapshot = None
    try:
        # Boundary 2/3 recovery: the durable pointer always names the truth;
        # any stale or missing JSON file is rewritten before a new round.
        _reconcile_published(conn, output_path)
        fetcher = HttpFetcher(index_url)
        snapshot, probe = _index_probe(fetcher, conn)
        run_id = permit_store.begin_round(conn, snapshot)
        try:
            collector = Collector(fetcher, conn, snapshot)
            entries, manifest = collector.crawl(probe)
            report, dataset_id = permit_model.build_report(
                snapshot, entries, manifest)
            merged = permit_model.fold_outlets(entries)
            permit_store.save_candidate(
                conn, run_id, [merged[key] for key in sorted(merged)])
            # Boundary 1: candidate rows are durable; the pointer has not moved.
            conn.execute('COMMIT')
            _crash_hook(_CRASH_AFTER_CANDIDATE)
            conn.execute('BEGIN IMMEDIATE')
            permit_store.publish(conn, run_id, snapshot, report, dataset_id)
            conn.execute('COMMIT')
            # Boundary 2: database says published; report file not replaced.
            _crash_hook(_CRASH_AFTER_PUBLISH)
            save(output_path, report)
            # Boundary 3: report replaced; receipt not yet emitted.
            _crash_hook(_CRASH_AFTER_REPLACE)
        except BaseException:
            # Only an still-open, unpublished candidate may be rolled back;
            # a run that already committed its publication is never touched.
            if conn.in_transaction:
                state = conn.execute(
                    'SELECT state FROM runs WHERE run_id = ?',
                    (run_id,)).fetchone()[0]
                if state == 'candidate':
                    permit_store.fail_round(conn, run_id)
                else:
                    conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()
    return {
        'schema_version': permit_model.SCHEMA_VERSION,
        'kind': 'permit-collect-receipt',
        'dataset_id': dataset_id,
        'snapshot': snapshot,
        'outlets': len(report['outlets']),
        'report': str(output_path),
    }


def verify(index_url, db_path, output_path):
    ident(index_url)
    output_path = Path(output_path)
    permit_store.ensure_not_symlink(output_path)
    permit_store._ensure_output_distinct(db_path, output_path)
    conn = permit_store.init_or_open(db_path, index_url)
    try:
        report = _reconcile_published(conn, output_path, full_verify=True)
    finally:
        conn.close()
    if report is None:
        raise CollectionError('no published dataset exists in this database')
    return {
        'schema_version': permit_model.SCHEMA_VERSION,
        'kind': 'permit-verify-receipt',
        'dataset_id': report['dataset_id'],
        'snapshot': report['snapshot'],
        'outlets': len(report['outlets']),
        'report': str(output_path),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Collect water-use permit announcements into an offline dataset')
    parser.add_argument('--index', required=True,
                        help='complete http/https index URL of the announcements')
    parser.add_argument('--state', required=True,
                        help='SQLite collector state database path')
    parser.add_argument('--output', required=True,
                        help='JSON report output path (never the database)')
    parser.add_argument('command', nargs='?', default='collect',
                        choices=('collect', 'verify'),
                        help='collect (default) or offline verify')
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            receipt = verify(args.index, args.state, args.output)
        else:
            receipt = collect(args.index, args.state, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
