"""Restricted input contract for the terrain drainage base-map product.

The input is the same 8 MB bounded, duplicate-key-rejecting JSON document used
by the other offline products (see :mod:`water_rights.common`).  This module
only normalizes and validates the decoded object; it performs no I/O and never
coerces a missing measurement into zero.
"""
from .common import ident, require

MAX_SIDE = 300
MIN_ELEVATION = -10 ** 6
MAX_ELEVATION = 10 ** 6
MAX_RAINFALL = 10 ** 9
MAX_CELL_AREA = 10 ** 6


class TerrainError(ValueError):
    """Contract violation; the CLI maps it to exit status 2."""


def _strict_int(value, low, high, label):
    # ``type(value) is int`` deliberately rejects ``bool`` (``True`` is an int).
    if type(value) is not int or not low <= value <= high:
        raise TerrainError('%s must be an integer in [%d, %d]' % (label, low, high))
    return value


def _grid(layer, rows_name, cols_name, rows, cols):
    if not isinstance(layer, list) or len(layer) != rows:
        raise TerrainError('%s must be a %d-row rectangular array' % (rows_name, rows))
    for r, row in enumerate(layer):
        if not isinstance(row, list) or len(row) != cols:
            raise TerrainError('%s row %d must contain exactly %d cells'
                               % (rows_name, r, cols))


def parse_input(data):
    """Validate the exact input object and return a flat, normalized model.

    The returned mapping contains::

        rows, cols, cell_area_m2,
        elevation  (flat list of int or None),
        rainfall   (flat list of int or None),
        outlets    (list of (id, row, col) tuples in input order),
        outlet_at  (flat list of outlet id or None)
    """
    require(data, 'elevation_mm rainfall_mm cell_area_m2 outlets')
    elevation_raw = data['elevation_mm']
    rainfall_raw = data['rainfall_mm']
    if not isinstance(elevation_raw, list) or not (1 <= len(elevation_raw) <= MAX_SIDE):
        raise TerrainError('elevation_mm must have 1..%d rows' % MAX_SIDE)
    rows = len(elevation_raw)
    first = elevation_raw[0]
    if not isinstance(first, list) or not (1 <= len(first) <= MAX_SIDE):
        raise TerrainError('elevation_mm rows must be arrays of 1..%d cells' % MAX_SIDE)
    cols = len(first)
    _grid(elevation_raw, 'elevation_mm', 'cols', rows, cols)
    _grid(rainfall_raw, 'rainfall_mm', 'cols', rows, cols)

    n = rows * cols
    elevation = [None] * n
    rainfall = [None] * n
    valid_count = 0
    for r in range(rows):
        base = r * cols
        for c in range(cols):
            i = base + c
            ev = elevation_raw[r][c]
            if ev is not None:
                _strict_int(ev, MIN_ELEVATION, MAX_ELEVATION,
                            'elevation_mm[%d][%d]' % (r, c))
                elevation[i] = ev
                valid_count += 1
            rv = rainfall_raw[r][c]
            if ev is None:
                if rv is not None:
                    raise TerrainError(
                        'rainfall_mm[%d][%d] must be null on a masked cell' % (r, c))
            else:
                if rv is None:
                    raise TerrainError(
                        'rainfall_mm[%d][%d] is missing; missing rainfall is not zero'
                        % (r, c))
                _strict_int(rv, 0, MAX_RAINFALL, 'rainfall_mm[%d][%d]' % (r, c))
                rainfall[i] = rv
    if valid_count == 0:
        raise TerrainError('the raster needs at least one passable (non-null) cell')

    cell_area = _strict_int(data['cell_area_m2'], 1, MAX_CELL_AREA, 'cell_area_m2')

    outlets_raw = data['outlets']
    if not isinstance(outlets_raw, list) or not outlets_raw:
        raise TerrainError('outlets must be a non-empty array')
    outlets = []
    seen_ids = set()
    seen_cells = set()
    outlet_at = [None] * n
    for entry in outlets_raw:
        require(entry, 'id row col')
        outlet_id = ident(entry['id'])
        if outlet_id in seen_ids:
            raise TerrainError('duplicate outlet id: ' + outlet_id)
        r = _strict_int(entry['row'], 0, rows - 1, 'outlet row')
        c = _strict_int(entry['col'], 0, cols - 1, 'outlet col')
        i = r * cols + c
        if elevation[i] is None:
            raise TerrainError('outlet %s sits on a masked cell at (%d,%d)'
                               % (outlet_id, r, c))
        if (r, c) in seen_cells:
            raise TerrainError('duplicate outlet cell (%d,%d)' % (r, c))
        seen_ids.add(outlet_id)
        seen_cells.add((r, c))
        outlets.append((outlet_id, r, c))
        outlet_at[i] = outlet_id

    return {
        'rows': rows,
        'cols': cols,
        'cell_area_m2': cell_area,
        'elevation': elevation,
        'rainfall': rainfall,
        'outlets': outlets,
        'outlet_at': outlet_at,
    }
