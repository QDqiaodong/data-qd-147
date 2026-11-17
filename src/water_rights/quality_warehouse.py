"""Persistent warehouse for quality observations, calibrations and episodes.

The database is an independent data warehouse: it never reads allocation,
delivery, tabulation or tariff state.  It keeps every accepted change event,
the effective revision of each entity, the input breakpoint and the materialized
coverage / excess / alert intervals derived from the effective facts.  One
ingest commits facts, derived intervals and the breakpoint in a single
transaction; any failure leaves all three at their previous values.

All temporal arithmetic is exact.  Times and values are Fractions, so
observation interpolation, calibration crossing points, threshold
intersections and integrals never use floats or fixed sampling.
"""
import os
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .common import read, save
from .quality_model import (
    DAY, MAX_EVENTS, QualityError, canonical, config_pairs,
    parse_rational_text, read_changes, validate_config, validate_event,
)

SCHEMA = 'quality-warehouse-v1'
_BUSY = 30


# ---------------------------------------------------------------------------
# small helpers

def _connect(path):
    conn = sqlite3.connect(str(path), timeout=_BUSY, isolation_level=None)
    conn.execute('PRAGMA busy_timeout = %d' % (_BUSY * 1000))
    return conn


def _frac_text(value):
    value = Fraction(value)
    return '%d/%d' % (value.numerator, value.denominator)


def _frac_json(value):
    value = Fraction(value)
    return [value.numerator, value.denominator]


def _meta(conn):
    return {k: v for k, v in conn.execute('SELECT k, v FROM meta')}


def _open_warehouse(db_path):
    if not os.path.lexists(db_path):
        raise QualityError('database does not exist: ' + str(db_path))
    try:
        conn = _connect(db_path)
    except sqlite3.Error as error:
        raise QualityError('cannot open warehouse: ' + str(error)) from error
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
    except sqlite3.Error as error:
        conn.close()
        raise QualityError('not a quality warehouse database') from error
    if not row or row[0] != SCHEMA:
        conn.close()
        raise QualityError('not a quality warehouse database')
    return conn


def _config_from_meta(meta):
    import json
    return validate_config(json.loads(meta['config_json']))


def _rollback(conn):
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# init

def init_warehouse(source_path, db_path):
    """Create a new warehouse; refuses to overwrite any existing file."""
    if Path(source_path).resolve() == Path(db_path).resolve():
        raise QualityError('input and database must differ')
    data, digest = read(source_path)
    config = validate_config(data)
    dest = Path(db_path)
    if os.path.lexists(dest):
        raise QualityError('database already exists: ' + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    os.close(fd)
    conn = None
    try:
        conn = _connect(dest)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        conn.execute(
            'CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,'
            ' entity TEXT NOT NULL, revision INTEGER NOT NULL,'
            ' deleted INTEGER NOT NULL, raw_json TEXT NOT NULL,'
            ' station TEXT, analyte TEXT, obs_at INTEGER, obs_value TEXT,'
            ' cal_start INTEGER, cal_end INTEGER, cal_a TEXT, cal_b TEXT,'
            ' UNIQUE (kind, entity, revision))')
        conn.execute('CREATE TABLE derived_known (station TEXT NOT NULL,'
                     ' lo TEXT NOT NULL, hi TEXT NOT NULL)')
        conn.execute('CREATE TABLE derived_unsafe (station TEXT NOT NULL,'
                     ' lo TEXT NOT NULL, hi TEXT NOT NULL)')
        conn.execute('CREATE TABLE derived_excess (station TEXT NOT NULL,'
                     ' analyte TEXT NOT NULL, lo TEXT NOT NULL, hi TEXT NOT NULL,'
                     ' m TEXT NOT NULL, c TEXT NOT NULL)')
        conn.execute('CREATE TABLE derived_alert (station TEXT NOT NULL,'
                     ' lo TEXT NOT NULL, hi TEXT NOT NULL)')
        conn.execute('CREATE INDEX derived_known_station ON derived_known(station)')
        conn.execute('CREATE INDEX derived_unsafe_station ON derived_unsafe(station)')
        conn.execute('CREATE INDEX derived_excess_station ON derived_excess(station, analyte)')
        conn.execute('CREATE INDEX derived_alert_station ON derived_alert(station)')
        meta = {
            'schema': SCHEMA,
            'start': str(config['start']),
            'end': str(config['end']),
            'last_seq': '0',
            'event_count': '0',
            'config_sha256': digest,
            'config_json': canonical(data),
        }
        for key, value in meta.items():
            conn.execute('INSERT INTO meta (k, v) VALUES (?, ?)', (key, value))
        conn.execute('COMMIT')
    except Exception:
        if conn is not None:
            _rollback(conn)
            conn.close()
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    conn.close()
    return {
        'schema_version': 1,
        'kind': 'quality-init',
        'start': config['start'],
        'end': config['end'],
        'stations': len(config['stations']),
        'analytes': sum(len(station['analyte_ids']) for station in config['stations']),
        'seq': 0,
        'config_sha256': digest,
    }


# ---------------------------------------------------------------------------
# effective facts

def _load_facts(conn):
    """Return (stored raw by seq, latest state per (kind, entity))."""
    stored = {}
    state = {}
    for row in conn.execute(
            'SELECT seq, kind, entity, revision, deleted, raw_json, station,'
            ' analyte, obs_at, obs_value, cal_start, cal_end, cal_a, cal_b'
            ' FROM events ORDER BY seq'):
        stored[row[0]] = row[5]
        record = {
            'seq': row[0], 'kind': row[1], 'entity': row[2],
            'revision': row[3], 'deleted': bool(row[4]),
            'station': row[6], 'analyte': row[7],
            'obs_at': row[8], 'obs_value': row[9],
            'cal_start': row[10], 'cal_end': row[11],
            'cal_a': row[12], 'cal_b': row[13],
        }
        state[(record['kind'], record['entity'])] = record
    return stored, state


def _apply_new_event(record, state):
    key = (record['kind'], record['entity'])
    previous = state.get(key)
    if previous is None:
        if record['revision'] != 1:
            raise QualityError('first revision of entity %s must be 1' % record['entity'])
        if record['deleted']:
            raise QualityError('first revision of entity %s must not be deleted'
                               % record['entity'])
    else:
        if record['revision'] != previous['revision'] + 1:
            raise QualityError('revision must increase by exactly one for entity %s'
                               % record['entity'])
        if record['deleted']:
            # A deletion carries no payload identity, but the pair is fixed by
            # the entity's first payload and is inherited for later restores.
            record['station'] = previous['station']
            record['analyte'] = previous['analyte']
        elif (record['station'], record['analyte']) != (previous['station'],
                                                        previous['analyte']):
            raise QualityError('station/analyte of entity %s may not change'
                               % record['entity'])
    state[key] = record


def _effective_facts(state):
    observations = {}
    calibrations = {}
    for record in state.values():
        if record['deleted']:
            continue
        pair = (record['station'], record['analyte'])
        if record['kind'] == 'observation':
            observations.setdefault(pair, []).append(
                (record['obs_at'], parse_rational_text(record['obs_value'])))
        else:
            calibrations.setdefault(pair, []).append((
                record['cal_start'], record['cal_end'],
                parse_rational_text(record['cal_a']),
                parse_rational_text(record['cal_b'])))
    return observations, calibrations


def _check_final_state(observations, calibrations):
    """Cross-entity conflicts are judged on the batch's final state only."""
    for pair, points in observations.items():
        points.sort(key=lambda item: item[0])
        for earlier, later in zip(points, points[1:]):
            if earlier[0] == later[0]:
                raise QualityError(
                    'two effective observations share an instant for %s/%s' % pair)
    for pair, intervals in calibrations.items():
        intervals.sort(key=lambda item: item[0])
        for earlier, later in zip(intervals, intervals[1:]):
            if later[0] < earlier[1]:
                raise QualityError(
                    'effective calibration intervals overlap for %s/%s' % pair)


# ---------------------------------------------------------------------------
# exact materialization

def _interpolated_pieces(points, max_gap):
    """Linear pieces between adjacent observations no farther than max_gap."""
    pieces = []
    for (t1, v1), (t2, v2) in zip(points, points[1:]):
        if t2 - t1 > max_gap:
            continue
        slope = Fraction(v2 - v1, t2 - t1)
        intercept = v1 - slope * t1
        pieces.append((Fraction(t1), Fraction(t2), slope, intercept))
    return pieces


def _clip_with_calibrations(pieces, calibrations):
    """Two-pointer intersection of interpolation spans and calibrations."""
    result = []
    i = j = 0
    while i < len(pieces) and j < len(calibrations):
        lo = max(pieces[i][0], Fraction(calibrations[j][0]))
        hi = min(pieces[i][1], Fraction(calibrations[j][1]))
        if lo < hi:
            a, b = calibrations[j][2], calibrations[j][3]
            result.append((lo, hi, a * pieces[i][2], a * pieces[i][3] + b))
        if pieces[i][1] <= Fraction(calibrations[j][1]):
            i += 1
        else:
            j += 1
    return result


def _intersect_intervals(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if lo < hi:
            result.append((lo, hi))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return result


def _piece_at(pieces, starts, moment):
    index = bisect_right(starts, moment) - 1
    if index < 0 or not pieces[index][0] <= moment < pieces[index][1]:
        raise QualityError('internal coverage gap while materializing')
    return pieces[index]


def _unsafe_span(lo, hi, slope, intercept, threshold):
    """Closure of ``{t in [lo, hi) : slope*t + intercept > threshold}``."""
    constant = intercept - threshold
    if slope == 0:
        return (lo, hi) if constant > 0 else None
    root = -constant / slope
    if slope > 0:
        start, end = max(lo, root), hi
    else:
        start, end = lo, min(hi, root)
    return (start, end) if end > start else None


def _integral(slope, constant, lo, hi):
    """Exact integral of ``slope*t + constant`` over ``[lo, hi)``."""
    return slope * (hi * hi - lo * lo) / 2 + constant * (hi - lo)


def _merge_touching(intervals):
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def _split_at_piece_starts(lo, hi, pieces, starts):
    """Cut ``[lo, hi)`` at every linear piece start strictly inside it.

    The station's known domain is the intersection of all analyte coverage
    lists, so it can span several linear pieces of one analyte (for example
    when a calibration boundary falls inside it).  Splitting keeps every
    unsafe span and every excess integral on exactly one affine piece.
    """
    points = [lo, hi]
    index = bisect_right(starts, lo)
    while index < len(pieces) and starts[index] < hi:
        points.append(starts[index])
        index += 1
    points.sort()
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)
            if points[i + 1] > points[i]]


def materialize(config, observations, calibrations):
    """Derive coverage, excess and alert intervals from effective facts.

    Each analyte is interpolated only between adjacent observations and only
    where a calibration covers the span; the station is known only where every
    configured analyte is known.  Unsafe intervals are closures of the exact
    ``value > threshold`` sets so equal endpoints and simultaneous switches do
    not split an episode.  Excess integrals are exact line integrals of
    ``max(calibrated value - threshold, 0)``.  Alerts are the maximal
    positive-length connected components of the unsafe closure, kept when
    their full-warehouse length reaches ``min_episode_seconds``.
    """
    known_rows = []
    unsafe_rows = []
    excess_rows = []
    alert_rows = []
    for station in sorted(config['stations'], key=lambda item: item['id']):
        station_id = station['id']
        analyte_ids = station['analyte_ids']
        pieces = {}
        covers = []
        for analyte_id in analyte_ids:
            pair = (station_id, analyte_id)
            analyte = station['analytes'][analyte_id]
            interpolated = _interpolated_pieces(
                sorted(observations.get(pair, [])), analyte['max_gap_seconds'])
            calibrated = _clip_with_calibrations(
                interpolated, sorted(calibrations.get(pair, [])))
            pieces[analyte_id] = calibrated
            covers.append([(piece[0], piece[1]) for piece in calibrated])
        known = covers[0] if covers else []
        for other in covers[1:]:
            known = _intersect_intervals(known, other)
        known = _merge_touching(known)
        starts = {analyte_id: [piece[0] for piece in pieces[analyte_id]]
                  for analyte_id in analyte_ids}
        station_unsafe = []
        for lo, hi in known:
            known_rows.append((station_id, lo, hi))
            cuts = [(lo, hi)]
            for analyte_id in analyte_ids:
                cuts = [part
                        for cut_lo, cut_hi in cuts
                        for part in _split_at_piece_starts(
                            cut_lo, cut_hi, pieces[analyte_id],
                            starts[analyte_id])]
            for cut_lo, cut_hi in cuts:
                for analyte_id in analyte_ids:
                    piece = _piece_at(pieces[analyte_id], starts[analyte_id], cut_lo)
                    analyte = station['analytes'][analyte_id]
                    span = _unsafe_span(cut_lo, cut_hi, piece[2], piece[3],
                                        analyte['threshold'])
                    if span is not None:
                        station_unsafe.append(span)
                        excess_rows.append((station_id, analyte_id, span[0], span[1],
                                            piece[2], piece[3] - analyte['threshold']))
        for lo, hi in _merge_touching(station_unsafe):
            unsafe_rows.append((station_id, lo, hi))
            if hi - lo >= station['min_episode_seconds']:
                alert_rows.append((station_id, lo, hi))
    return {
        'known': known_rows,
        'unsafe': unsafe_rows,
        'excess': excess_rows,
        'alerts': alert_rows,
    }


# ---------------------------------------------------------------------------
# ingest

def _decode_changes(path, config):
    objects, _size = read_changes(path)
    pairs = config_pairs(config)
    events = []
    previous_seq = None
    for obj in objects:
        event = validate_event(obj, pairs, config['start'], config['end'])
        if previous_seq is not None and event['seq'] <= previous_seq:
            raise QualityError('change stream seq must be strictly increasing')
        previous_seq = event['seq']
        event['raw'] = canonical(obj)
        events.append(event)
    return events


def _record_from_event(event):
    record = {
        'seq': event['seq'], 'kind': event['kind'], 'entity': event['id'],
        'revision': event['revision'], 'deleted': event['deleted'],
        'station': event['station'], 'analyte': event['analyte'],
        'obs_at': None, 'obs_value': None,
        'cal_start': None, 'cal_end': None, 'cal_a': None, 'cal_b': None,
    }
    if not event['deleted']:
        payload = event['payload']
        if event['kind'] == 'observation':
            record['obs_at'] = payload['at']
            record['obs_value'] = _frac_text(payload['value'])
        else:
            record['cal_start'] = payload['cal_start']
            record['cal_end'] = payload['cal_end']
            record['cal_a'] = _frac_text(payload['a'])
            record['cal_b'] = _frac_text(payload['b'])
    return record


def _insert_event(conn, event, record):
    """Store the event; deletions keep the inherited station/analyte pair."""
    conn.execute(
        'INSERT INTO events (seq, kind, entity, revision, deleted, raw_json,'
        ' station, analyte, obs_at, obs_value, cal_start, cal_end, cal_a, cal_b)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (event['seq'], event['kind'], event['id'], event['revision'],
         1 if event['deleted'] else 0, event['raw'], record['station'],
         record['analyte'], record['obs_at'], record['obs_value'],
         record['cal_start'], record['cal_end'], record['cal_a'],
         record['cal_b']))


def _replace_derived(conn, derived):
    conn.execute('DELETE FROM derived_known')
    conn.execute('DELETE FROM derived_unsafe')
    conn.execute('DELETE FROM derived_excess')
    conn.execute('DELETE FROM derived_alert')
    conn.executemany('INSERT INTO derived_known VALUES (?, ?, ?)',
                     [(s, _frac_text(lo), _frac_text(hi)) for s, lo, hi in derived['known']])
    conn.executemany('INSERT INTO derived_unsafe VALUES (?, ?, ?)',
                     [(s, _frac_text(lo), _frac_text(hi)) for s, lo, hi in derived['unsafe']])
    conn.executemany(
        'INSERT INTO derived_excess VALUES (?, ?, ?, ?, ?, ?)',
        [(s, a, _frac_text(lo), _frac_text(hi), _frac_text(m), _frac_text(c))
         for s, a, lo, hi, m, c in derived['excess']])
    conn.executemany('INSERT INTO derived_alert VALUES (?, ?, ?)',
                     [(s, _frac_text(lo), _frac_text(hi)) for s, lo, hi in derived['alerts']])


def _derived_counts(conn):
    return {
        'known_intervals': conn.execute('SELECT COUNT(*) FROM derived_known').fetchone()[0],
        'unsafe_intervals': conn.execute('SELECT COUNT(*) FROM derived_unsafe').fetchone()[0],
        'excess_intervals': conn.execute('SELECT COUNT(*) FROM derived_excess').fetchone()[0],
        'alerts': conn.execute('SELECT COUNT(*) FROM derived_alert').fetchone()[0],
    }


def ingest_changes(source_path, db_path):
    """Apply one change file atomically; matching committed seqs replay."""
    if Path(source_path).resolve() == Path(db_path).resolve():
        raise QualityError('input and database must differ')
    conn = _open_warehouse(db_path)
    try:
        config = _config_from_meta(_meta(conn))
        events = _decode_changes(source_path, config)
        conn.execute('BEGIN IMMEDIATE')
        try:
            meta = _meta(conn)
            last_seq = int(meta['last_seq'])
            stored, state = _load_facts(conn)
            replayed = 0
            new_events = []
            for event in events:
                if event['seq'] <= last_seq:
                    if stored.get(event['seq']) != event['raw']:
                        raise QualityError(
                            'replayed seq has different content: %d' % event['seq'])
                    replayed += 1
                else:
                    new_events.append(event)
            if new_events:
                if new_events[0]['seq'] != last_seq + 1:
                    raise QualityError('new seq must continue at %d' % (last_seq + 1))
                for earlier, later in zip(new_events, new_events[1:]):
                    if later['seq'] != earlier['seq'] + 1:
                        raise QualityError('new seq values must be contiguous')
                if len(stored) + len(new_events) > MAX_EVENTS:
                    raise QualityError(
                        'warehouse would exceed %d events' % MAX_EVENTS)
                for event in new_events:
                    record = _record_from_event(event)
                    _apply_new_event(record, state)
                    _insert_event(conn, event, record)
                observations, calibrations = _effective_facts(state)
                _check_final_state(observations, calibrations)
                derived = materialize(config, observations, calibrations)
                _replace_derived(conn, derived)
                final_seq = new_events[-1]['seq']
                total = len(stored) + len(new_events)
                conn.execute("UPDATE meta SET v = ? WHERE k = 'last_seq'",
                             (str(final_seq),))
                conn.execute("UPDATE meta SET v = ? WHERE k = 'event_count'",
                             (str(total),))
            else:
                final_seq = last_seq
                total = len(stored)
            counts = _derived_counts(conn)
            conn.execute('COMMIT')
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()
    return {
        'schema_version': 1,
        'kind': 'quality-ingest',
        'seq': final_seq,
        'applied': len(new_events),
        'replayed': replayed,
        'events': total,
        **counts,
    }


# ---------------------------------------------------------------------------
# report

def _read_snapshot(db_path):
    conn = _open_warehouse(db_path)
    try:
        conn.execute('BEGIN')
        try:
            meta = _meta(conn)
            rows = {
                'known': conn.execute(
                    'SELECT station, lo, hi FROM derived_known'
                    ' ORDER BY station, lo, hi').fetchall(),
                'unsafe': conn.execute(
                    'SELECT station, lo, hi FROM derived_unsafe'
                    ' ORDER BY station, lo, hi').fetchall(),
                'excess': conn.execute(
                    'SELECT station, analyte, lo, hi, m, c FROM derived_excess'
                    ' ORDER BY station, analyte, lo, hi').fetchall(),
                'alerts': conn.execute(
                    'SELECT station, lo, hi FROM derived_alert'
                    ' ORDER BY station, lo, hi').fetchall(),
            }
            conn.execute('COMMIT')
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()
    return meta, rows


def _day_rows(query_start, query_end, known, unsafe, excess, analyte_ids):
    base = (query_start // DAY) * DAY
    rows = []
    cursor = base
    while cursor < query_end:
        lo = max(cursor, query_start)
        hi = min(cursor + DAY, query_end)
        rows.append({
            'date': datetime.fromtimestamp(cursor, timezone.utc).strftime('%Y-%m-%d'),
            'lo': Fraction(lo), 'hi': Fraction(hi),
            'known': Fraction(0), 'unsafe': Fraction(0),
            'excess': {analyte_id: Fraction(0) for analyte_id in analyte_ids},
        })
        cursor += DAY
    if not rows:
        return rows
    boundaries = [Fraction(row['lo']) for row in rows]

    def spans(item_lo, item_hi):
        clipped_lo = max(item_lo, Fraction(query_start))
        clipped_hi = min(item_hi, Fraction(query_end))
        if clipped_hi <= clipped_lo:
            return None
        first = max(0, bisect_right(boundaries, clipped_lo) - 1)
        last = bisect_right(boundaries, clipped_hi) - 1
        if last > 0 and boundaries[last] == clipped_hi:
            last -= 1
        return first, min(len(rows) - 1, last)

    for interval_lo, interval_hi in known:
        found = spans(interval_lo, interval_hi)
        if not found:
            continue
        for index in range(found[0], found[1] + 1):
            row = rows[index]
            lo = max(interval_lo, row['lo'])
            hi = min(interval_hi, row['hi'])
            if hi > lo:
                row['known'] += hi - lo
    for interval_lo, interval_hi in unsafe:
        found = spans(interval_lo, interval_hi)
        if not found:
            continue
        for index in range(found[0], found[1] + 1):
            row = rows[index]
            lo = max(interval_lo, row['lo'])
            hi = min(interval_hi, row['hi'])
            if hi > lo:
                row['unsafe'] += hi - lo
    for analyte_id, interval_lo, interval_hi, slope, constant in excess:
        found = spans(interval_lo, interval_hi)
        if not found:
            continue
        for index in range(found[0], found[1] + 1):
            row = rows[index]
            lo = max(interval_lo, row['lo'])
            hi = min(interval_hi, row['hi'])
            if hi > lo:
                row['excess'][analyte_id] += _integral(slope, constant, lo, hi)
    return rows


def build_report(db_path, query_start, query_end):
    """Read one committed snapshot and render the exact report."""
    if type(query_start) is not int or type(query_end) is not int:
        raise QualityError('query bounds must be integer Unix seconds')
    meta, rows = _read_snapshot(db_path)
    warehouse_start = int(meta['start'])
    warehouse_end = int(meta['end'])
    if not warehouse_start <= query_start < query_end <= warehouse_end:
        raise QualityError(
            'query must be a positive integer-second span inside the warehouse')
    config = _config_from_meta(meta)
    by_station = {station['id']: {'known': [], 'unsafe': [], 'excess': [],
                                  'alerts': []} for station in config['stations']}
    for station_id, lo, hi in rows['known']:
        by_station[station_id]['known'].append(
            (parse_rational_text(lo), parse_rational_text(hi)))
    for station_id, lo, hi in rows['unsafe']:
        by_station[station_id]['unsafe'].append(
            (parse_rational_text(lo), parse_rational_text(hi)))
    for station_id, analyte_id, lo, hi, slope, constant in rows['excess']:
        by_station[station_id]['excess'].append((
            analyte_id, parse_rational_text(lo), parse_rational_text(hi),
            parse_rational_text(slope), parse_rational_text(constant)))
    for station_id, lo, hi in rows['alerts']:
        by_station[station_id]['alerts'].append(
            (parse_rational_text(lo), parse_rational_text(hi)))

    qs, qe = Fraction(query_start), Fraction(query_end)
    report = {
        'schema_version': 1,
        'kind': 'quality-report',
        'seq': int(meta['last_seq']),
        'warehouse': {'start': warehouse_start, 'end': warehouse_end},
        'query': {'start': query_start, 'end': query_end},
        'stations': [],
    }
    for station in sorted(config['stations'], key=lambda item: item['id']):
        data = by_station[station['id']]
        analyte_ids = station['analyte_ids']
        days = _day_rows(query_start, query_end, data['known'], data['unsafe'],
                         data['excess'], analyte_ids)
        day_rows = []
        for row in days:
            day_rows.append({
                'date': row['date'],
                'known_seconds': _frac_json(row['known']),
                'unsafe_seconds': _frac_json(row['unsafe']),
                'excess_integral': {analyte_id: _frac_json(row['excess'][analyte_id])
                                    for analyte_id in analyte_ids},
            })
        alerts = []
        for lo, hi in sorted(data['alerts']):
            clipped_lo = max(lo, qs)
            clipped_hi = min(hi, qe)
            if clipped_hi > clipped_lo:
                alerts.append({
                    'start': _frac_json(lo),
                    'end': _frac_json(hi),
                    'length': _frac_json(hi - lo),
                    'clipped_start': _frac_json(clipped_lo),
                    'clipped_end': _frac_json(clipped_hi),
                    'clipped_length': _frac_json(clipped_hi - clipped_lo),
                })
        report['stations'].append({
            'station': station['id'],
            'min_episode_seconds': station['min_episode_seconds'],
            'days': day_rows,
            'alerts': alerts,
        })
    return report


def publish_report(db_path, query_start, query_end, output_path):
    if Path(output_path).resolve() == Path(db_path).resolve():
        raise QualityError('report output must not overwrite the database')
    report = build_report(db_path, query_start, query_end)
    save(output_path, report)
    return report
