"""Acceptance tests for the terrain drainage base-map product.

The production heap algorithm is never used to generate expectations:

* every filled elevation H is checked against ``terrain_reference``, an
  independent per-threshold flood fill from the explicit outlets;
* plateau distances, receiver tie rules, acyclicity, outlet partitions and
  every volume are re-derived from the published receiver edges in
  ``terrain_reference.verify_report``;
* two hand-drawn grids (including a saddle change that moves the spill level,
  routing and partition together) carry fully manual expectations.
"""
import hashlib
import json
import random
import sys
import time
import unittest

from terrain_helpers import Workspace, document, rain_like, write_json
from terrain_reference import reference_filled, verify_report

from water_rights.terrain_analysis import analyze, build_report
from water_rights.terrain_cli import run as run_pipeline
from water_rights.terrain_model import TerrainError, parse_input


def run_api(doc):
    parsed = parse_input(doc)
    return build_report(parsed, '0' * 64, analyze(parsed))


# ---------------------------------------------------------------------------
# Hand-drawn grids with fully manual expectations
# ---------------------------------------------------------------------------
class HandDrawn(unittest.TestCase):
    def test_single_cell_outlet(self):
        doc = document([[7]], [[3]], area=5)
        report = run_api(doc)
        cell = report['cells'][0][0]
        self.assertEqual(cell, {
            'filled_mm': 7, 'fill_mm': 0, 'flat_distance': 0,
            'receiver': None, 'outlet_id': 'o',
            'upstream_cells': 1, 'runoff_l': 15})
        self.assertEqual(report['outlets'], [{
            'id': 'o', 'row': 0, 'col': 0, 'cells': 1,
            'area_m2': 5, 'runoff_l': 15, 'fill_l': 0}])
        self.assertEqual(report['totals'],
                         {'cells': 1, 'rainfall_l': 15, 'fill_l': 0})

    def test_nested_depressions_manual(self):
        # 1x5: outlet at the left; two nested basins behind walls 8 and 9.
        elev = [[2, 8, 3, 9, 4]]
        doc = document(elev, rain_like(elev, 1), area=2)
        report = run_api(doc)
        row = report['cells'][0]
        self.assertEqual([c['filled_mm'] for c in row], [2, 8, 8, 9, 9])
        self.assertEqual([c['fill_mm'] for c in row], [0, 0, 5, 0, 5])
        self.assertEqual([c['flat_distance'] for c in row], [0, 0, 1, 0, 1])
        self.assertEqual([c['receiver'] for c in row],
                         [None, [0, 0], [0, 1], [0, 2], [0, 3]])
        # Volumes: rainfall 1 mm * 2 m2 = 2 L per cell.
        self.assertEqual([c['runoff_l'] for c in row], [10, 8, 6, 4, 2])
        self.assertEqual([c['upstream_cells'] for c in row], [5, 4, 3, 2, 1])
        outlet = report['outlets'][0]
        self.assertEqual(outlet['cells'], 5)
        self.assertEqual(outlet['area_m2'], 10)
        self.assertEqual(outlet['runoff_l'], 10)
        self.assertEqual(outlet['fill_l'], 20)      # (5+5) mm * 2 m2
        self.assertEqual(report['totals'],
                         {'cells': 5, 'rainfall_l': 10, 'fill_l': 20})

    def test_equal_level_outlets_tie_manual(self):
        # Flat 1x3 with an outlet at each end: the middle flows to the left
        # because (0,0) is the smallest (row,col) same-H d-1 neighbour.
        elev = [[5, 5, 5]]
        doc = document(elev, rain_like(elev, 1), area=1, outlets=[
            {'id': 'R', 'row': 0, 'col': 2},
            {'id': 'L', 'row': 0, 'col': 0}])
        report = run_api(doc)
        row = report['cells'][0]
        self.assertEqual([c['outlet_id'] for c in row], ['L', 'L', 'R'])
        self.assertEqual([c['flat_distance'] for c in row], [0, 1, 0])
        self.assertEqual(row[1]['receiver'], [0, 0])
        # Outlets are emitted by Unicode code point order: L < R.
        self.assertEqual([o['id'] for o in report['outlets']], ['L', 'R'])
        self.assertEqual([o['cells'] for o in report['outlets']], [2, 1])
        self.assertEqual([o['runoff_l'] for o in report['outlets']], [2, 1])

    def test_detour_must_move_away_from_outlet(self):
        # Masked centre forces the top-right cell to drain around the bottom of
        # the ring even though the outlet is top-left.
        elev = [[1, None, 1],
                [1, None, 1],
                [1, 1, 1]]
        doc = document(elev, rain_like(elev), area=1)
        report = run_api(doc)
        h = [[c['filled_mm'] if c else None for c in row]
             for row in report['cells']]
        self.assertEqual(h, [[1, None, 1], [1, None, 1], [1, 1, 1]])
        d = [[c['flat_distance'] if c else -1 for c in row]
             for row in report['cells']]
        self.assertEqual(d, [[0, -1, 6], [1, -1, 5], [2, 3, 4]])
        # The far cell's chain walks the full ring before reaching the outlet.
        chain = []
        r, c = 0, 2
        while (r, c) != (0, 0):
            chain.append((r, c))
            r, c = report['cells'][r][c]['receiver']
        self.assertEqual(chain, [(0, 2), (1, 2), (2, 2), (2, 1),
                                 (2, 0), (1, 0)])

    def test_negative_elevation_and_zero_rainfall(self):
        elev = [[-5, -10, -3]]
        rain = [[2, 0, 1]]
        doc = document(elev, rain, area=4,
                       outlets=[{'id': 'o', 'row': 0, 'col': 1}])
        report = run_api(doc)
        row = report['cells'][0]
        # Everything already drains downhill to the -10 outlet, no fill needed.
        self.assertEqual([c['filled_mm'] for c in row], [-5, -10, -3])
        self.assertTrue(all(c['fill_mm'] == 0 for c in row))
        # Upstream runoff: the -5 cell's own 8 L; the outlet collects all 12 L.
        self.assertEqual([c['runoff_l'] for c in row], [8, 12, 4])
        self.assertEqual([c['upstream_cells'] for c in row], [1, 3, 1])
        self.assertEqual(report['totals']['rainfall_l'], 12)


# ---------------------------------------------------------------------------
# Saddle change: spill level, routing and partition change together
# ---------------------------------------------------------------------------
class SaddleChange(unittest.TestCase):
    """Two competing saddles decide which outlet drains one low pocket.

    Layout (3 rows x 5 cols), outlets A at (1,0) and B at (1,4)::

        9 9 9 9 9
        A sL 3 sR B      sR is fixed at 5; sL is 4 (low) or 6 (raised)
        9 9 9 9 9

    The level-3 pocket at (1,2) can spill either way.  With sL=4 its
    minimax path goes over sL to A, so H=4 and the partition is A.  With
    sL=6 the route over sR=5 to B is cheaper, so H=5 and the partition
    flips to B.  One saddle elevation therefore moves the spill level, the
    plateau routing and the outlet partition at the same time.
    """

    def grid(self, saddle_left):
        elev = [
            [9, 9, 9, 9, 9],
            [1, saddle_left, 3, 5, 1],
            [9, 9, 9, 9, 9],
        ]
        outlets = [{'id': 'B', 'row': 1, 'col': 4},
                   {'id': 'A', 'row': 1, 'col': 0}]
        return elev, rain_like(elev), outlets

    def _chain_end(self, cells, r, c):
        entry = cells[r][c]
        while entry['receiver'] is not None:
            r, c = entry['receiver']
            entry = cells[r][c]
        return entry['outlet_id'], (r, c)

    def test_low_saddle_drains_to_a(self):
        elev, rain, outlets = self.grid(4)
        report = run_api(document(elev, rain, 2, outlets))
        cells = report['cells']
        # Minimax spill to A through sL=4; the sR=5 route is never better.
        self.assertEqual(cells[1][2]['filled_mm'], 4)
        self.assertEqual(cells[1][1]['filled_mm'], 4)
        self.assertEqual(cells[1][3]['filled_mm'], 5)
        self.assertEqual(cells[1][2]['outlet_id'], 'A')
        # Every chain reaches the explicit outlet named on the cell.
        for r in range(3):
            for c in range(5):
                oid, end = self._chain_end(cells, r, c)
                self.assertEqual(oid, cells[r][c]['outlet_id'])
                self.assertEqual(end, (1, 0) if oid == 'A' else (1, 4))
        verify_report(self, document(elev, rain, 2, outlets), report)

    def test_raised_saddle_drains_to_b(self):
        elev, rain, outlets = self.grid(6)
        report = run_api(document(elev, rain, 2, outlets))
        cells = report['cells']
        # sL=6 is now higher than sR=5: the pocket spills toward B at level 5.
        self.assertEqual(cells[1][2]['filled_mm'], 5)
        self.assertEqual(cells[1][1]['filled_mm'], 6)
        self.assertEqual(cells[1][3]['filled_mm'], 5)
        self.assertEqual(cells[1][2]['outlet_id'], 'B')
        for r in range(3):
            for c in range(5):
                oid, end = self._chain_end(cells, r, c)
                self.assertEqual(oid, cells[r][c]['outlet_id'])
                self.assertEqual(end, (1, 0) if oid == 'A' else (1, 4))
        verify_report(self, document(elev, rain, 2, outlets), report)

    def test_change_moves_spill_routing_and_partition_together(self):
        low_elev, rain, outlets = self.grid(4)
        high_elev = [row[:] for row in low_elev]
        high_elev[1][1] = 6
        low = run_api(document(low_elev, rain, 2, outlets))
        high = run_api(document(high_elev, rain, 2, outlets))
        # Hand expectations, cell by cell, for the three middle-row cells.
        self.assertEqual([low['cells'][1][c]['filled_mm'] for c in (1, 2, 3)],
                         [4, 4, 5])
        self.assertEqual([high['cells'][1][c]['filled_mm'] for c in (1, 2, 3)],
                         [6, 5, 5])
        self.assertEqual(low['cells'][1][2]['outlet_id'], 'A')
        self.assertEqual(high['cells'][1][2]['outlet_id'], 'B')
        self.assertNotEqual(low['cells'][1][2]['receiver'],
                            high['cells'][1][2]['receiver'])
        # Fill changes with the saddle; rainfall conservation never moves.
        self.assertNotEqual(low['totals']['fill_l'], high['totals']['fill_l'])
        self.assertEqual(low['totals']['rainfall_l'],
                         high['totals']['rainfall_l'])
        self.assertEqual(low['totals']['rainfall_l'], 15 * 2)
        # Only the source hash would differ beyond fill-dependent fields.
        self.assertEqual(low['shape'], high['shape'])


# ---------------------------------------------------------------------------
# Independent threshold reference on many small random rasters
# ---------------------------------------------------------------------------
class ThresholdReference(unittest.TestCase):
    def _random_doc(self, rng, rows, cols):
        elev = []
        rain = []
        for r in range(rows):
            er, rr = [], []
            for c in range(cols):
                if rng.random() < 0.2:
                    er.append(None)
                    rr.append(None)
                else:
                    er.append(rng.randint(-20, 20))
                    rr.append(rng.randint(0, 50))
            elev.append(er)
            rain.append(rr)
        if all(v is None for row in elev for v in row):
            elev[0][0] = 0
            rain[0][0] = 0
        valid = [(r, c) for r in range(rows) for c in range(cols)
                 if elev[r][c] is not None]
        # Flood from a random valid cell to pick a reachable outlet set that
        # covers every component (independent plain BFS over passable cells).
        seen = set()
        outlets = []
        remaining = set(valid)
        while remaining:
            seed = min(remaining)
            queue = [seed]
            seen.add(seed)
            component = []
            while queue:
                r, c = queue.pop()
                component.append((r, c))
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = r + dr, c + dc
                    if (0 <= nr < rows and 0 <= nc < cols
                            and elev[nr][nc] is not None
                            and (nr, nc) not in seen):
                        seen.add((nr, nc))
                        queue.append((nr, nc))
            # One or two outlets per component, including interior ones.
            chosen = rng.sample(component, min(2, len(component)))
            for k, (r, c) in enumerate(chosen):
                outlets.append({'id': 'out-%d-%d' % (r, c),
                                'row': r, 'col': c})
            remaining -= set(component)
        rng.shuffle(outlets)
        return document(elev, rain, rng.randint(1, 7), outlets)

    def test_many_random_rasters_match_reference(self):
        rng = random.Random(20261117)
        for trial in range(60):
            rows, cols = rng.randint(1, 7), rng.randint(1, 7)
            doc = self._random_doc(rng, rows, cols)
            parsed = parse_input(doc)
            result = analyze(parsed)
            report = build_report(parsed, '%064x' % trial, result)
            # Independent H first...
            expected = reference_filled(doc['elevation_mm'],
                                        [(o['id'], o['row'], o['col'])
                                         for o in doc['outlets']])
            for r in range(rows):
                for c in range(cols):
                    if expected[r][c] is not None:
                        self.assertEqual(
                            report['cells'][r][c]['filled_mm'],
                            expected[r][c], (trial, r, c))
            # ...then the full report contract from receiver edges.
            verify_report(self, doc, report)

    def test_component_without_outlet_is_rejected(self):
        elev = [[1, None, 1]]
        doc = document(elev, rain_like(elev),
                       outlets=[{'id': 'a', 'row': 0, 'col': 0}])
        with self.assertRaises(TerrainError):
            analyze(parse_input(doc))

    def test_multiple_disconnected_components_each_drained(self):
        elev = [[3, None, 8],
                [2, None, 1]]
        rain = [[5, None, 0],
                [4, None, 7]]
        doc = document(elev, rain, area=10, outlets=[
            {'id': 'right', 'row': 1, 'col': 2},
            {'id': 'left', 'row': 1, 'col': 0}])
        report = run_api(doc)
        verify_report(self, doc, report)
        left = next(o for o in report['outlets'] if o['id'] == 'left')
        right = next(o for o in report['outlets'] if o['id'] == 'right')
        self.assertEqual(left['cells'], 2)
        self.assertEqual(right['cells'], 2)
        self.assertEqual(left['runoff_l'], 90)
        self.assertEqual(right['runoff_l'], 70)


# ---------------------------------------------------------------------------
# Input contract rejection
# ---------------------------------------------------------------------------
class ContractRejection(unittest.TestCase):
    def _base(self, **overrides):
        doc = document([[1, 2], [3, 4]], rain_like([[1, 2], [3, 4]]))
        doc.update(overrides)
        return doc

    def test_extra_and_missing_fields_rejected(self):
        doc = self._base()
        doc['unexpected'] = 1
        with self.assertRaises(ValueError):
            parse_input(doc)
        with self.assertRaises(ValueError):
            parse_input({'elevation_mm': doc['elevation_mm']})

    def test_boolean_and_float_rejected(self):
        for bad in (True, False, 1.0, -0.0):
            doc = self._base()
            doc['elevation_mm'][0][0] = bad
            with self.assertRaises((ValueError, TypeError)):
                parse_input(doc)
        doc = self._base()
        doc['cell_area_m2'] = True
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = self._base()
        doc['rainfall_mm'][0][1] = 2.5
        with self.assertRaises(ValueError):
            parse_input(doc)

    def test_bounds_enforced(self):
        doc = self._base()
        doc['elevation_mm'][0][0] = 10 ** 6 + 1
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = self._base()
        doc['rainfall_mm'][1][0] = -1
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = self._base()
        doc['cell_area_m2'] = 10 ** 6 + 1
        with self.assertRaises(ValueError):
            parse_input(doc)

    def test_ragged_rectangle_rejected(self):
        doc = document([[1, 2, 3], [4, 5]],
                       [[1, 1, 1], [1, 1]])
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = document([[1, 2], [3, 4]], [[1], [1, 2]])
        with self.assertRaises(ValueError):
            parse_input(doc)

    def test_masked_rainfall_must_be_null(self):
        doc = document([[1, None]], [[1, 0]])
        with self.assertRaisesRegex(ValueError, 'null on a masked cell'):
            parse_input(doc)
        doc = document([[1, None]], [[1, None]])
        parse_input(doc)  # null/null is the only masked form accepted

    def test_missing_rainfall_is_not_zero(self):
        doc = document([[1, 2]], [[1, None]])
        with self.assertRaisesRegex(ValueError, 'missing rainfall'):
            parse_input(doc)

    def test_all_masked_raster_rejected(self):
        doc = document([[None, None], [None, None]],
                       [[None, None], [None, None]])
        with self.assertRaisesRegex(ValueError, 'at least one'):
            parse_input(doc)

    def test_duplicate_and_illegal_outlets(self):
        doc = self._base()
        doc['outlets'].append({'id': 'o', 'row': 1, 'col': 1})
        with self.assertRaisesRegex(ValueError, 'duplicate outlet id'):
            parse_input(doc)
        doc = self._base()
        doc['outlets'] = [{'id': 'o', 'row': 0, 'col': 0},
                          {'id': 'p', 'row': 0, 'col': 0}]
        with self.assertRaisesRegex(ValueError, 'duplicate outlet cell'):
            parse_input(doc)
        doc = self._base()
        doc['outlets'] = [{'id': 'o', 'row': 2, 'col': 0}]
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = self._base()
        doc['outlets'] = [{'id': 'o', 'row': 0, 'col': 1}]
        doc['elevation_mm'][0][1] = None
        doc['rainfall_mm'][0][1] = None
        with self.assertRaisesRegex(ValueError, 'masked cell'):
            parse_input(doc)
        doc = self._base()
        doc['outlets'] = []
        with self.assertRaises(ValueError):
            parse_input(doc)
        doc = self._base()
        doc['outlets'] = [{'id': '  ', 'row': 0, 'col': 0}]
        with self.assertRaises(ValueError):
            parse_input(doc)

    def test_duplicate_json_keys_rejected_at_read(self):
        ws = Workspace(self)
        raw = ('{"elevation_mm":[[1]],"elevation_mm":[[2]],'
               '"rainfall_mm":[[1]],"cell_area_m2":1,'
               '"outlets":[{"id":"o","row":0,"col":0}]}')
        result, _src, out = ws.run(None, raw=raw)
        self.assertEqual(result.returncode, 2)
        self.assertIn('duplicate JSON key', result.stderr)
        self.assertFalse(out.exists())

    def test_nonfinite_constant_rejected(self):
        ws = Workspace(self)
        raw = ('{"elevation_mm":[[NaN]],"rainfall_mm":[[1]],'
               '"cell_area_m2":1,"outlets":[{"id":"o","row":0,"col":0}]}')
        result, _src, out = ws.run(None, raw=raw)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(out.exists())

    def test_shape_limits(self):
        # Empty layers are rejected before any dimension arithmetic.
        with self.assertRaises(ValueError):
            parse_input(document([], []))
        # A 301-column row exceeds the 1..300 cell bound.
        wide = [[1] * 301]
        with self.assertRaises(ValueError):
            parse_input(document(wide, [[1] * 301]))
        tall = [[1] for _ in range(301)]
        with self.assertRaises(ValueError):
            parse_input(document(tall, [[1] for _ in range(301)]))
        # Both bounds are inclusive at 300.
        edge = [[1] * 300 for _ in range(300)]
        parse_input(document(edge, [[1] * 300 for _ in range(300)],
                             outlets=[{'id': 'o', 'row': 0, 'col': 0}]))


# ---------------------------------------------------------------------------
# CLI behaviour, determinism and publication safety
# ---------------------------------------------------------------------------
class CommandLine(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def _doc(self):
        elev = [[5, 1, 5], [2, 9, None]]
        return document(elev, rain_like(elev), area=3)

    def test_success_and_stdout_matches_file(self):
        result, source, out = self.ws.run(self._doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.ws.read()
        self.assertEqual(json.loads(result.stdout), report)
        self.assertEqual(report['schema_version'], 'terrain-v1')
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        # No timestamps, randomness or floats anywhere in the report bytes.

        def integers_only(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertIs(type(key), str)
                    integers_only(value)
            elif isinstance(node, list):
                for value in node:
                    integers_only(value)
            elif node is not None:
                self.assertIn(type(node), (int, str))

        integers_only(report)
        text = out.read_text()
        for token in ('.', 'e+', 'E+', 'NaN', 'Infinity'):
            self.assertNotIn(token, text)

    def test_byte_identical_replay(self):
        _r, _s, out = self.ws.run(self._doc())
        first = out.read_bytes()
        result, _s, _o = self.ws.run(self._doc(), output='report.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_bytes(), first)

    def test_outlet_reorder_only_changes_source_hash(self):
        doc = self._doc()
        doc['outlets'] = [
            {'id': 'b', 'row': 0, 'col': 1},
            {'id': 'a', 'row': 0, 'col': 0}]
        r1, _s, o1 = self.ws.run(doc, output='r1.json')
        self.assertEqual(r1.returncode, 0, r1.stderr)
        doc['outlets'].reverse()
        r2, _s, o2 = self.ws.run(doc, output='r2.json')
        self.assertEqual(r2.returncode, 0, r2.stderr)
        a = json.loads(o1.read_text())
        b = json.loads(o2.read_text())
        self.assertNotEqual(a['source_sha256'], b['source_sha256'])
        a['source_sha256'] = b['source_sha256'] = None
        self.assertEqual(a, b)

    def test_failure_preserves_existing_report(self):
        good = self._doc()
        result, _s, out = self.ws.run(good)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = out.read_bytes()
        # No-outlet component, malformed JSON and contract errors all exit 2.
        bad = document([[1, None, 1]], [[1, None, 1]],
                       outlets=[{'id': 'a', 'row': 0, 'col': 0}])
        for raw in (json.dumps(bad), '{not json', '{}'):
            result, _s, _o = self.ws.run(None, name='bad.json', raw=raw)
            self.assertEqual(result.returncode, 2, raw)
            self.assertTrue(result.stderr.strip())
            self.assertEqual(out.read_bytes(), before)

    def test_source_and_output_same_path_rejected(self):
        doc = self._doc()
        source = write_json(self.ws.path('same.json'), doc)
        before = source.read_bytes()
        result = __import__('terrain_helpers', fromlist=['cli']).cli(
            source, source)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), before)
        # Direct API call refuses as well, including the decoded-document case.
        with self.assertRaises(ValueError):
            run_pipeline(source, source)

    def test_symlink_output_rejected(self):
        doc = self._doc()
        source = write_json(self.ws.path('src.json'), doc)
        target = self.ws.path('target.json')
        link = self.ws.path('link.json')
        link.symlink_to(target)
        result = __import__('terrain_helpers', fromlist=['cli']).cli(
            source, link)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(target.exists())

    def test_save_failure_leaves_old_report(self):
        result, _s, out = self.ws.run(self._doc())
        self.assertEqual(result.returncode, 0)
        before = out.read_bytes()
        # Pointing the output at a directory makes the atomic replace fail.
        source = write_json(self.ws.path('src2.json'), self._doc())
        directory = self.ws.path('adirectory')
        directory.mkdir()
        run = __import__('terrain_helpers', fromlist=['cli']).cli(
            source, directory)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(out.read_bytes(), before)


# ---------------------------------------------------------------------------
# Scale boundaries, non-recursive confluence, large exact integers
# ---------------------------------------------------------------------------
class ScaleAndRecursion(unittest.TestCase):
    def test_flat_300x300_plateau(self):
        n = 300
        elev = [[7] * n for _ in range(n)]
        rain = [[10 ** 9] * n for _ in range(n)]
        outlets = [{'id': 'o', 'row': n - 1, 'col': n - 1}]
        parsed = parse_input(document(elev, rain, 10 ** 6, outlets))
        start = time.perf_counter()
        result = analyze(parsed)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 30.0)
        report = build_report(parsed, 'f' * 64, result)
        # Manhattan distance on the flat plateau; the far corner is farthest.
        self.assertEqual(result['distance'][0], 2 * (n - 1))
        self.assertEqual(result['distance'][-1], 0)
        # The whole 90 000-cell basin converges on the single outlet.
        self.assertEqual(result['upstream_cells'][-1], n * n)
        per_cell = 10 ** 9 * 10 ** 6           # litres, exact big integer
        self.assertEqual(result['runoff_l'][-1], n * n * per_cell)
        self.assertEqual(report['totals']['rainfall_l'], n * n * per_cell)
        self.assertEqual(report['totals']['fill_l'], 0)
        verify_report(self, document(elev, rain, 10 ** 6, outlets), report)

    def _snake(self, n):
        # Horizontal snake: solid top row, alternating walls below, one outlet.
        wall = [[None] * n for _ in range(n)]
        for r in range(n):
            for c in range(n):
                wall[r][c] = 1 if r % 2 == 0 else None
            if r % 2 == 1:
                wall[r][n - 1 if (r // 2) % 2 == 0 else 0] = 1
        rain = [[None if v is None else 1 for v in row] for row in wall]
        return wall, rain

    def test_long_snake_channel_is_iterative(self):
        n = 300
        elev, rain = self._snake(n)
        outlets = [{'id': 'o', 'row': 0, 'col': 0}]
        parsed = parse_input(document(elev, rain, 1, outlets))
        # A chain far longer than sys.getrecursionlimit() must not raise.
        self.assertGreater(sum(v is not None for row in elev for v in row),
                           sys.getrecursionlimit())
        result = analyze(parsed)
        report = build_report(parsed, 'a' * 64, result)
        valid = sum(v is not None for row in elev for v in row)
        self.assertEqual(result['upstream_cells'][0], valid)
        # One linear memoized pass proves every chain ends at the outlet,
        # without per-cell chain re-walking or recursion.
        terminal = {}
        for r in range(n):
            for c in range(n):
                if elev[r][c] is None:
                    continue
                path = []
                cr, cc = r, c
                while (cr, cc) not in terminal and \
                        report['cells'][cr][cc]['receiver'] is not None:
                    path.append((cr, cc))
                    cr, cc = report['cells'][cr][cc]['receiver']
                end = terminal.get((cr, cc), (cr, cc))
                for point in path:
                    terminal[point] = end
                self.assertEqual(
                    report['cells'][end[0]][end[1]]['outlet_id'], 'o')
        verify_report(self, document(elev, rain, 1, outlets), report)

    def test_large_volumes_are_exact_integers(self):
        elev = [[0, -1000000]]
        rain = [[10 ** 9, 10 ** 9]]
        doc = document(elev, rain, 10 ** 6,
                       outlets=[{'id': 'o', 'row': 0, 'col': 1}])
        report = run_api(doc)
        total = 2 * 10 ** 15
        self.assertEqual(report['totals']['rainfall_l'], total)
        # The level-0 cell already sits above the -10^6 outlet, so no ponding:
        # raw elevations are never lowered and fill only ever raises ground.
        self.assertEqual(report['cells'][0][0]['fill_mm'], 0)
        self.assertEqual(report['cells'][0][1]['fill_mm'], 0)
        self.assertEqual(report['outlets'][0]['fill_l'], 0)
        # Both cells still contribute their full rainfall to the outlet.
        self.assertEqual(report['outlets'][0]['runoff_l'], 2 * 10 ** 15)
        for value in (report['totals']['rainfall_l'],
                      report['cells'][0][0]['runoff_l'],
                      report['outlets'][0]['fill_l']):
            self.assertIs(type(value), int)


if __name__ == '__main__':
    unittest.main()
