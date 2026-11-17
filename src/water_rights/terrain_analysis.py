"""Terrain drainage analysis: minimax fill, plateau routing and confluence.

Every routine here is iterative (no recursion) and uses only exact integer
arithmetic.  The grid is kept in flat row-major arrays of length
``rows * cols`` (mask cells included), so the auxiliary space stays O(N) for
N = rows*cols.

Three exact results are produced, each with its own correctness argument:

* **Filled elevation H (global minimum ponding).**  H[c] is the smallest value
  such that some edge-adjacent path from c to an *explicit* outlet stays at
  elevation <= H[c]; the original surface is never lowered.  This is a
  minimax-path problem solved by one heap relaxation seeded with all outlets
  (see :func:`filled_elevations`).
* **Acyclic plateau routing.**  Inside every connected equal-H plateau the
  anchors (outlets, and cells bordering a strictly lower H) are distance 0; a
  level-synchronous breadth-first search gives the minimum edge distance d, and
  every tie is broken by fixed (row, col) scans, never by traversal order.
  Each receiver edge goes to a lower-H cell or a same-H cell with d-1, so the
  receiver forest is acyclic and every chain ends at an explicit outlet.
* **Confluence conservation.**  With labels propagated from outlets along
  receiver edges, one reverse pass sums each cell (including itself) into its
  receiver; therefore every outlet receives exactly the rainfall and area of
  its partition, and the outlet totals equal the global raw rainfall totals.
"""
import heapq

from .terrain_model import TerrainError

# Fixed neighbor offsets; neighbors are re-sorted by (row, col) wherever a tie
# rule needs it, but scanning these four is itself row/col deterministic.
_OFFSETS = ((-1, 0), (0, -1), (0, 1), (1, 0))


def _neighbors(index, rows, cols, elevation):
    """Yield (neighbor_index, neighbor_row, neighbor_col) for passable cells."""
    r, c = divmod(index, cols)
    for dr, dc in _OFFSETS:
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            ni = nr * cols + nc
            if elevation[ni] is not None:
                yield ni, nr, nc


def filled_elevations(parsed):
    """Return the exact minimax filled elevation H of every valid cell.

    H[c] = min over all valid-cell paths from c to any explicit outlet of the
    maximum original elevation on the path, including both endpoints.  This is
    Dijkstra-style relaxation with edge weight

        relax(v) = max(H[u], elevation[v]).

    The weight is monotone non-decreasing along every relaxation, so the heap
    always finalizes a cell at its minimax value; elevations are never reduced
    and mask cells and the raster boundary are never traversed.  O(N log N)
    time and O(N) space, with one heap entry finalized per valid cell.
    """
    rows = parsed['rows']
    cols = parsed['cols']
    elevation = parsed['elevation']
    n = rows * cols
    filled = [None] * n
    heap = []
    for outlet_id, r, c in parsed['outlets']:
        i = r * cols + c
        # Outlets are the only seeds; an outlet's H equals its own elevation.
        filled[i] = elevation[i]
        heapq.heappush(heap, (elevation[i], i))
    while heap:
        level, ui = heapq.heappop(heap)
        if level != filled[ui]:
            continue
        for vi, _nr, _nc in _neighbors(ui, rows, cols, elevation):
            candidate = level
            if elevation[vi] > candidate:
                candidate = elevation[vi]
            if filled[vi] is None or candidate < filled[vi]:
                filled[vi] = candidate
                heapq.heappush(heap, (candidate, vi))
    for i, value in enumerate(filled):
        if elevation[i] is not None and value is None:
            raise TerrainError(
                'a passable connected component has no outlet; every component '
                'must contain an explicit outlet')
    return filled


def route_plateaus(parsed, filled):
    """Assign flat distance d and deterministic receiver to every valid cell.

    Returns ``(flat_distance, receiver)`` flat arrays.  Outlets receive
    ``None``; every other cell points at one passable neighbor.  The work is a
    multi-source breadth-first search restricted to equal-H edges, performed
    level by level so that distances are provably minimum edge counts and no
    tie depends on iteration order.
    """
    rows = parsed['rows']
    cols = parsed['cols']
    elevation = parsed['elevation']
    outlet_at = parsed['outlet_at']
    n = rows * cols
    distance = [-1] * n
    receiver = [None] * n

    # Pass 1: anchors.  Every equal-H plateau contains at least one anchor:
    # otherwise no relaxation could ever have carried its H into the plateau,
    # and the heap is seeded solely by explicit outlets.
    frontier = []
    for i in range(n):
        if elevation[i] is None:
            continue
        if outlet_at[i] is not None:
            distance[i] = 0
            frontier.append(i)
            continue
        best = None  # (H, row, col) of the strictly lower-H neighbor
        best_index = None
        for ni, nr, nc in _neighbors(i, rows, cols, elevation):
            if filled[ni] < filled[i]:
                key = (filled[ni], nr, nc)
                if best is None or key < best:
                    best = key
                    best_index = ni
        if best_index is not None:
            distance[i] = 0
            receiver[i] = best_index
            frontier.append(i)

    # Pass 2: level-synchronous BFS inside each equal-H plateau.  A cell is
    # gathered once; its receiver is then chosen by a fixed (row, col) scan of
    # same-H neighbors whose distance is exactly one less, so reordering the
    # frontier cannot change any result.
    level = 0
    while frontier:
        gathered = []
        gathered_mark = set()
        for ui in frontier:
            for vi, _nr, _nc in _neighbors(ui, rows, cols, elevation):
                if distance[vi] == -1 and filled[vi] == filled[ui] \
                        and vi not in gathered_mark:
                    gathered_mark.add(vi)
                    gathered.append(vi)
        next_level = level + 1
        for vi in gathered:
            r, c = divmod(vi, cols)
            choice = None  # smallest (row, col) same-H neighbor at d-1
            choice_index = None
            for ni, nr, nc in _neighbors(vi, rows, cols, elevation):
                if filled[ni] == filled[vi] and distance[ni] == level:
                    key = (nr, nc)
                    if choice is None or key < choice:
                        choice = key
                        choice_index = ni
            # A same-H neighbor at the previous level must exist: the cell was
            # gathered through one, and distances never shrink.
            if choice_index is None:  # defensive, unreachable on valid input
                raise TerrainError('plateau routing could not find a receiver')
            distance[vi] = next_level
            receiver[vi] = choice_index
        frontier = gathered
        level = next_level

    for i in range(n):
        if elevation[i] is not None and distance[i] < 0:
            raise TerrainError('plateau without an anchor; routing is incomplete')
    return distance, receiver


def analyze(parsed):
    """Run the full pipeline on a normalized input from :mod:`terrain_model`.

    Returns a flat-array result mapping with H, fill height, flat distance,
    receiver indices, final outlet labels, upstream counts and exact runoff in
    litres.  All accumulation is iterative, so long routing chains are
    unaffected by Python's recursion limit.
    """
    rows = parsed['rows']
    cols = parsed['cols']
    elevation = parsed['elevation']
    rainfall = parsed['rainfall']
    area = parsed['cell_area_m2']
    outlet_at = parsed['outlet_at']
    n = rows * cols

    filled = filled_elevations(parsed)
    distance, receiver = route_plateaus(parsed, filled)

    fill_mm = [None] * n
    for i in range(n):
        if elevation[i] is not None:
            fill_mm[i] = filled[i] - elevation[i]

    # Deterministic processing order: receiver always precedes its cell because
    # a receiver has either strictly lower H or equal H and d-1.
    order = [i for i in range(n) if elevation[i] is not None]
    order.sort(key=lambda i: (filled[i], distance[i], i))

    # Forward pass: final outlet labels travel down each receiver chain.
    outlet_label = [None] * n
    for i in order:
        if outlet_at[i] is not None:
            outlet_label[i] = outlet_at[i]
        else:
            outlet_label[i] = outlet_label[receiver[i]]

    # Reverse pass: each cell contributes itself and its own rainfall, then the
    # running totals flow one edge at a time into the receiver.  Iterating the
    # receiver-before-cell order backwards guarantees every upstream subtree is
    # fully summed before the cell itself is added to its receiver.
    upstream_cells = [0] * n
    runoff_l = [0] * n
    for i in reversed(order):
        upstream_cells[i] += 1
        runoff_l[i] += rainfall[i] * area
        ri = receiver[i]
        if ri is not None:
            upstream_cells[ri] += upstream_cells[i]
            runoff_l[ri] += runoff_l[i]

    return {
        'rows': rows,
        'cols': cols,
        'cell_area_m2': area,
        'filled': filled,
        'fill_mm': fill_mm,
        'distance': distance,
        'receiver': receiver,
        'outlet_label': outlet_label,
        'upstream_cells': upstream_cells,
        'runoff_l': runoff_l,
    }


def build_report(parsed, source_sha256, result=None):
    """Assemble the exact ``terrain-v1`` report contract from flat arrays."""
    if result is None:
        result = analyze(parsed)
    rows = result['rows']
    cols = result['cols']
    area = result['cell_area_m2']
    elevation = parsed['elevation']
    cells = []
    total_cells = 0
    total_rainfall = 0
    total_fill = 0
    per_outlet = {}
    for outlet_id, _r, _c in parsed['outlets']:
        per_outlet[outlet_id] = {
            'id': outlet_id, 'cells': 0, 'area_m2': 0,
            'runoff_l': 0, 'fill_l': 0,
        }
    for r in range(rows):
        row_out = []
        base = r * cols
        for c in range(cols):
            i = base + c
            if elevation[i] is None:
                row_out.append(None)
                continue
            total_cells += 1
            ri = result['receiver'][i]
            entry = {
                'filled_mm': result['filled'][i],
                'fill_mm': result['fill_mm'][i],
                'flat_distance': result['distance'][i],
                'receiver': None if ri is None else [ri // cols, ri % cols],
                'outlet_id': result['outlet_label'][i],
                'upstream_cells': result['upstream_cells'][i],
                'runoff_l': result['runoff_l'][i],
            }
            row_out.append(entry)
            bucket = per_outlet[result['outlet_label'][i]]
            bucket['cells'] += 1
            bucket['area_m2'] += area
            bucket['runoff_l'] += parsed['rainfall'][i] * area
            bucket['fill_l'] += result['fill_mm'][i] * area
            total_rainfall += parsed['rainfall'][i] * area
            total_fill += result['fill_mm'][i] * area
        cells.append(row_out)

    # Sorting plain Python strings orders by Unicode code point, which is the
    # required outlet order; input order carries no business priority.
    coordinates = {oid: (r, c) for oid, r, c in parsed['outlets']}
    outlets = []
    for outlet_id in sorted(per_outlet):
        bucket = per_outlet[outlet_id]
        r, c = coordinates[outlet_id]
        outlets.append({
            'id': outlet_id, 'row': r, 'col': c,
            'cells': bucket['cells'],
            'area_m2': bucket['area_m2'],
            'runoff_l': bucket['runoff_l'],
            'fill_l': bucket['fill_l'],
        })
    return {
        'schema_version': 'terrain-v1',
        'source_sha256': source_sha256,
        'shape': [rows, cols],
        'cell_area_m2': area,
        'cells': cells,
        'outlets': outlets,
        'totals': {
            'cells': total_cells,
            'rainfall_l': total_rainfall,
            'fill_l': total_fill,
        },
    }
