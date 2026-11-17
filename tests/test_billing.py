"""Acceptance tests for the standalone time-of-use billing audit.

The exact greedy solver is cross-checked against an independent exhaustive
enumeration of every joint-feasible integer allocation on small instances,
including cross-cycle intervals whose raw difference must be conserved once.
"""
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from water_rights.billing_audit import (
    _Greedy, bill_cycle, build_intervals,
)
from water_rights.billing_model import (
    BillingError, interval_usage, tier_charge, validate_input,
)
from water_rights.billing_pipeline import audit
from water_rights.billing_verify import verify_report
from water_rights.common import read

ROOT = Path(__file__).resolve().parents[1]


def flat_rates(start, count, prices):
    rates = []
    for offset in range(count):
        day = (start + timedelta(days=offset)).isoformat()
        cursor = 0
        segments = []
        for end_minute, price in prices:
            segments.append({'start_minute': cursor, 'end_minute': end_minute,
                             'cents_per_kwh': price})
            cursor = end_minute
        rates.append({'date': day, 'segments': segments})
    return rates


def tiny_doc():
    """Two 30-day cycles; only short intervals carry non-zero usage.

    Reading layout (the middle interval crosses the C1/C2 midnight boundary):
      r0 Jan 1 reg 0 -> r1 Jan 30 reg 0 (zero interval)
      r1 Jan 30 reg 0 -> r2 Feb 1 reg 3 (Jan 30 in C1, Jan 31 in C2)
      r2 Feb 1 reg 3 -> r3 Feb 3 reg 6 (two C2 days)
      r3 Feb 3 reg 6 -> r4 Mar 2 reg 6 (zero interval)
    """
    rates = flat_rates(date(2026, 1, 1), 61,
                       [(720, 5), (1440, 15)])
    return {
        'meters': [
            {'id': 'M', 'customer': 'acme', 'modulus': 10000, 'max_kwh_per_hour': 100},
        ],
        'readings': [
            {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-01-30T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-02-01T00:00Z', 'register': 3},
            {'meter': 'M', 'at': '2026-02-03T00:00Z', 'register': 6},
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 6},
        ],
        'cycles': [
            {'id': 'C1', 'customer': 'acme',
             'start': '2026-01-01T00:00Z', 'end': '2026-01-31T00:00Z',
             'tiers': [{'threshold': 2, 'price': 10}, {'threshold': None, 'price': 12}]},
            {'id': 'C2', 'customer': 'acme',
             'start': '2026-01-31T00:00Z', 'end': '2026-03-02T00:00Z',
             'tiers': [{'threshold': 1, 'price': 20}, {'threshold': None, 'price': 25}]},
        ],
        'rates': rates,
    }


def normalized_structure(doc):
    data = validate_input(doc)
    meter = data['meters']['M']
    cycles = data['cycles']['acme']
    interval_map = build_intervals(meter, data['readings']['M'], cycles, data['rates'])
    intervals = [interval_map[index] for index in sorted(interval_map)]
    return data, meter, cycles, intervals


def brute_force(doc):
    """Enumerate every joint-feasible allocation; return per-cycle min/max.

    Depth-first with per-interval reading conservation pruning: each stage
    branches only over segment splits whose sum equals the interval's exact
    register difference, so no candidate outside the conservation hyperplane
    is materialized and leaf costs are evaluated in place.
    """
    data, meter, cycles, intervals = normalized_structure(doc)
    stage_slots = []
    for interval in intervals:
        stage_slots.append([
            (interval.index, day, segment)
            for day, segment, _ in interval.parts])
    results = {cycle.id: {'min': None, 'max': None,
                          'min_w': None, 'max_w': None} for cycle in cycles}
    values = {}

    def splits(slot_count, total):
        if slot_count == 1:
            yield (total,)
            return
        for head in range(total + 1):
            for tail in splits(slot_count - 1, total - head):
                yield (head,) + tail

    def evaluate():
        for interval in intervals:
            for day, cap in interval.day_cap.items():
                day_sum = sum(
                    values[(interval.index, day.isoformat(), segment.start_minute)]
                    for part in interval.by_day[day]
                    for _d, segment, _m in [interval.parts[part]])
                if day_sum > cap:
                    return
        witness_vector = [values[key] for key in sorted(values)]
        for cycle in cycles:
            _u, _t, _tou, total = bill_cycle(cycle, values, intervals)
            record = results[cycle.id]
            if record['min'] is None or total < record['min'] or (
                    total == record['min'] and witness_vector < record['min_w']):
                record['min'] = total
                record['min_w'] = witness_vector
            if record['max'] is None or total > record['max'] or (
                    total == record['max'] and witness_vector < record['max_w']):
                record['max'] = total
                record['max_w'] = witness_vector

    def search(pos):
        if pos == len(intervals):
            evaluate()
            return
        interval = intervals[pos]
        combos = ([tuple(0 for _ in stage_slots[pos])] if interval.usage == 0
                  else splits(len(stage_slots[pos]), interval.usage))
        for combo in combos:
            for (interval_index, day, segment), amount in zip(
                    stage_slots[pos], combo):
                values[(interval_index, day.isoformat(),
                        segment.start_minute)] = amount
            search(pos + 1)

    search(0)
    return results


class ReadingNormalization(unittest.TestCase):
    def bad(self, doc):
        with self.assertRaises((ValueError, TypeError, BillingError)):
            validate_input(doc)

    def test_duplicate_fold_and_sort(self):
        doc = tiny_doc()
        doc['readings'].append(
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 6})
        doc['readings'] = list(reversed(doc['readings']))  # shuffle order
        report = audit(doc)
        block = report['customers'][0]
        self.assertEqual(block['folded_duplicate_readings'], 1)
        self.assertEqual([row['at'] for row in block['readings']],
                         sorted(row['at'] for row in block['readings']))
        self.assertEqual(len(block['readings']), 5)

    def test_same_instant_conflict_rejected(self):
        doc = tiny_doc()
        doc['readings'].append(
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 8})
        self.bad(doc)

    def test_one_modulus_wrap(self):
        doc = tiny_doc()
        doc['readings'] = [
            {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 9990},
            {'meter': 'M', 'at': '2026-01-02T00:00Z', 'register': 10},
        ]
        data = validate_input(doc)
        meter = data['meters']['M']
        parsed = data['readings']['M']
        self.assertEqual(interval_usage(parsed[0].register, parsed[1].register,
                                        meter.modulus), 20)

    def test_max_rate_violation_rejected(self):
        doc = tiny_doc()
        # 100 kWh in 30 minutes while the meter allows 5 kWh/hour: the
        # interval max-rate cap is floor(30*5/60)=2 kWh.  Midnight readings
        # keep both cycles bracketed so the interval is actually checked.
        doc['meters'][0]['max_kwh_per_hour'] = 5
        doc['readings'] = [
            {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-01-05T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-01-05T00:30Z', 'register': 100},
            {'meter': 'M', 'at': '2026-01-31T00:00Z', 'register': 100},
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 100},
        ]
        with self.assertRaises(BillingError) as context:
            audit(doc)
        self.assertIn('max-rate', str(context.exception))

    def test_missing_bracketing_reading_skips_cycle(self):
        doc = tiny_doc()
        # drop the final reading: C2's end is no longer bracketed, C1 still is
        doc['readings'] = doc['readings'][:-1]
        report = audit(doc)
        by_id = {c['id']: c for c in report['customers'][0]['cycles']}
        self.assertTrue(by_id['C1']['billed'])
        self.assertFalse(by_id['C2']['billed'])
    def test_contract_rejections(self):
        doc = tiny_doc()
        doc['extra'] = 1
        self.bad(doc)
        doc = tiny_doc()
        doc['readings'][0]['at'] = '2026-01-01 00:00'
        self.bad(doc)
        doc = tiny_doc()
        doc['rates'][0]['segments'][0]['end_minute'] = 300
        self.bad(doc)
        doc = tiny_doc()
        doc['meters'].append(
            {'id': 'M2', 'customer': 'acme', 'modulus': 10, 'max_kwh_per_hour': 1})
        self.bad(doc)
        doc = tiny_doc()
        doc['cycles'][0]['tiers'][0]['price'] = 99
        self.bad(doc)
        doc = tiny_doc()
        doc['cycles'][0]['start'] = '2026-01-01T00:01Z'
        self.bad(doc)


class GreedyVersusEnumeration(unittest.TestCase):
    def solver_bounds(self, doc):
        data, meter, cycles, intervals = normalized_structure(doc)
        interval_map = {interval.index: interval for interval in intervals}
        out = {}
        for cycle in cycles:
            record = {}
            for label, maximize in (('min', False), ('max', True)):
                values = _Greedy(interval_map, cycles, cycle, maximize).solve()
                _u, _t, _tou, total = bill_cycle(cycle, values, intervals)
                record[label] = total
                record[label + '_w'] = [values[key] for key in sorted(values)]
            out[cycle.id] = record
        return out

    def test_matches_brute_force(self):
        doc = tiny_doc()
        expected = brute_force(doc)
        actual = self.solver_bounds(doc)
        for cycle_id in expected:
            self.assertEqual(actual[cycle_id]['min'], expected[cycle_id]['min'])
            self.assertEqual(actual[cycle_id]['max'], expected[cycle_id]['max'])
            self.assertEqual(actual[cycle_id]['min_w'], expected[cycle_id]['min_w'])
            self.assertEqual(actual[cycle_id]['max_w'], expected[cycle_id]['max_w'])

    def test_lexicographic_tie_breaking(self):
        # Flat prices everywhere: every allocation costs the same, so the
        # witness must be the unique lexicographically smallest vector.
        doc = tiny_doc()
        for entry in doc['rates']:
            for segment in entry['segments']:
                segment['cents_per_kwh'] = 7
        doc['cycles'][0]['tiers'] = [
            {'threshold': None, 'price': 10}]
        doc['cycles'][1]['tiers'] = [
            {'threshold': None, 'price': 10}]
        expected = brute_force(doc)
        actual = self.solver_bounds(doc)
        for cycle_id in expected:
            self.assertEqual(actual[cycle_id]['min_w'], expected[cycle_id]['min_w'])
            self.assertEqual(actual[cycle_id]['max_w'], expected[cycle_id]['max_w'])

    def test_integer_cent_boundaries_and_zero_usage(self):
        doc = tiny_doc()
        report = audit(doc)
        block = report['customers'][0]
        c1 = next(c for c in block['cycles'] if c['id'] == 'C1')
        verify_report(doc, report)
        # a threshold exactly equal to the cycle total must not jump tiers
        self.assertEqual(tier_charge(((3, 10), (None, 12)), 3), 30)
        self.assertEqual(tier_charge(((3, 10), (None, 12)), 4), 42)
        self.assertEqual(tier_charge(((None, 11),), 0), 0)
        # zero-usage witness intervals exist and are all zeros
        zero_intervals = [
            interval for interval in c1['minimum']['intervals']
            if interval['usage_kwh'] == 0]
        self.assertTrue(zero_intervals)
        for zero_interval in zero_intervals:
            self.assertTrue(all(
                segment['kwh'] == 0
                for day in zero_interval['days'] for segment in day['segments']))


class CrossCycleCoupling(unittest.TestCase):
    def test_shared_difference_conserved_once(self):
        doc = tiny_doc()
        report = audit(doc)
        c1 = next(c for c in report['customers'][0]['cycles'] if c['id'] == 'C1')
        c2 = next(c for c in report['customers'][0]['cycles'] if c['id'] == 'C2')
        data, meter, cycles, intervals = normalized_structure(doc)
        # interval 1 crosses the boundary: its raw difference (3) is shared
        cross = next(i for i in intervals if i.index == 1)
        self.assertEqual(cross.usage, 3)
        for bound in ('minimum', 'maximum'):
            for cycle_entry in (c1, c2):
                values = {}
                for witness in cycle_entry[bound]['intervals']:
                    if witness['index'] != cross.index:
                        continue
                    for day_entry in witness['days']:
                        for segment in day_entry['segments']:
                            values[(cross.index, day_entry['date'],
                                    segment['start_minute'])] = segment['kwh']
                cross_total = sum(values.values())
                # the shared 3 kWh can sit on either side of the boundary,
                # never more than the single raw difference
                self.assertLessEqual(cross_total, cross.usage)
                self.assertGreaterEqual(cross_total, 0)
        verify_report(doc, report)

    def test_independent_split_duplicates_the_difference(self):
        """The wrong approach (per-cycle independent splits) is impossible."""
        doc = tiny_doc()
        data, meter, cycles, intervals = normalized_structure(doc)
        cross = next(i for i in intervals if i.index == 1)
        # A naive implementation gives each cycle its own copy of the full
        # difference.  Two copies violate reading conservation.
        duplicated = 2 * cross.usage
        self.assertGreater(duplicated, cross.usage)
        # The joint witness instead conserves the single difference: build
        # both cycle optima and confirm each honors the shared cap.
        interval_map = {interval.index: interval for interval in intervals}
        for cycle, maximize in ((cycles[0], False), (cycles[1], True)):
            values = _Greedy(interval_map, cycles, cycle, maximize).solve()
            part_sum = sum(
                values[(cross.index, day.isoformat(), segment.start_minute)]
                for day, segment, _ in cross.parts)
            self.assertEqual(
                sum(interval.usage for interval in intervals),
                sum(values.values()))
            self.assertLessEqual(part_sum, cross.usage)

    def test_two_cycles_different_prices(self):
        expected = brute_force(tiny_doc())
        report = audit(tiny_doc())
        for cycle in report['customers'][0]['cycles']:
            self.assertEqual(cycle['minimum']['total_cents'],
                             expected[cycle['id']]['min'])
            self.assertEqual(cycle['maximum']['total_cents'],
                             expected[cycle['id']]['max'])


class Infeasibility(unittest.TestCase):
    def test_daily_cap_makes_allocation_infeasible(self):
        # The interval power cap passes (24 h * 100 kWh/h is generous) but the
        # single UTC day touched by the interval can only hold 24*rate kWh:
        # 100 kWh inside one day with a 2 kWh/h meter cannot be allocated.
        doc = tiny_doc()
        doc['meters'][0]['max_kwh_per_hour'] = 2  # daily cap 48 kWh
        doc['readings'] = [
            {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-01-02T00:00Z', 'register': 100},
            {'meter': 'M', 'at': '2026-01-31T00:00Z', 'register': 100},
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 100},
        ]
        with self.assertRaises(BillingError):
            audit(doc)

    def test_cross_cycle_infeasibility_fails_whole_report(self):
        # The shared boundary interval spans two UTC days whose joint daily
        # caps are below the raw difference, so every joint allocation is
        # infeasible and the whole report must fail (no partial billing).
        doc = tiny_doc()
        doc['meters'][0]['max_kwh_per_hour'] = 1  # daily cap 24 kWh/day
        doc['readings'] = [
            {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-01-30T00:00Z', 'register': 0},
            {'meter': 'M', 'at': '2026-02-01T00:00Z', 'register': 60},
            {'meter': 'M', 'at': '2026-03-02T00:00Z', 'register': 60},
        ]
        with self.assertRaises(BillingError):
            audit(doc)


class RandomizedDifferential(unittest.TestCase):
    def _random_doc(self, rng):
        rates = []
        for offset in range(61):
            day = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
            cut = rng.choice((360, 720, 1080))
            rates.append({'date': day, 'segments': [
                {'start_minute': 0, 'end_minute': cut,
                 'cents_per_kwh': rng.randrange(5)},
                {'start_minute': cut, 'end_minute': 1440,
                 'cents_per_kwh': rng.randrange(5)}]})

        def tiers():
            prices = sorted(rng.sample(range(6), rng.choice((1, 2))))
            if len(prices) == 1:
                return [{'threshold': None, 'price': prices[0]}]
            return [{'threshold': rng.choice((1, 2, 3)), 'price': prices[0]},
                    {'threshold': None, 'price': prices[1]}]

        modulus = rng.choice((20, 100000))
        # keep every interval difference in 0..3 so the exhaustive
        # conservation-split enumeration stays tiny; occasional drops exercise
        # the single modulus wrap rule (a drop of 1 with modulus 20 wraps to
        # modulus-1, which is rejected by the solver as a power-cap breach).
        registers = [rng.randrange(min(modulus, 4))]
        for _ in range(4):
            step = rng.choice((-1, 0, 1, 2, 3))
            registers.append((registers[-1] + step) % modulus)
        # All readings cluster around the C1/C2 boundary so every interval
        # spans at most two UTC days; interval 1 still crosses the boundary.
        return {
            'meters': [{'id': 'M', 'customer': 'acme', 'modulus': modulus,
                        'max_kwh_per_hour': 100}],
            'readings': [
                {'meter': 'M', 'at': '2026-01-30T00:00Z', 'register': registers[0]},
                {'meter': 'M', 'at': '2026-01-31T00:00Z', 'register': registers[1]},
                {'meter': 'M', 'at': '2026-02-01T00:00Z', 'register': registers[2]},
                {'meter': 'M', 'at': '2026-02-02T00:00Z', 'register': registers[3]},
                {'meter': 'M', 'at': '2026-02-03T00:00Z', 'register': registers[4]},
            ],
            'cycles': [
                {'id': 'C1', 'customer': 'acme',
                 'start': '2026-01-01T00:00Z', 'end': '2026-01-31T00:00Z',
                 'tiers': tiers()},
                {'id': 'C2', 'customer': 'acme',
                 'start': '2026-01-31T00:00Z', 'end': '2026-03-02T00:00Z',
                 'tiers': tiers()},
            ],
            'rates': rates,
        }

    def test_greedy_matches_enumeration_on_random_instances(self):
        rng = random.Random(20261117)
        solver = GreedyVersusEnumeration()
        checked = 0
        attempts = 0
        while checked < 8 and attempts < 40:
            attempts += 1
            doc = self._random_doc(rng)
            try:
                expected = brute_force(doc)
            except BillingError:
                # a wrapped register can legitimately exceed the power cap;
                # such documents are rejected upstream, not enumerated
                continue
            actual = solver.solver_bounds(doc)
            report = audit(doc)
            verify_report(doc, report)
            for cycle_id in expected:
                self.assertEqual(actual[cycle_id]['min'], expected[cycle_id]['min'])
                self.assertEqual(actual[cycle_id]['max'], expected[cycle_id]['max'])
                self.assertEqual(actual[cycle_id]['min_w'], expected[cycle_id]['min_w'])
                self.assertEqual(actual[cycle_id]['max_w'], expected[cycle_id]['max_w'])
            checked += 1


class LargeScale(unittest.TestCase):
    def test_billion_scale_is_not_unit_iterated(self):
        big = 10 ** 9
        rates = flat_rates(date(2025, 12, 31), 32,
                           [(360, 3), (1080, 9), (1440, 4)])
        doc = {
            'meters': [
                {'id': 'M', 'customer': 'acme', 'modulus': big,
                 'max_kwh_per_hour': big},
            ],
            'readings': [
                {'meter': 'M', 'at': '2026-01-01T00:00Z', 'register': 0},
                {'meter': 'M', 'at': '2026-01-02T00:00Z', 'register': big // 2},
                {'meter': 'M', 'at': '2026-01-03T00:00Z', 'register': big - 1},
                {'meter': 'M', 'at': '2026-01-31T00:00Z', 'register': big - 1},
            ],
            'cycles': [
                {'id': 'C1', 'customer': 'acme',
                 'start': '2026-01-01T00:00Z', 'end': '2026-01-31T00:00Z',
                 'tiers': [{'threshold': big // 3, 'price': 6},
                           {'threshold': None, 'price': 11}]},
            ],
            'rates': rates,
        }
        report = audit(doc)
        c1 = report['customers'][0]['cycles'][0]
        total = big - 1
        self.assertEqual(c1['minimum']['usage_kwh'], total)
        self.assertEqual(c1['maximum']['usage_kwh'], total)
        self.assertLess(c1['minimum']['total_cents'], c1['maximum']['total_cents'])
        # exact hand computation for the cheapest possible bill
        tier_cost = (big // 3) * 6 + (total - big // 3) * 11
        self.assertEqual(c1['minimum']['total_cents'],
                         tier_cost + total * 3)
        verify_report(doc, report)


class CommandLine(unittest.TestCase):
    def call(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'water_rights.billing_cli',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)

    def test_replay_and_verify(self):
        with tempfile.TemporaryDirectory() as td:
            src = ROOT / 'fixtures' / 'billing_input.json'
            out = Path(td) / 'report.json'
            run = self.call(src, out)
            self.assertEqual(run.returncode, 0, run.stderr)
            first = out.read_bytes()
            self.assertEqual(json.loads(first)['source_sha256'],
                             hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(self.call(src, out).returncode, 0)
            self.assertEqual(first, out.read_bytes())
            data, _ = read(src)
            self.assertTrue(verify_report(data, json.loads(first)))

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'bad.json'
            out = Path(td) / 'out.json'
            out.write_text('prior')
            for body in ('{}', '{"a":1,"a":2}', 'NaN',
                         json.dumps({'meters': [], 'readings': [],
                                     'cycles': [], 'rates': []})):
                src.write_text(body)
                self.assertEqual(self.call(src, out).returncode, 2)
                self.assertEqual(out.read_text(), 'prior')

    def test_input_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            src.write_bytes((ROOT / 'fixtures' / 'billing_input.json').read_bytes())
            before = src.read_bytes()
            self.assertEqual(self.call(src, src).returncode, 2)
            self.assertEqual(src.read_bytes(), before)
            link = Path(td) / 'link.json'
            link.symlink_to(src)
            self.assertEqual(self.call(src, link).returncode, 2)
            self.assertEqual(src.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
