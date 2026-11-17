"""Strict input contract and reading normalization for the billing audit.

The restricted JSON document contains exactly four groups: ``meters``,
``readings``, ``cycles`` and ``rates``.  Every identifier follows the shared
``common.ident`` rule, every quantity is an exact integer, and timestamps are
UTC minute stamps ending in ``Z``.  Normalization never guesses data:

* duplicate readings (same meter, same instant, same register) fold to one;
* the same instant with two different registers is rejected;
* remaining readings are ordered by instant.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from .common import require, ident, rows

MAX_METERS = 1000                 # defensive bound; hard rule: one meter/customer
MAX_READINGS_PER_METER = 4000
MAX_RAW_READINGS = 4_000_000      # defensive; the 8 MB JSON cap binds first
MAX_CYCLES = 3
MAX_RATE_DAYS = 400               # defensive; cycles span at most 93 days
MAX_SEGMENTS_PER_DAY = 8
MAX_AMOUNT = 10 ** 9
MAX_CENTS = 10 ** 9               # defensive bound on integer cent prices
_STAMP_LEN = len('2026-01-01T00:00Z')
_DATE_LEN = len('2026-01-01')


class BillingError(ValueError):
    """Contract or feasibility error; the CLI maps it to exit code 2."""


@dataclass(frozen=True)
class Meter:
    id: str
    customer: str
    modulus: int
    max_kwh_per_hour: int


@dataclass(frozen=True)
class Reading:
    at: datetime
    register: int


@dataclass(frozen=True)
class Cycle:
    id: str
    customer: str
    start: datetime
    end: datetime
    tiers: tuple               # ((threshold or None, cents), ...)


@dataclass(frozen=True)
class Segment:
    start_minute: int
    end_minute: int
    cents_per_kwh: int


def parse_stamp(value):
    """Parse a UTC minute timestamp ``YYYY-MM-DDTHH:MMZ`` exactly."""
    if not isinstance(value, str) or not value.endswith('Z') or len(value) != _STAMP_LEN:
        raise BillingError('expected UTC minute timestamp YYYY-MM-DDTHH:MMZ: ' + repr(value))
    try:
        moment = datetime.strptime(value[:-1], '%Y-%m-%dT%H:%M')
    except ValueError as error:
        raise BillingError('bad UTC minute timestamp: ' + value) from error
    return moment.replace(tzinfo=timezone.utc)


def parse_date(value):
    if not isinstance(value, str) or len(value) != _DATE_LEN:
        raise BillingError('expected UTC date YYYY-MM-DD: ' + repr(value))
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as error:
        raise BillingError('bad UTC date: ' + value) from error


def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%MZ')


def _meter_entries(data):
    meters = {}
    customers = {}
    for entry in rows(data['meters'], MAX_METERS):
        require(entry, 'id customer modulus max_kwh_per_hour')
        meter = Meter(
            id=ident(entry['id']),
            customer=ident(entry['customer']),
            modulus=_integer(entry['modulus'], 1, MAX_AMOUNT, 'modulus'),
            max_kwh_per_hour=_integer(entry['max_kwh_per_hour'], 0, MAX_AMOUNT,
                                      'max_kwh_per_hour'),
        )
        if meter.id in meters:
            raise BillingError('duplicate meter id: ' + meter.id)
        if meter.customer in customers:
            raise BillingError('customer may have at most one meter: ' + meter.customer)
        meters[meter.id] = meter
        customers[meter.customer] = meter.id
    if not meters:
        raise BillingError('at least one meter is required')
    return meters, customers


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise BillingError('%s must be an integer in [%d, %d]' % (label, low, high))
    return value


def _reading_entries(data, meters):
    normalized = {meter_id: {} for meter_id in meters}
    raw_counts = {meter_id: 0 for meter_id in meters}
    for entry in rows(data['readings'], MAX_RAW_READINGS):
        require(entry, 'meter at register')
        meter_id = ident(entry['meter'])
        if meter_id not in meters:
            raise BillingError('reading references unknown meter: ' + meter_id)
        raw_counts[meter_id] += 1
        if raw_counts[meter_id] > MAX_READINGS_PER_METER:
            raise BillingError('meter has more than %d readings: %s'
                               % (MAX_READINGS_PER_METER, meter_id))
        moment = parse_stamp(entry['at'])
        modulus = meters[meter_id].modulus
        register = _integer(entry['register'], 0, modulus - 1, 'register')
        bucket = normalized[meter_id]
        if moment in bucket:
            if bucket[moment] != register:
                raise BillingError(
                    'conflicting registers for meter %s at %s' % (meter_id, entry['at']))
            continue  # exact duplicate folds
        bucket[moment] = register
    return {
        meter_id: [Reading(at=moment, register=bucket[moment])
                   for moment in sorted(bucket)]
        for meter_id, bucket in normalized.items()
    }, raw_counts


def _cycle_entries(data, customers):
    cycles = []
    seen = set()
    for entry in rows(data['cycles'], MAX_CYCLES):
        require(entry, 'id customer start end tiers')
        cycle_id = ident(entry['id'])
        if cycle_id in seen:
            raise BillingError('duplicate cycle id: ' + cycle_id)
        seen.add(cycle_id)
        customer = ident(entry['customer'])
        if customer not in customers:
            raise BillingError('cycle references customer without a meter: ' + customer)
        start = parse_stamp(entry['start'])
        end = parse_stamp(entry['end'])
        if (start.hour, start.minute) != (0, 0) or (end.hour, end.minute) != (0, 0):
            raise BillingError('cycle boundaries must be UTC midnight: ' + cycle_id)
        duration = (end - start).days
        if duration not in (30, 31) or end <= start:
            raise BillingError('cycle must be a 30/31 day half-open span: ' + cycle_id)
        tiers = _tiers(entry['tiers'], cycle_id)
        cycles.append(Cycle(cycle_id, customer, start, end, tiers))
    by_customer = {}
    for cycle in cycles:
        by_customer.setdefault(cycle.customer, []).append(cycle)
    for customer, group in by_customer.items():
        group.sort(key=lambda cycle: cycle.start)
        for earlier, later in zip(group, group[1:]):
            if later.start != earlier.end:
                raise BillingError(
                    'cycles of customer %s must be continuous and non-overlapping' % customer)
    return by_customer


def _tiers(value, cycle_id):
    if not isinstance(value, list) or not value:
        raise BillingError('cycle needs at least one tier: ' + cycle_id)
    tiers = []
    previous_threshold = 0
    previous_price = None
    for pos, entry in enumerate(value):
        require(entry, 'threshold price')
        last = pos == len(value) - 1
        if last:
            if entry['threshold'] is not None:
                raise BillingError('last tier threshold must be null: ' + cycle_id)
            threshold = None
        else:
            if entry['threshold'] is None:
                raise BillingError('only the last tier may have null threshold: ' + cycle_id)
            threshold = _integer(entry['threshold'], 0, MAX_AMOUNT, 'tier threshold')
            if threshold <= previous_threshold:
                raise BillingError('tier thresholds must be strictly ascending: ' + cycle_id)
        price = _integer(entry['price'], 0, MAX_CENTS, 'tier price')
        if previous_price is not None and price < previous_price:
            raise BillingError('tier prices must not decrease: ' + cycle_id)
        if threshold is not None:
            previous_threshold = threshold
        previous_price = price
        tiers.append((threshold, price))
    return tuple(tiers)


def _rate_entries(data):
    rates = {}
    for entry in rows(data['rates'], MAX_RATE_DAYS):
        require(entry, 'date segments')
        day = parse_date(entry['date'])
        if day in rates:
            raise BillingError('duplicate rate date: ' + entry['date'])
        segments = entry['segments']
        if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS_PER_DAY:
            raise BillingError('each UTC date needs 1..%d rate segments' % MAX_SEGMENTS_PER_DAY)
        parsed = []
        for segment in segments:
            require(segment, 'start_minute end_minute cents_per_kwh')
            start = _integer(segment['start_minute'], 0, 1440, 'start_minute')
            end = _integer(segment['end_minute'], 0, 1440, 'end_minute')
            price = _integer(segment['cents_per_kwh'], 0, MAX_CENTS, 'cents_per_kwh')
            if start >= end:
                raise BillingError('rate segment must be a positive half-open minute range')
            parsed.append(Segment(start, end, price))
        parsed.sort(key=lambda segment: segment.start_minute)
        if parsed[0].start_minute != 0 or parsed[-1].end_minute != 1440:
            raise BillingError('rate segments must cover the full 24 hours: ' + entry['date'])
        for earlier, later in zip(parsed, parsed[1:]):
            if earlier.end_minute != later.start_minute:
                raise BillingError('rate segments must be contiguous and non-overlapping: '
                                   + entry['date'])
        rates[day] = tuple(parsed)
    if not rates:
        raise BillingError('at least one rate day is required')
    return rates


def validate_input(data):
    """Validate the restricted document and return normalized structures."""
    require(data, 'meters readings cycles rates')
    meters, customers = _meter_entries(data)
    readings, raw_counts = _reading_entries(data, meters)
    cycles_by_customer = _cycle_entries(data, customers)
    rates = _rate_entries(data)
    return {
        'meters': meters,
        'customers': customers,
        'readings': readings,
        'raw_reading_counts': raw_counts,
        'cycles': cycles_by_customer,
        'rates': rates,
    }


def interval_usage(register_from, register_to, modulus):
    """Register difference with at most one modulus wrap."""
    if register_to >= register_from:
        return register_to - register_from
    return register_to - register_from + modulus


def tier_charge(tiers, total_kwh):
    """Progressive tier charge in exact integer cents."""
    charge = 0
    remaining = total_kwh
    floor = 0
    for threshold, price in tiers:
        width = (threshold - floor) if threshold is not None else remaining
        taken = min(remaining, width)
        charge += taken * price
        remaining -= taken
        if threshold is not None:
            floor = threshold
        if remaining == 0:
            break
    return charge
