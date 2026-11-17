"""Shared helpers for the terrain drainage base-map acceptance tests."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(source, output):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'src')
    return subprocess.run(
        [sys.executable, '-m', 'water_rights.terrain_cli',
         '--source', str(source), '--output', str(output)],
        capture_output=True, text=True, env=env)


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj))
    return Path(path)


def document(elevation, rainfall, area=1, outlets=None):
    if outlets is None:
        outlets = [{'id': 'o', 'row': 0, 'col': 0}]
    return {
        'elevation_mm': elevation,
        'rainfall_mm': rainfall,
        'cell_area_m2': area,
        'outlets': outlets,
    }


def rain_like(elevation, value=1):
    return [[None if v is None else value for v in row] for row in elevation]


class Workspace:
    """Temporary directory wrapping the real terrain CLI."""

    def __init__(self, case):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def path(self, name):
        return self.dir / name

    def run(self, doc, name='terrain.json', output='report.json', raw=None):
        source = self.path(name)
        if raw is None:
            write_json(source, doc)
        else:
            source.write_text(raw)
        result = cli(source, self.path(output))
        return result, source, self.path(output)

    def read(self, name='report.json'):
        return json.loads(self.path(name).read_text())
