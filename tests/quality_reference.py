"""Independent exact reference for the quality warehouse acceptance tests.

This module deliberately re-derives coverage, excess and alert intervals with
a different decomposition than ``quality_warehouse``: it cuts each known run at
every structural breakpoint, finds threshold roots by endpoint interpolation,
buckets time by integer floor division, and integrates the positive part of a
linear function with its antiderivative.  All values are exact ``Fraction``
objects; nothing is sampled, rounded or floated.
"""
from datetime import datetime, timezone
from fractions import Fraction

DAY = 86400


def parse_config(doc):
    stations = []
    for raw in doc['stations']:
        analytes = {}
        for item in raw['analytes']:
            analytes[item['id']] = {
                'threshold': Fraction(*item['threshold']),
                'max_gap': item['max_gap_seconds'],
            }
        stations.append({'id': raw['id'],
                         'min_episode': raw['min_episode_seconds'],
                         'analytes': analytes,
                         'order': sorted(analytes)})
    return {'start': doc['start'], 'end': doc['end'], 'stations': stations}


def effective_facts(objects):
    """Highest-revision state per entity, mapped to effective observations."""
    state = {}
    for obj in objects:
        key = (obj['kind'], obj['id'])
        previous = state.get(key)
        if previous is None or obj['revision'] > previous['revision']:
            state[key] = obj
    observations = {}
    calibrations = {}
    for (kind, _entity), obj in state.items():
        if obj['deleted']:
            continue
        payload = obj['payload']
        pair = (payload['station'], payload['analyte'])
        if kind == 'observation':
            observations.setdefault(pair, []).append(
                (Fraction(payload['at']), Fraction(*payload['value'])))
        else:
            calibrations.setdefault(pair, []).append((
                Fraction(payload['start']), Fraction(payload['end']),
                Fraction(*payload['a']), Fraction(*payload['b'])))
    return observations, calibrations


def _cut(points):
    points = sorted(set(points))
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)
            if points[i] < points[i + 1]]


def _merge(intervals):
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _intersect(left, right):
    result = []
    cuts = _cut([x for interval in left + right for x in interval])
    for lo, hi in cuts:
        mid = (lo + hi) / 2
        if (any(a <= mid < b for a, b in left)
                and any(a <= mid < b for a, b in right)):
            result.append((lo, hi))
    return _merge(result)


def _affine(pieces, moment):
    for lo, hi, slope, intercept in pieces:
        if lo <= moment < hi:
            return slope, intercept
    raise AssertionError('no affine piece covers %s' % (moment,))


def _positive_part(slope, intercept, lo, hi):
    """Exact integral of ``max(slope*t + intercept, 0)`` over ``[lo, hi)``."""
    def antiderivative(t):
        return slope * t * t / 2 + intercept * t
    v_lo = slope * lo + intercept
    v_hi = slope * hi + intercept
    if v_lo <= 0 and v_hi <= 0:
        return Fraction(0)
    if v_lo >= 0 and v_hi >= 0:
        return antiderivative(hi) - antiderivative(lo)
    root = lo + (0 - v_lo) * (hi - lo) / (v_hi - v_lo)
    if v_lo < 0:
        return antiderivative(hi) - antiderivative(root)
    return antiderivative(root) - antiderivative(lo)


def _positive_span(slope, constant, lo, hi):
    v_lo = slope * lo + constant
    v_hi = slope * hi + constant
    if v_lo > 0 and v_hi > 0:
        return lo, hi
    if v_lo <= 0 and v_hi <= 0:
        return None
    root = _root(slope, constant, lo, hi)
    return (root, hi) if v_lo <= 0 else (lo, root)


def _root(slope, constant, lo, hi):
    v_lo, v_hi = slope * lo + constant, slope * hi + constant
    return lo + (0 - v_lo) * (hi - lo) / (v_hi - v_lo)


def _episodes(intervals):
    """Maximal positive-length connected components of an interval union."""
    return _merge(intervals)


def analyse(config, observations, calibrations):
    """Return per-station coverage, excess segments and alerts (warehouse-wide)."""
    stations = []
    for station in config['stations']:
        sid = station['id']
        ids = station['order']
        pieces = {}
        for aid in ids:
            pair = (sid, aid)
            points = sorted(observations.get(pair, []))
            spans = []
            for (t1, v1), (t2, v2) in zip(points, points[1:]):
                if t2 - t1 > station['analytes'][aid]['max_gap']:
                    continue
                slope = (v2 - v1) / (t2 - t1)
                spans.append((t1, t2, slope, v1 - slope * t1))
            calibrated = []
            for lo, hi, slope, intercept in spans:
                for cs, ce, a, b in sorted(calibrations.get(pair, [])):
                    cut_lo, cut_hi = max(lo, cs), min(hi, ce)
                    if cut_lo < cut_hi:
                        calibrated.append((cut_lo, cut_hi,
                                           a * slope, a * intercept + b))
            pieces[aid] = sorted(calibrated)
        coverage = [(p[0], p[1]) for p in pieces[ids[0]]]
        for aid in ids[1:]:
            coverage = _intersect(coverage, [(p[0], p[1]) for p in pieces[aid]])
        coverage = _merge(coverage)
        breaks = {aid: [p[0] for p in pieces[aid]] for aid in ids}
        excess = []
        unsafe = []
        for lo, hi in coverage:
            inner = [lo, hi]
            for aid in ids:
                inner += [b for b in breaks[aid] if lo < b < hi]
            for seg_lo, seg_hi in _cut(inner):
                for aid in ids:
                    slope, intercept = _affine(pieces[aid], seg_lo)
                    constant = intercept - station['analytes'][aid]['threshold']
                    span = _positive_span(slope, constant, seg_lo, seg_hi)
                    if span is not None:
                        unsafe.append(span)
                        excess.append((aid, span[0], span[1], slope, constant))
        alerts = []
        for lo, hi in _episodes(unsafe):
            if hi - lo >= station['min_episode']:
                alerts.append((lo, hi))
        stations.append({
            'station': sid,
            'order': ids,
            'thresholds': {aid: station['analytes'][aid]['threshold'] for aid in ids},
            'min_episode': station['min_episode'],
            'coverage': coverage,
            'excess': excess,
            'unsafe': _episodes(unsafe),
            'alerts': alerts,
        })
    return stations


def report(config, observations, calibrations, query_start, query_end):
    """Independent recomputation of the ``report`` payload for comparison."""
    qs, qe = Fraction(query_start), Fraction(query_end)
    stations = analyse(config, observations, calibrations)
    base = (query_start // DAY) * DAY
    result = {'seq': None,
              'warehouse': {'start': config['start'], 'end': config['end']},
              'query': {'start': query_start, 'end': query_end},
              'stations': []}
    for station in stations:
        day_rows = []
        cursor = base
        while cursor < query_end:
            day_lo = max(Fraction(cursor), qs)
            day_hi = min(Fraction(cursor + DAY), qe)
            known = Fraction(0)
            for lo, hi in station['coverage']:
                known += max(Fraction(0), min(hi, day_hi) - max(lo, day_lo))
            unsafe = Fraction(0)
            for lo, hi in station['unsafe']:
                unsafe += max(Fraction(0), min(hi, day_hi) - max(lo, day_lo))
            areas = {}
            for aid in station['order']:
                area = Fraction(0)
                for item_aid, lo, hi, slope, constant in station['excess']:
                    if item_aid != aid:
                        continue
                    lo2, hi2 = max(lo, day_lo), min(hi, day_hi)
                    if hi2 > lo2:
                        area += _positive_part(slope, constant, lo2, hi2)
                areas[aid] = (area.numerator, area.denominator)
            day_rows.append({
                'date': datetime.fromtimestamp(cursor, timezone.utc).strftime('%Y-%m-%d'),
                'known_seconds': (known.numerator, known.denominator),
                'unsafe_seconds': (unsafe.numerator, unsafe.denominator),
                'excess_integral': areas,
            })
            cursor += DAY
        alerts = []
        for lo, hi in station['alerts']:
            clipped_lo, clipped_hi = max(lo, qs), min(hi, qe)
            if clipped_hi > clipped_lo:
                alerts.append({
                    'start': (lo.numerator, lo.denominator),
                    'end': (hi.numerator, hi.denominator),
                    'length': ((hi - lo).numerator, (hi - lo).denominator),
                    'clipped_start': (clipped_lo.numerator, clipped_lo.denominator),
                    'clipped_end': (clipped_hi.numerator, clipped_hi.denominator),
                    'clipped_length': ((clipped_hi - clipped_lo).numerator,
                                       (clipped_hi - clipped_lo).denominator),
                })
        result['stations'].append({
            'station': station['station'],
            'min_episode_seconds': station['min_episode'],
            'days': day_rows,
            'alerts': alerts,
        })
    return normalise(result)


def normalise(value):
    """Convert tuples to lists so a reference result compares to decoded JSON."""
    if isinstance(value, dict):
        return {key: normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalise(item) for item in value]
    return value
