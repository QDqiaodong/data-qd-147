"""Exact feasible bill bounds from cumulative meter registers.

A reading interval is the kWh difference between two consecutive normalized
readings (one modulus wrap at most).  Its difference is conserved exactly once:
the integer kWh are split into UTC dates and, inside each date, rate segments.
Intervals that cross a cycle boundary are never split into independent
per-cycle problems - the same raw difference feeds adjacent cycles through one
joint set of feasible allocations, so tier and time-of-use prices of every
cycle compete for the same shared kWh.

Feasibility of an integer allocation (per meter/customer):

* reading conservation: each interval's segment quantities sum to its exact
  register difference;
* interval power cap: the difference never exceeds
  floor(elapsed_minutes * max_kwh_per_hour / 60);
* daily power cap: each UTC date's total never exceeds 24 * max_kwh_per_hour;
* every touched UTC date must have a complete rate schedule.

Optimization (exact, standard library only, never expanded per kWh):
the only capacity sets are per-interval sets and their per-day subsets, hence a
laminar family, so the feasible flows form an integral polymatroid base
polytope and separable convex costs (non-decreasing progressive tier prices
plus constant time-of-use prices) are optimized by marginal greedy allocation.
For every billed cycle we solve twice over the *same joint feasible polytope*
- once minimizing and once maximizing with that cycle's real prices and every
other cycle priced at zero - yielding the exact minimum and maximum bill the
cycle can take across all joint-feasible allocations.  Ties resolve to the
lexicographically smallest witness vector ordered by UTC date and segment
start minute.
"""
import heapq
from datetime import timedelta

from .billing_model import BillingError, interval_usage, stamp, tier_charge


class Interval:
    def __init__(self, index, start, end, register_from, register_to, modulus):
        self.index = index
        self.start = start
        self.end = end
        self.register_from = register_from
        self.register_to = register_to
        self.wraps = register_to < register_from
        self.usage = interval_usage(register_from, register_to, modulus)
        self.elapsed_minutes = int((end - start).total_seconds() // 60)
        # (date, Segment, covered_minutes) pieces, chronological
        self.parts = []
        self.by_day = {}
        self.day_cap = {}

    def build_parts(self, meter, rates):
        cursor = self.start
        rate_per_hour = meter.max_kwh_per_hour
        day_cap = 24 * rate_per_hour
        while cursor < self.end:
            day = cursor.date()
            schedule = rates[day]  # coverage pre-checked in build_intervals
            day_start = cursor.replace(hour=0, minute=0)
            block_end = min(self.end, day_start + timedelta(days=1))
            covered_minutes = int((block_end - cursor).total_seconds() // 60)
            cursor_minute = cursor.hour * 60 + cursor.minute
            end_minute = cursor_minute + covered_minutes
            self.day_cap[day] = day_cap
            for segment in schedule:
                if segment.end_minute <= cursor_minute:
                    continue
                if segment.start_minute >= end_minute:
                    break
                overlap = min(segment.end_minute, end_minute) - max(
                    segment.start_minute, cursor_minute)
                if overlap <= 0:
                    continue
                part_index = len(self.parts)
                self.parts.append((day, segment, overlap))
                self.by_day.setdefault(day, []).append(part_index)
            cursor = block_end


def _cycle_for_day(day, cycles):
    for cycle in cycles:
        if cycle.start.date() <= day < cycle.end.date():
            return cycle
    return None


def _iter_dates(start, end):
    day = start.date()
    last = (end - timedelta(minutes=1)).date()
    while day <= last:
        yield day
        day += timedelta(days=1)


def build_intervals(meter, readings, cycles, rates):
    """Normalized intervals overlapping the billed cycle window."""
    window_start = min(cycle.start for cycle in cycles)
    window_end = max(cycle.end for cycle in cycles)
    intervals = []
    for index, (earlier, later) in enumerate(zip(readings, readings[1:])):
        if later.at <= window_start or earlier.at >= window_end:
            continue
        interval = Interval(index, earlier.at, later.at,
                            earlier.register, later.register, meter.modulus)
        if interval.elapsed_minutes <= 0:
            raise BillingError('meter %s has two readings at the same instant' % meter.id)
        power_cap = (interval.elapsed_minutes * meter.max_kwh_per_hour) // 60
        if interval.usage > power_cap:
            raise BillingError(
                'interval %s..%s of meter %s exceeds the max-rate capacity'
                % (stamp(interval.start), stamp(interval.end), meter.id))
        for cursor_day in _iter_dates(interval.start, interval.end):
            if cursor_day not in rates:
                raise BillingError(
                    'missing rate schedule for UTC date ' + cursor_day.isoformat())
        interval.build_parts(meter, rates)
        if interval.usage > sum(interval.day_cap.values()):
            raise BillingError(
                'interval %s..%s of meter %s exceeds the daily rate capacity'
                % (stamp(interval.start), stamp(interval.end), meter.id))
        intervals.append(interval)
    return {interval.index: interval for interval in intervals}


def _tier_price(tiers, total):
    price = tiers[0][1]
    for threshold, tier_price in tiers:
        if threshold is None or total < threshold:
            return price
        price = tier_price
    return price


def _next_tier_threshold(tiers, total):
    for threshold, _price in tiers:
        if threshold is not None and threshold > total:
            return threshold
    return None


class _Greedy:
    """Marginal greedy over the joint feasible polymatroid base polytope.

    Only slots owned by ``target_cycle`` carry prices; every other slot (other
    cycles, unbilled dates inside a bracketing interval) has zero marginal, so
    the optimum prices the target cycle against the whole joint feasible set.
    """

    def __init__(self, intervals, cycles, target_cycle, maximize):
        self.interval_map = intervals
        self.target = target_cycle
        self.maximize = maximize
        self.remaining = {interval.index: interval.usage
                          for interval in intervals.values()}
        self.day_used = {}
        self.slots = []
        self.slot_cycle = {}
        self.slot_tou = {}
        self.slot_day = {}
        self.slot_interval = {}
        for interval in intervals.values():
            for day, segment, _minutes in interval.parts:
                key = (interval.index, day.isoformat(), segment.start_minute)
                self.slots.append(key)
                self.slot_day[key] = day
                self.slot_interval[key] = interval.index
                self.slot_tou[key] = segment.cents_per_kwh
                self.slot_cycle[key] = _cycle_for_day(day, cycles)
                self.day_used[(interval.index, day)] = 0
        self.slots.sort(key=lambda key: (key[1], key[2], self.slot_interval[key]))
        self.position = {key: pos for pos, key in enumerate(self.slots)}
        self.cycle_totals = {cycle.id: 0 for cycle in cycles}
        self.values = {key: 0 for key in self.slots}
        self.heap = []
        for key in self.slots:
            heapq.heappush(self.heap, (self._heap_key(key), key))

    def _marginal(self, key):
        owner = self.slot_cycle[key]
        if owner is not None and owner.id == self.target.id:
            return self.slot_tou[key] + _tier_price(
                owner.tiers, self.cycle_totals[owner.id])
        return 0

    def _heap_key(self, key):
        marginal = self._marginal(key)
        # equal prices: the later witness position fills first, which makes
        # the final witness vector lexicographically smallest
        tie = -self.position[key]
        return (-marginal, tie) if self.maximize else (marginal, tie)

    def _active(self, key):
        interval_index = self.slot_interval[key]
        if self.remaining[interval_index] <= 0:
            return False
        day_key = (interval_index, self.slot_day[key])
        return self.day_used[day_key] < self.interval_map[interval_index].day_cap[
            self.slot_day[key]]

    def _rebuild(self):
        self.heap = []
        for key in self.slots:
            if self._active(key):
                heapq.heappush(self.heap, (self._heap_key(key), key))

    def solve(self):
        total_usage = sum(self.remaining.values())
        allocated = 0
        while allocated < total_usage:
            key = None
            while self.heap:
                stored, candidate = heapq.heappop(self.heap)
                if self._active(candidate) and stored == self._heap_key(candidate):
                    key = candidate
                    break
            if key is None:
                raise BillingError('no feasible integer allocation exists for cycle '
                                   + self.target.id)
            interval_index = self.slot_interval[key]
            day = self.slot_day[key]
            day_key = (interval_index, day)
            push = min(self.remaining[interval_index],
                       self.interval_map[interval_index].day_cap[day]
                       - self.day_used[day_key])
            owner = self.slot_cycle[key]
            if owner is not None and owner.id == self.target.id:
                # never stride over a tier boundary in one batch: marginals
                # stay constant inside the batch, preserving greedy ordering
                threshold = _next_tier_threshold(
                    self.target.tiers, self.cycle_totals[self.target.id])
                if threshold is not None:
                    push = min(push, threshold - self.cycle_totals[self.target.id])
            if push <= 0:
                continue
            self.values[key] += push
            self.remaining[interval_index] -= push
            self.day_used[day_key] += push
            allocated += push
            if owner is not None:
                self.cycle_totals[owner.id] += push
            if self._active(key):
                heapq.heappush(self.heap, (self._heap_key(key), key))
            if owner is not None and owner.id == self.target.id:
                threshold = _next_tier_threshold(
                    self.target.tiers,
                    self.cycle_totals[self.target.id] - 1)
                if threshold is not None and threshold == self.cycle_totals[self.target.id]:
                    self._rebuild()
        return self.values


def _ordered(intervals):
    if isinstance(intervals, dict):
        return [intervals[index] for index in sorted(intervals)]
    return intervals


def bill_cycle(cycle, values, intervals):
    """Recompute tier + time-of-use cents from a witness allocation."""
    tou_cents = 0
    usage = 0
    for interval in _ordered(intervals):
        for day, segment, _minutes in interval.parts:
            key = (interval.index, day.isoformat(), segment.start_minute)
            amount = values[key]
            if cycle.start.date() <= day < cycle.end.date():
                usage += amount
                tou_cents += amount * segment.cents_per_kwh
    tier_cents = tier_charge(cycle.tiers, usage)
    return usage, tier_cents, tou_cents, tier_cents + tou_cents


def _witness_intervals(intervals, values):
    out = []
    for interval in _ordered(intervals):
        days = []
        for day, cap in sorted(interval.day_cap.items()):
            segments = []
            day_total = 0
            for part_index in interval.by_day[day]:
                part_day, segment, _minutes = interval.parts[part_index]
                key = (interval.index, day.isoformat(), segment.start_minute)
                amount = values[key]
                day_total += amount
                segments.append({
                    'start_minute': segment.start_minute,
                    'end_minute': segment.end_minute,
                    'cents_per_kwh': segment.cents_per_kwh,
                    'kwh': amount,
                })
            days.append({
                'date': day.isoformat(),
                'day_cap_kwh': cap,
                'total_kwh': day_total,
                'segments': segments,
            })
        out.append({
            'index': interval.index,
            'start': stamp(interval.start),
            'end': stamp(interval.end),
            'register_start': interval.register_from,
            'register_end': interval.register_to,
            'modulus_wraps': interval.wraps,
            'elapsed_minutes': interval.elapsed_minutes,
            'usage_kwh': interval.usage,
            'days': days,
        })
    return out


def audit_customer(meter, readings, cycles, rates):
    """Build min/max bills and witnesses for one customer's cycles."""
    cycles = sorted(cycles, key=lambda cycle: cycle.start)
    bracketed = []
    for cycle in cycles:
        has_start = any(reading.at <= cycle.start for reading in readings)
        has_end = any(reading.at >= cycle.end for reading in readings)
        if has_start and has_end:
            bracketed.append(cycle)
    result_cycles = [{
        'id': cycle.id,
        'start': stamp(cycle.start),
        'end': stamp(cycle.end),
        'tiers': [{'threshold': threshold, 'price': price}
                  for threshold, price in cycle.tiers],
        'billed': False,
    } for cycle in cycles if cycle not in bracketed]
    if not bracketed:
        return result_cycles, []
    interval_map = build_intervals(meter, readings, bracketed, rates)
    if not interval_map:
        raise BillingError('billed cycles of meter %s have no reading interval' % meter.id)
    intervals = _ordered(interval_map)
    for cycle in bracketed:
        entry = {
            'id': cycle.id,
            'start': stamp(cycle.start),
            'end': stamp(cycle.end),
            'tiers': [{'threshold': threshold, 'price': price}
                      for threshold, price in cycle.tiers],
            'billed': True,
        }
        for label, maximize in (('minimum', False), ('maximum', True)):
            values = _Greedy(interval_map, bracketed, cycle, maximize).solve()
            _assert_feasible(interval_map, values)
            usage, tier_cents, tou_cents, total = bill_cycle(cycle, values, interval_map)
            entry[label] = {
                'usage_kwh': usage,
                'tier_cents': tier_cents,
                'time_of_use_cents': tou_cents,
                'total_cents': total,
                'intervals': _witness_intervals(interval_map, values),
            }
        result_cycles.append(entry)
    result_cycles.sort(key=lambda entry: entry['start'])
    return result_cycles, intervals


def _assert_feasible(interval_map, values):
    """Defensive exact check of reading conservation and daily caps."""
    for interval in _ordered(interval_map):
        part_sum = sum(
            values[(interval.index, day.isoformat(), segment.start_minute)]
            for day, segment, _ in interval.parts)
        if part_sum != interval.usage:
            raise BillingError('internal conservation failure')
        for day, cap in interval.day_cap.items():
            day_sum = sum(
                values[(interval.index, day.isoformat(), segment.start_minute)]
                for part_index in interval.by_day[day]
                for _d, segment, _m in [interval.parts[part_index]])
            if day_sum > cap:
                raise BillingError('internal daily cap failure')
