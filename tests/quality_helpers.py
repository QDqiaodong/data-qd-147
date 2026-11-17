"""Shared helpers for the quality warehouse acceptance tests."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400


def cli(*args, cwd=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'src')
    return subprocess.run([sys.executable, '-m', 'water_rights.quality_cli', *args],
                          capture_output=True, text=True, env=env, cwd=cwd)


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj))
    return Path(path)


def write_jsonl(path, events):
    Path(path).write_text(''.join(json.dumps(event, separators=(',', ':')) + '\n'
                                  for event in events))
    return Path(path)


def observation(seq, entity, revision, station, analyte, at, value,
                deleted=False):
    return {
        'seq': seq, 'kind': 'observation', 'id': entity, 'revision': revision,
        'deleted': deleted,
        'payload': None if deleted else {
            'station': station, 'analyte': analyte, 'at': at, 'value': value},
    }


def calibration(seq, entity, revision, station, analyte, start, end, a, b,
                deleted=False):
    return {
        'seq': seq, 'kind': 'calibration', 'id': entity, 'revision': revision,
        'deleted': deleted,
        'payload': None if deleted else {
            'station': station, 'analyte': analyte, 'start': start,
            'end': end, 'a': a, 'b': b},
    }


class Workspace:
    """Temp directory with init/ingest/report helpers around the real CLI."""

    def __init__(self, case):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def path(self, name):
        return self.dir / name

    def init(self, config, db='q.db'):
        source = write_json(self.path('config.json'), config)
        return cli('init', '--source', str(source), '--db', str(self.path(db)))

    def ingest(self, events, db='q.db', name='changes.jsonl'):
        source = write_jsonl(self.path(name), events)
        return cli('ingest', '--source', str(source), '--db', str(self.path(db)))

    def report(self, db='q.db', start=None, end=None, output='report.json'):
        return cli('report', '--db', str(self.path(db)), '--start', str(start),
                   '--end', str(end), '--output', str(self.path(output)))

    def read(self, name='report.json'):
        return json.loads(self.path(name).read_text())

    def db_bytes(self, db='q.db'):
        return self.path(db).read_bytes()

    def snapshot(self, db='q.db'):
        """Breakpoint, stored facts and derived rows, read straight from SQLite."""
        import sqlite3
        conn = sqlite3.connect(str(self.path(db)))
        try:
            meta = dict(conn.execute('SELECT k, v FROM meta'))
            events = conn.execute(
                'SELECT seq, kind, entity, revision, deleted, raw_json'
                ' FROM events ORDER BY seq').fetchall()
            derived = {
                name: conn.execute('SELECT * FROM %s ORDER BY 1, 2, 3' % name).fetchall()
                for name in ('derived_known', 'derived_unsafe',
                             'derived_excess', 'derived_alert')
            }
        finally:
            conn.close()
        return meta, events, derived


def ok(test, run):
    test.assertEqual(run.returncode, 0, run.stderr)
    return json.loads(run.stdout)
