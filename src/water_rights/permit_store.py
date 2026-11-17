"""Persistent SQLite state for the permit collector.

One database file is one collector application identity, bound to its entry
index URL.  The schema stores raw response evidence for every fetched
resource (never just parsed fields) plus the run state machine:

  candidate  -> published -> superseded

A new round deletes unfinished candidates but never deletes resources, so
the evidence behind every published report survives offline.  Publication is
a single transaction flipping the durable pointer; the JSON report is only
replaced afterwards via common.save, so reconciliation on restart can always
restore the exact published report from the database.
"""
import hashlib
import os
import sqlite3
from pathlib import Path

SCHEMA = 'permit-collector-v1'
_SIDECARS = ('-journal', '-wal', '-shm')


class StoreError(ValueError):
    pass


class CorruptCache(StoreError):
    """Cached bytes no longer match their recorded SHA256."""


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn


def _ensure_output_distinct(db_path, output_path):
    db_resolved = Path(db_path).resolve()
    out_resolved = Path(output_path).resolve()
    if out_resolved == db_resolved:
        raise StoreError('output must not overwrite the SQLite database')
    for suffix in _SIDECARS:
        if out_resolved == Path(str(db_path) + suffix).resolve():
            raise StoreError('output must not overwrite a SQLite sidecar file')


def ensure_not_symlink(path):
    if os.path.islink(path):
        raise StoreError('output cannot be a symlink: ' + str(path))


def init_or_open(db_path, index_url):
    """Create the database on first run or verify its identity afterwards."""
    ensure_not_symlink(db_path)
    dest = Path(db_path)
    if not os.path.lexists(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
        created = True
    else:
        created = False
    try:
        conn = _connect(dest)
    except sqlite3.Error as error:
        raise StoreError('cannot open collector database: ' + str(error)) from error
    try:
        if created:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
            conn.execute(
                'CREATE TABLE resources ('
                ' request_url TEXT NOT NULL, snapshot TEXT NOT NULL,'
                ' final_url TEXT NOT NULL, etag TEXT NOT NULL,'
                ' sha256 TEXT NOT NULL, kind TEXT NOT NULL,'
                ' raw BLOB NOT NULL, PRIMARY KEY (request_url, snapshot))')
            conn.execute(
                'CREATE TABLE runs ('
                ' run_id INTEGER PRIMARY KEY,'
                ' state TEXT NOT NULL, snapshot TEXT NOT NULL,'
                ' report_json TEXT, dataset_id TEXT)')
            conn.execute(
                'CREATE TABLE run_outlets ('
                ' run_id INTEGER NOT NULL, permit_id TEXT NOT NULL,'
                ' outlet_id TEXT NOT NULL, holder TEXT NOT NULL,'
                ' annual_litres INTEGER NOT NULL, months_json TEXT NOT NULL,'
                ' refs_json TEXT NOT NULL)')
            for key, value in (('schema', SCHEMA), ('index_url', index_url),
                               ('published_run_id', '')):
                conn.execute('INSERT INTO meta (k, v) VALUES (?, ?)', (key, value))
            conn.execute('COMMIT')
        else:
            try:
                row = conn.execute(
                    "SELECT v FROM meta WHERE k = 'schema'").fetchone()
            except sqlite3.Error as error:
                raise StoreError('not a permit collector database') from error
            if not row or row[0] != SCHEMA:
                raise StoreError('not a permit collector database')
            bound = conn.execute(
                "SELECT v FROM meta WHERE k = 'index_url'").fetchone()
            if not bound or bound[0] != index_url:
                raise StoreError(
                    'database is bound to a different index URL; refusing to mix states')
    except Exception:
        conn.close()
        if created:
            try:
                os.unlink(dest)
            except OSError:
                pass
        raise
    return conn


def published_state(conn):
    row = conn.execute(
        "SELECT v FROM meta WHERE k = 'published_run_id'").fetchone()
    if not row or not row[0]:
        return None
    run_id = int(row[0])
    record = conn.execute(
        'SELECT snapshot, dataset_id, report_json FROM runs WHERE run_id = ?',
        (run_id,)).fetchone()
    if record is None:
        raise StoreError('published run pointer is dangling')
    return {'run_id': run_id, 'snapshot': record[0],
            'dataset_id': record[1], 'report_json': record[2]}


def begin_round(conn, snapshot):
    """Discard every unfinished candidate; evidence and publications stay."""
    conn.execute('BEGIN IMMEDIATE')
    stale = [r[0] for r in conn.execute(
        "SELECT run_id FROM runs WHERE state IN ('candidate', 'failed')")]
    if stale:
        conn.execute(
            'DELETE FROM run_outlets WHERE run_id IN (%s)'
            % ','.join('?' for _ in stale), stale)
        conn.execute(
            'DELETE FROM runs WHERE run_id IN (%s)'
            % ','.join('?' for _ in stale), stale)
    cur = conn.execute('SELECT COALESCE(MAX(run_id), 0) + 1 FROM runs')
    run_id = cur.fetchone()[0]
    conn.execute(
        'INSERT INTO runs (run_id, state, snapshot, report_json, dataset_id)'
        " VALUES (?, 'candidate', ?, NULL, NULL)", (run_id, snapshot))
    return run_id


def fail_round(conn, run_id):
    """Mark an open candidate failed; the caller owns the transaction."""
    conn.execute("UPDATE runs SET state = 'failed' WHERE run_id = ?", (run_id,))
    conn.execute('COMMIT')


def lookup_resource(conn, request_url, snapshot):
    row = conn.execute(
        'SELECT final_url, etag, sha256, kind, raw FROM resources'
        ' WHERE request_url = ? AND snapshot = ?', (request_url, snapshot)).fetchone()
    if row is None:
        return None
    final_url, etag, sha256, kind, raw = row
    raw = bytes(raw)
    if hashlib.sha256(raw).hexdigest() != sha256:
        conn.execute('DELETE FROM resources WHERE request_url = ? AND snapshot = ?',
                     (request_url, snapshot))
        return {'corrupt': True, 'etag': etag}
    return {'final_url': final_url, 'etag': etag, 'sha256': sha256,
            'kind': kind, 'raw': raw}


def upsert_resource(conn, resource, kind):
    """Store a fresh 200 response with its raw bytes and contract metadata."""
    conn.execute(
        'INSERT INTO resources (request_url, snapshot, final_url, etag,'
        ' sha256, kind, raw) VALUES (?, ?, ?, ?, ?, ?, ?)'
        ' ON CONFLICT(request_url, snapshot) DO UPDATE SET'
        ' final_url = excluded.final_url, etag = excluded.etag,'
        ' sha256 = excluded.sha256, kind = excluded.kind, raw = excluded.raw',
        (resource.request_url, resource.snapshot, resource.final_url,
         resource.etag, resource.sha256, kind,
         sqlite3.Binary(resource.raw)))


def save_candidate(conn, run_id, outlets):
    """Persist all verified outlets in the open round transaction."""
    import json
    for outlet in outlets:
        conn.execute(
            'INSERT INTO run_outlets (run_id, permit_id, outlet_id, holder,'
            ' annual_litres, months_json, refs_json) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (run_id, outlet['permit_id'], outlet['outlet_id'],
             outlet['holder'], outlet['annual_litres'],
             json.dumps(outlet['months'], sort_keys=True, separators=(',', ':')),
             json.dumps(outlet['refs'], sort_keys=True, separators=(',', ':'))))


def publish(conn, run_id, snapshot, report, dataset_id):
    """Atomically move the durable published pointer to the new run."""
    import json
    canonical = json.dumps(report, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False)
    previous = published_state(conn)
    if previous is not None:
        conn.execute(
            "UPDATE runs SET state = 'superseded' WHERE run_id = ?",
            (previous['run_id'],))
    conn.execute(
        "UPDATE runs SET state = 'published', report_json = ?, dataset_id = ?"
        ' WHERE run_id = ?', (canonical, dataset_id, run_id))
    conn.execute(
        "UPDATE meta SET v = ? WHERE k = 'published_run_id'", (str(run_id),))


def evidence_for(conn, request_url, snapshot):
    return lookup_resource(conn, request_url, snapshot)
