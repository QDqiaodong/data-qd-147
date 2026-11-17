"""Independent recomputation of a billing audit report.

Given the original restricted input and a produced report, every witness is
re-summed from scratch to re-check reading conservation, daily power caps,
cycle tier volumes and the two-part (tier + time-of-use) integer-cent bill.
"""
from datetime import date as _date

from .billing_model import (
    BillingError, interval_usage, parse_stamp, tier_charge, validate_input,
)


def verify_report(data, report):
    """Raise BillingError unless the report is fully recomputable; else True."""
    normalized = validate_input(data)
    if report.get('schema_version') != 1 or report.get('kind') != 'billing-audit':
        raise BillingError('not a billing-audit report')
    meters = normalized['meters']
    for customer_block in report['customers']:
        meter = meters[customer_block['meter']['id']]
        if meter.customer != customer_block['customer']:
            raise BillingError('witness customer/meter mismatch')
        readings = {parse_stamp(row['at']): row['register']
                    for row in customer_block['readings']}
        for cycle_entry in customer_block['cycles']:
            if not cycle_entry.get('billed'):
                continue
            for bound in ('minimum', 'maximum'):
                bound_entry = cycle_entry[bound]
                seen_intervals = set()
                cycle_usage = 0
                tou_cents = 0
                cycle_start = parse_stamp(cycle_entry['start'])
                cycle_end = parse_stamp(cycle_entry['end'])
                for witness in bound_entry['intervals']:
                    if witness['index'] in seen_intervals:
                        raise BillingError('duplicate witness interval')
                    seen_intervals.add(witness['index'])
                    expected_usage = interval_usage(
                        witness['register_start'], witness['register_end'], meter.modulus)
                    if witness['usage_kwh'] != expected_usage:
                        raise BillingError('witness usage disagrees with register difference')
                    start = parse_stamp(witness['start'])
                    end = parse_stamp(witness['end'])
                    if readings.get(start) != witness['register_start'] or \
                            readings.get(end) != witness['register_end']:
                        raise BillingError('witness readings not present in normalized input')
                    interval_total = 0
                    for day_entry in witness['days']:
                        day_total = sum(segment['kwh'] for segment in day_entry['segments'])
                        if day_total != day_entry['total_kwh']:
                            raise BillingError('day segment totals disagree')
                        if day_total > day_entry['day_cap_kwh']:
                            raise BillingError('daily power cap exceeded in witness')
                        interval_total += day_total
                        in_cycle = cycle_start.date().isoformat() <= day_entry['date'] \
                            < cycle_end.date().isoformat()
                        for segment in day_entry['segments']:
                            # segment windows must match the rate schedule
                            schedule = normalized['rates'].get(
                                _date.fromisoformat(day_entry['date']))
                            if schedule is None:
                                raise BillingError(
                                    'rate schedule missing for ' + day_entry['date'])
                            match = [s for s in schedule
                                     if s.start_minute == segment['start_minute']
                                     and s.end_minute == segment['end_minute']]
                            if not match or match[0].cents_per_kwh != segment['cents_per_kwh']:
                                raise BillingError('witness rate segment disagrees with rates')
                            if in_cycle:
                                cycle_usage += segment['kwh']
                                tou_cents += segment['kwh'] * segment['cents_per_kwh']
                    if interval_total != witness['usage_kwh']:
                        raise BillingError('reading conservation broken in witness')
                if cycle_usage != bound_entry['usage_kwh']:
                    raise BillingError('cycle usage disagrees with witness')
                if tou_cents != bound_entry['time_of_use_cents']:
                    raise BillingError('time-of-use charge disagrees with witness')
                tier_cents = tier_charge(
                    tuple((tier['threshold'], tier['price']) for tier in cycle_entry['tiers']),
                    cycle_usage)
                if tier_cents != bound_entry['tier_cents']:
                    raise BillingError('tier charge disagrees with witness')
                if tier_cents + tou_cents != bound_entry['total_cents']:
                    raise BillingError('total charge disagrees with witness')
    return True
