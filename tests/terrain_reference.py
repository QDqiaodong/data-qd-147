"""Independent reference implementation for terrain acceptance tests.

Everything here is written from the textual contract, without importing any
production terrain function.  Two independent pieces are provided:

* :func:`reference_filled` recomputes the filled elevation H by scanning
  elevation thresholds and flood-filling from the explicit outlets, instead of
  the production heap relaxation;
* :func:`verify_report` reads only the decoded report and the raw input and
  re-derives the plateau distances, the receiver rules, the receiver-forest
  acyclicity, the outlet partition and every accumulated volume straight from
  the published receiver edges.
"""
from collections import deque


def _neighbours(r, c, rows, cols, valid):
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols and valid[nr][nc]:
            yield nr, nc


def reference_filled(elevation, outlets):
    """Threshold flood fill: H[c] is the first water level that connects c.

    For every distinct original elevation in ascending order, flood from all
    explicit outlets through cells whose elevation is at or below that level.
    A cell first reached at level t has minimax path value t.  This is a
    deliberately different algorithm from the production Dijkstra-style heap.
    """
    rows = len(elevation)
    cols = len(elevation[0])
    valid = [[v is not None for v in row] for row in elevation]
    levels = sorted({v for row in elevation for v in row if v is not None})
    filled = [[None] * cols for _ in range(rows)]
    for level in levels:
        # Canonical threshold definition: fresh flood from every eligible
        # explicit outlet through cells at or below this level.  A cell first
        # covered here has minimax path value exactly this level.
        reachable = set()
        seeds = deque()
        for oid, r, c in outlets:
            if elevation[r][c] <= level and (r, c) not in reachable:
                reachable.add((r, c))
                seeds.append((r, c))
        while seeds:
            r, c = seeds.popleft()
            for nr, nc in _neighbours(r, c, rows, cols, valid):
                if (nr, nc) not in reachable and elevation[nr][nc] <= level:
                    reachable.add((nr, nc))
                    seeds.append((nr, nc))
        for r, c in reachable:
            if filled[r][c] is None:
                filled[r][c] = level
    return filled


def reference_plateaus(elevation, filled, outlet_cells):
    """Independent BFS per equal-H plateau returning anchors and distances."""
    rows = len(elevation)
    cols = len(elevation[0])
    valid = [[v is not None for v in row] for row in elevation]
    distance = [[-1] * cols for _ in range(rows)]
    anchors = set()
    frontier = deque()
    for r in range(rows):
        for c in range(cols):
            if not valid[r][c]:
                continue
            is_anchor = (r, c) in outlet_cells
            if not is_anchor:
                for nr, nc in _neighbours(r, c, rows, cols, valid):
                    if filled[nr][nc] < filled[r][c]:
                        is_anchor = True
                        break
            if is_anchor:
                anchors.add((r, c))
                distance[r][c] = 0
                frontier.append((r, c))
    # Plain multi-source BFS over same-H edges.
    while frontier:
        r, c = frontier.popleft()
        for nr, nc in _neighbours(r, c, rows, cols, valid):
            if distance[nr][nc] == -1 and filled[nr][nc] == filled[r][c]:
                distance[nr][nc] = distance[r][c] + 1
                frontier.append((nr, nc))
    return anchors, distance


def verify_report(testcase, data, report):
    """Assert the whole terrain-v1 contract from the report alone."""
    elevation = data['elevation_mm']
    rainfall = data['rainfall_mm']
    area = data['cell_area_m2']
    rows, cols = len(elevation), len(elevation[0])
    valid = [[v is not None for v in row] for row in elevation]
    outlets = [(o['id'], o['row'], o['col']) for o in data['outlets']]
    outlet_cells = {(r, c): oid for oid, r, c in outlets}

    testcase.assertEqual(set(report), {
        'schema_version', 'source_sha256', 'shape', 'cell_area_m2',
        'cells', 'outlets', 'totals'})
    testcase.assertEqual(report['schema_version'], 'terrain-v1')
    testcase.assertEqual(report['shape'], [rows, cols])
    testcase.assertEqual(report['cell_area_m2'], area)
    testcase.assertIsInstance(report['source_sha256'], str)

    # --- H against the independent threshold reference ---------------------
    expected_h = reference_filled(elevation, outlets)
    cells = report['cells']
    testcase.assertEqual(len(cells), rows)
    for r in range(rows):
        testcase.assertEqual(len(cells[r]), cols)
        for c in range(cols):
            if not valid[r][c]:
                testcase.assertIsNone(cells[r][c])
                continue
            entry = cells[r][c]
            testcase.assertEqual(set(entry), {
                'filled_mm', 'fill_mm', 'flat_distance', 'receiver',
                'outlet_id', 'upstream_cells', 'runoff_l'})
            for key in ('filled_mm', 'fill_mm', 'flat_distance',
                        'upstream_cells', 'runoff_l'):
                testcase.assertIs(type(entry[key]), int)
            testcase.assertEqual(entry['filled_mm'], expected_h[r][c])
            testcase.assertEqual(entry['fill_mm'],
                                 expected_h[r][c] - elevation[r][c])
            testcase.assertGreaterEqual(entry['fill_mm'], 0)

    # --- plateau anchors and minimum distances -----------------------------
    anchors, expected_d = reference_plateaus(
        elevation, expected_h, set(outlet_cells))
    for r in range(rows):
        for c in range(cols):
            if valid[r][c]:
                testcase.assertEqual(cells[r][c]['flat_distance'],
                                     expected_d[r][c])
                testcase.assertGreaterEqual(expected_d[r][c], 0)

    # --- receiver tie rules -------------------------------------------------
    for r in range(rows):
        for c in range(cols):
            if not valid[r][c]:
                continue
            entry = cells[r][c]
            receiver = entry['receiver']
            if (r, c) in outlet_cells:
                testcase.assertIsNone(receiver)
                testcase.assertEqual(entry['outlet_id'], outlet_cells[(r, c)])
                continue
            testcase.assertIsNotNone(receiver)
            rr, rc = receiver
            testcase.assertTrue(valid[rr][rc])
            d = entry['flat_distance']
            if d == 0:
                # Strictly lower-H neighbour, minimal (H, row, col).
                testcase.assertLess(cells[rr][rc]['filled_mm'],
                                    entry['filled_mm'])
                lower = [(expected_h[nr][nc], nr, nc)
                         for nr, nc in _neighbours(r, c, rows, cols, valid)
                         if expected_h[nr][nc] < expected_h[r][c]]
                testcase.assertEqual((expected_h[rr][rc], rr, rc), min(lower))
            else:
                # Same-H neighbour with d-1, minimal (row, col).
                testcase.assertEqual(cells[rr][rc]['filled_mm'],
                                     entry['filled_mm'])
                testcase.assertEqual(cells[rr][rc]['flat_distance'], d - 1)
                same = [(nr, nc)
                        for nr, nc in _neighbours(r, c, rows, cols, valid)
                        if expected_h[nr][nc] == expected_h[r][c]
                        and expected_d[nr][nc] == d - 1]
                testcase.assertEqual((rr, rc), min(same))

    # --- acyclic receiver forest ending at explicit outlets ----------------
    # One memoized walk per cell gives O(N) total work regardless of chain
    # length; a revisited cell inside an unfinished walk would be a cycle.
    terminal = {}

    def walk(r0, c0):
        path = []
        on_path = set()
        r, c = r0, c0
        while (r, c) not in terminal:
            if (r, c) in on_path:
                raise AssertionError('receiver cycle at %r' % (r, c))
            on_path.add((r, c))
            path.append((r, c))
            nxt = cells[r][c]['receiver']
            if nxt is None:
                break
            r, c = nxt
        end = (r, c) if cells[r][c]['receiver'] is None else terminal[(r, c)]
        for pr, pc in path:
            terminal[(pr, pc)] = end
        return end

    for r in range(rows):
        for c in range(cols):
            if not valid[r][c]:
                continue
            end = walk(r, c)
            testcase.assertIn(end, outlet_cells)
            testcase.assertEqual(cells[r][c]['outlet_id'], outlet_cells[end])

    # --- independent accumulation straight from receiver edges --------------
    upstream = {(r, c): 1 for r in range(rows) for c in range(cols)
                if valid[r][c]}
    runoff = {(r, c): rainfall[r][c] * area
              for r in range(rows) for c in range(cols) if valid[r][c]}
    # Cells before receivers: process by (H, d) descending so subtrees finish
    # first; this ordering is derived from the report, not from production code.
    order = sorted(((r, c) for r in range(rows) for c in range(cols)
                    if valid[r][c]),
                   key=lambda p: (cells[p[0]][p[1]]['filled_mm'],
                                  cells[p[0]][p[1]]['flat_distance']),
                   reverse=True)
    for r, c in order:
        entry = cells[r][c]
        testcase.assertEqual(entry['upstream_cells'], upstream[(r, c)])
        testcase.assertEqual(entry['runoff_l'], runoff[(r, c)])
        nxt = entry['receiver']
        if nxt is not None:
            upstream[tuple(nxt)] += upstream[(r, c)]
            runoff[tuple(nxt)] += runoff[(r, c)]

    # --- outlet summaries and global conservation ---------------------------
    # Single pass over the grid; outlet coordinates come from the input, never
    # from production data structures.
    coord = {oid: (r, c) for oid, r, c in outlets}
    buckets = {oid: {'cells': 0, 'runoff_l': 0, 'fill_l': 0}
               for oid, _r, _c in outlets}
    for r in range(rows):
        for c in range(cols):
            if not valid[r][c]:
                continue
            oid = cells[r][c]['outlet_id']
            testcase.assertIn(oid, buckets)
            bucket = buckets[oid]
            bucket['cells'] += 1
            bucket['runoff_l'] += rainfall[r][c] * area
            bucket['fill_l'] += (expected_h[r][c] - elevation[r][c]) * area
    expected_outlets = []
    total_cells = total_rain = total_fill = 0
    for oid in sorted(buckets):
        bucket = buckets[oid]
        r, c = coord[oid]
        expected_outlets.append({
            'id': oid, 'row': r, 'col': c,
            'cells': bucket['cells'],
            'area_m2': bucket['cells'] * area,
            'runoff_l': bucket['runoff_l'],
            'fill_l': bucket['fill_l']})
        testcase.assertTrue(bucket['cells'])
        total_cells += bucket['cells']
        total_rain += bucket['runoff_l']
        total_fill += bucket['fill_l']
    testcase.assertEqual(report['outlets'], expected_outlets)
    testcase.assertEqual(report['totals'], {
        'cells': total_cells, 'rainfall_l': total_rain,
        'fill_l': total_fill})
    raw_rain = sum(rainfall[r][c] for r in range(rows) for c in range(cols)
                   if valid[r][c]) * area
    testcase.assertEqual(total_rain, raw_rain)
    testcase.assertEqual(
        sum(o['runoff_l'] for o in report['outlets']), raw_rain)
    return expected_h, expected_d
