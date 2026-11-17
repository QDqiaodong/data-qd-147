"""Strict configuration and change-stream contracts for the quality warehouse.

The warehouse has a single frozen configuration: a UTC-midnight window of at
most 31 days and at most 30 stations with one to five analytes each.  Change
files are UTF-8 JSONL documents; every rational is an exact ``[n, d]`` pair and
no float, boolean or extra field is accepted.
"""
from fractions import Fraction

from .common import ident, require, rows

DAY = 86400
MAX_WAREHOUSE_END = 4102444800          # 2100-01-01T00:00:00Z
MAX_SPAN = 31 * DAY
MAX_STATIONS = 30
MAX_ANALYTES = 5
MAX_SECONDS = 10 ** 6
MAX_RATIONAL_NUMERATOR = 10 ** 9
MAX_DENOMINATOR = 10 ** 6
MAX_EVENTS = 50000
MAX_CHANGES = 10000
MAX_CHANGES_BYTES = 8_000_000
KINDS = ('observation', 'calibration')


class QualityError(ValueError):
    """Contract violation; the CLI maps it to exit status 2."""


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise QualityError('%s must be an integer in [%d, %d]' % (label, low, high))
    return value


def positive_integer(value, label):
    return _integer(value, 1, 2 ** 63 - 1, label)


def rational(value, label, nonnegative=False, positive=False):
    """Parse exactly ``[n, d]``; reject booleans, floats and other containers."""
    if not isinstance(value, list) or len(value) != 2:
        raise QualityError('%s must be a rational [n, d] pair' % label)
    numerator, denominator = value
    if type(numerator) is not int or type(denominator) is not int:
        raise QualityError('%s must use integer numerator and denominator' % label)
    if abs(numerator) > MAX_RATIONAL_NUMERATOR:
        raise QualityError('%s numerator is outside +/-10^9' % label)
    if not 1 <= denominator <= MAX_DENOMINATOR:
        raise QualityError('%s denominator must be in [1, 10^6]' % label)
    result = Fraction(numerator, denominator)
    if nonnegative and result < 0:
        raise QualityError('%s must not be negative' % label)
    if positive and result <= 0:
        raise QualityError('%s must be positive' % label)
    return result


def parse_rational_text(value):
    """Inverse of the internal ``n/d`` text form; exact, never floating."""
    return Fraction(value)


def validate_config(data):
    """Validate the exact ``{start,end,stations}`` configuration document."""
    require(data, 'start end stations')
    start = _integer(data['start'], 0, MAX_WAREHOUSE_END - 1, 'start')
    end = _integer(data['end'], 1, MAX_WAREHOUSE_END, 'end')
    if start % DAY or end % DAY:
        raise QualityError('start and end must be UTC midnight Unix seconds')
    if not start < end:
        raise QualityError('start must be before end')
    if end - start > MAX_SPAN:
        raise QualityError('warehouse window may span at most 31 days')
    station_rows = rows(data['stations'], MAX_STATIONS)
    stations = []
    station_ids = set()
    for entry in station_rows:
        require(entry, 'id min_episode_seconds analytes')
        station_id = ident(entry['id'])
        if station_id in station_ids:
            raise QualityError('duplicate station id: ' + station_id)
        station_ids.add(station_id)
        min_episode = _integer(entry['min_episode_seconds'], 1, MAX_SECONDS,
                               'min_episode_seconds')
        analytes_raw = rows(entry['analytes'], MAX_ANALYTES)
        if not 1 <= len(analytes_raw) <= MAX_ANALYTES:
            raise QualityError('each station needs 1..%d analytes' % MAX_ANALYTES)
        analytes = {}
        for analyte_entry in analytes_raw:
            require(analyte_entry, 'id threshold max_gap_seconds')
            analyte_id = ident(analyte_entry['id'])
            if analyte_id in analytes:
                raise QualityError('duplicate analyte id on station %s: %s'
                                   % (station_id, analyte_id))
            threshold = rational(analyte_entry['threshold'], 'threshold',
                                 nonnegative=True)
            max_gap = _integer(analyte_entry['max_gap_seconds'], 1, MAX_SECONDS,
                               'max_gap_seconds')
            analytes[analyte_id] = {
                'id': analyte_id,
                'threshold': threshold,
                'max_gap_seconds': max_gap,
            }
        stations.append({
            'id': station_id,
            'min_episode_seconds': min_episode,
            'analytes': analytes,
            'analyte_ids': tuple(sorted(analytes)),
        })
    return {'start': start, 'end': end, 'stations': stations}


def config_pairs(config):
    return {(station['id'], analyte_id)
            for station in config['stations']
            for analyte_id in station['analyte_ids']}


def _payload_identity(payload, pairs, start, end, kind):
    if kind == 'observation':
        require(payload, 'station analyte at value')
        station = ident(payload['station'])
        analyte = ident(payload['analyte'])
        _require_pair(station, analyte, pairs)
        at = _integer(payload['at'], start, end, 'observation at')
        value = rational(payload['value'], 'observation value', nonnegative=True)
        return station, analyte, {
            'at': at,
            'value': value,
        }
    require(payload, 'station analyte start end a b')
    station = ident(payload['station'])
    analyte = ident(payload['analyte'])
    _require_pair(station, analyte, pairs)
    calibr_start = _integer(payload['start'], start, end - 1, 'calibration start')
    calibr_end = _integer(payload['end'], start + 1, end, 'calibration end')
    if not calibr_start < calibr_end:
        raise QualityError('calibration must have positive length')
    a = rational(payload['a'], 'calibration a', positive=True)
    b = rational(payload['b'], 'calibration b')
    return station, analyte, {
        'cal_start': calibr_start,
        'cal_end': calibr_end,
        'a': a,
        'b': b,
    }


def _require_pair(station, analyte, pairs):
    if (station, analyte) not in pairs:
        raise QualityError('unknown station/analyte pair: %s/%s' % (station, analyte))


def validate_event(obj, pairs, start, end):
    """Validate one decoded JSONL event and return its normalized form."""
    require(obj, 'seq kind id revision deleted payload')
    seq = positive_integer(obj['seq'], 'seq')
    revision = positive_integer(obj['revision'], 'revision')
    kind = obj['kind']
    if kind not in KINDS:
        raise QualityError('kind must be observation or calibration')
    entity_id = ident(obj['id'])
    deleted = obj['deleted']
    if type(deleted) is not bool:
        raise QualityError('deleted must be a JSON boolean')
    if deleted:
        if obj['payload'] is not None:
            raise QualityError('deleted events must have a null payload')
        return {
            'seq': seq, 'kind': kind, 'id': entity_id, 'revision': revision,
            'deleted': True, 'station': None, 'analyte': None, 'payload': None,
        }
    if obj['payload'] is None:
        raise QualityError('non-deleted events need a payload object')
    station, analyte, payload = _payload_identity(
        obj['payload'], pairs, start, end, kind)
    return {
        'seq': seq, 'kind': kind, 'id': entity_id, 'revision': revision,
        'deleted': False, 'station': station, 'analyte': analyte,
        'payload': payload,
    }


def canonical(obj):
    import json
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def read_changes(path):
    """Read a bounded UTF-8 JSONL file with duplicate-key rejection."""
    import json
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_CHANGES_BYTES + 1)
    if len(raw) > MAX_CHANGES_BYTES:
        raise QualityError('change stream exceeds 8 MB')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as error:
        raise QualityError('change stream is not valid UTF-8') from error
    if text == '':
        lines = []
    else:
        lines = text.split('\n')
        if lines and lines[-1] == '':
            lines.pop()
    if len(lines) > MAX_CHANGES:
        raise QualityError('change stream has more than %d lines' % MAX_CHANGES)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QualityError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise QualityError('nonfinite JSON number: ' + value)
    decoded = []
    for number, line in enumerate(lines, 1):
        if line.endswith('\r'):
            line = line[:-1]
        if line.strip() == '':
            raise QualityError('blank JSONL line at line %d' % number)
        try:
            obj = json.loads(line, object_pairs_hook=unique, parse_constant=invalid)
        except QualityError:
            raise
        except (ValueError, TypeError) as error:
            raise QualityError('invalid JSON at line %d: %s' % (number, error)) from error
        if not isinstance(obj, dict):
            raise QualityError('each JSONL line must be an object')
        decoded.append(obj)
    return decoded, len(raw)
