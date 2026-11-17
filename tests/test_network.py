"""Acceptance tests for the exact lexicographic network solver.

The reference is an independent exhaustive enumeration of all integer flows
on tiny networks, compared against the four-level objective directly.
"""
import itertools
import random
import time
import unittest

from water_rights.network_flow import solve_allocation, solve_delivery, largest_remainder


def brute_allocation(nodes, sources, channels, claims):
    """Exhaustively enumerate all feasible integer flows; return the optimum.

    Objective (all compared exactly, lexicographically):
      1. maximize group totals S by priority ascending
      2. minimize per-group |planned - hamilton target| sums, priority ascending
      3. maximize per-claim amounts by claim id ascending
      4. minimize (source withdrawals, channel flows) by id ascending
    Returns (deliveries, withdrawals, flows, group_totals, targets).
    """
    prios = sorted({c[2] for c in claims})
    best = None
    best_key = None
    ch_ranges = [range(c[3] + 1) for c in channels]
    src_ranges = [range(s[2] + 1) for s in sources]
    cl_ranges = [range(c[3] + 1) for c in claims]
    for f_vec in itertools.product(*ch_ranges):
        net = {n: 0 for n in nodes}
        for (eid, fr, to, cap), f in zip(channels, f_vec):
            net[fr] -= f
            net[to] += f
        for y_vec in itertools.product(*src_ranges):
            bal = dict(net)
            for (sid, node, cap), y in zip(sources, y_vec):
                bal[node] += y
            if any(b < 0 for b in bal.values()):
                continue
            for d_vec in itertools.product(*cl_ranges):
                need = dict(bal)
                ok = True
                for (cid, node, prio, req), d in zip(claims, d_vec):
                    need[node] -= d
                    if need[node] < 0:
                        ok = False
                        break
                if not ok or any(v != 0 for v in need.values()):
                    continue
                totals = {p: 0 for p in prios}
                for (cid, node, prio, req), d in zip(claims, d_vec):
                    totals[prio] += d
                targets = {}
                for p in prios:
                    reqs = {cid: req for (cid, _, pp, req) in claims if pp == p}
                    targets.update(largest_remainder(reqs, totals[p]))
                dev = {p: 0 for p in prios}
                for (cid, node, prio, req), d in zip(claims, d_vec):
                    dev[prio] += abs(d - targets[cid])
                key = (tuple(-totals[p] for p in prios),
                       tuple(dev[p] for p in prios),
                       tuple(-d for d in d_vec),
                       tuple(y_vec) + tuple(f_vec))
                if best_key is None or key < best_key:
                    best_key = key
                    best = (d_vec, y_vec, f_vec, totals, targets)
    return best


def brute_delivery(nodes, sources, channels, claim_nodes, amounts):
    """Lexicographically minimal (source, channel) routing for fixed amounts."""
    best = None
    best_key = None
    ch_ranges = [range(c[3] + 1) for c in channels]
    src_ranges = [range(s[2] + 1) for s in sources]
    want = {n: 0 for n in nodes}
    for cid, amount in amounts.items():
        want[claim_nodes[cid]] += amount
    for f_vec in itertools.product(*ch_ranges):
        net = {n: 0 for n in nodes}
        for (eid, fr, to, cap), f in zip(channels, f_vec):
            net[fr] -= f
            net[to] += f
        for y_vec in itertools.product(*src_ranges):
            bal = dict(net)
            for (sid, node, cap), y in zip(sources, y_vec):
                bal[node] += y
            if bal != want:
                continue
            key = tuple(y_vec) + tuple(f_vec)
            if best_key is None or key < best_key:
                best_key = key
                best = (y_vec, f_vec)
    return best


class SolverCase(unittest.TestCase):
    def check(self, nodes, sources, channels, claims, label):
        sources = sorted(sources)
        channels = sorted(channels)
        claims = sorted(claims)
        planned, targets, groups, withdrawals, flows = solve_allocation(
            nodes, sources, channels, claims)
        d_vec, y_vec, f_vec, totals, want_targets = brute_allocation(
            nodes, sources, channels, claims)
        got = ([planned[c[0]] for c in claims],
               [withdrawals[s[0]] for s in sources],
               [flows[e[0]] for e in channels])
        want = (list(d_vec), list(y_vec), list(f_vec))
        self.assertEqual(got, want, label)
        self.assertEqual(groups, totals, label)
        for c in claims:
            self.assertEqual(targets[c[0]], want_targets[c[0]], label)
        return planned, targets, groups, withdrawals, flows

    def test_reroute_counterexample(self):
        # High priority can use two paths, low priority only one; the optimal
        # high-priority total must reroute off the shared channel so the low
        # priority group also reaches its maximum.
        nodes = ['s', 'm', 'h', 'l']
        sources = [('src', 's', 14)]
        channels = [('c1', 's', 'm', 10), ('c2', 'm', 'h', 10),
                    ('c3', 'm', 'l', 4), ('c4', 's', 'h', 6)]
        claims = [('H', 'h', 1, 10), ('L', 'l', 2, 4)]
        planned, _, groups, _, flows = self.check(nodes, sources, channels, claims, 'reroute')
        self.assertEqual(groups, {1: 10, 2: 4})
        self.assertEqual(planned, {'H': 10, 'L': 4})
        self.assertEqual(flows, {'c1': 8, 'c2': 4, 'c3': 4, 'c4': 6})

    def test_hamilton_infeasible_bottleneck(self):
        # Proportional target 4/4 is blocked by the shared bottleneck e2=3;
        # the unique deviation-minimising plan 5/3 must still be produced.
        nodes = ['s', 'a', 'b']
        sources = [('s1', 's', 10)]
        channels = [('e1', 's', 'a', 10), ('e2', 's', 'b', 3)]
        claims = [('A', 'a', 1, 5), ('B', 'b', 1, 5)]
        planned, targets, groups, _, _ = self.check(nodes, sources, channels, claims, 'bottleneck')
        self.assertEqual(targets, {'A': 4, 'B': 4})
        self.assertEqual(planned, {'A': 5, 'B': 3})
        self.assertEqual(groups, {1: 8})

    def test_claim_id_tiebreak(self):
        # After levels 1-2 several plans remain; claim-id order decides.
        nodes = ['s', 'a', 'b', 'c']
        sources = [('s1', 's', 6)]
        channels = [('e1', 's', 'a', 1), ('e2', 's', 'b', 6), ('e3', 's', 'c', 6)]
        claims = [('a', 'a', 1, 6), ('b', 'b', 1, 6), ('c', 'c', 1, 6)]
        planned, targets, groups, _, _ = self.check(nodes, sources, channels, claims, 'tiebreak')
        self.assertEqual(planned, {'a': 1, 'b': 3, 'c': 2})

    def test_remainder_ties_by_id(self):
        # Equal remainders: units go to lexicographically smaller claim ids.
        nodes = ['s', 't']
        sources = [('s1', 's', 5)]
        channels = [('e1', 's', 't', 5)]
        claims = [('c', 't', 1, 3), ('a', 't', 1, 3), ('b', 't', 1, 3)]
        planned, targets, _, _, _ = self.check(nodes, sources, channels, claims, 'remainder')
        self.assertEqual(targets, {'a': 2, 'b': 2, 'c': 1})
        self.assertEqual(planned, {'a': 2, 'b': 2, 'c': 1})

    def test_source_channel_vector(self):
        # Level 4 minimises sources by id first, then channels by id.
        nodes = ['s', 't']
        sources = [('s1', 's', 10), ('s2', 's', 3)]
        channels = [('e1', 's', 't', 10)]
        claims = [('c', 't', 1, 5)]
        _, _, _, withdrawals, _ = self.check(nodes, sources, channels, claims, 'srcvec')
        self.assertEqual(withdrawals, {'s1': 2, 's2': 3})
        nodes = ['s', 't']
        sources = [('s1', 's', 10)]
        channels = [('e1', 's', 't', 5), ('e2', 's', 't', 5)]
        claims = [('c', 't', 1, 6)]
        _, _, _, _, flows = self.check(nodes, sources, channels, claims, 'chvec')
        self.assertEqual(flows, {'e1': 1, 'e2': 5})

    def test_zero_quotas(self):
        nodes = ['s', 't']
        sources = [('s1', 's', 0), ('s2', 's', 4)]
        channels = [('e1', 's', 't', 0), ('e2', 's', 't', 4)]
        claims = [('a', 't', 1, 0), ('b', 't', 1, 3), ('c', 't', 2, 5)]
        planned, _, groups, withdrawals, flows = self.check(
            nodes, sources, channels, claims, 'zero')
        self.assertEqual(planned, {'a': 0, 'b': 3, 'c': 1})
        self.assertEqual(groups, {1: 3, 2: 1})

    def test_exhaustive_random_small(self):
        rng = random.Random(20261116)
        for trial in range(25):
            nn = rng.randint(2, 4)
            nodes = ['n%d' % i for i in range(nn)]
            order = list(nodes)
            rng.shuffle(order)
            pos = {n: i for i, n in enumerate(order)}
            channels = []
            for k in range(rng.randint(0, 3)):
                fr, to = rng.sample(nodes, 2)
                if pos[fr] > pos[to]:
                    fr, to = to, fr
                channels.append(('e%d' % k, fr, to, rng.randint(0, 3)))
            sources = [('s%d' % k, rng.choice(nodes), rng.randint(0, 3))
                       for k in range(rng.randint(1, 2))]
            claims = [('c%d' % k, rng.choice(nodes), rng.randint(1, 2), rng.randint(0, 3))
                      for k in range(rng.randint(1, 3))]
            self.check(nodes, sources, channels, claims, 'random-%d' % trial)

    def test_permutation_invariance(self):
        nodes = ['s', 'm', 't']
        sources = [('s1', 's', 9), ('s2', 'm', 4)]
        channels = [('e1', 's', 'm', 7), ('e2', 'm', 't', 9), ('e3', 's', 't', 3)]
        claims = [('a', 't', 1, 6), ('b', 'm', 1, 4), ('c', 't', 2, 5)]
        base = solve_allocation(nodes, sorted(sources), sorted(channels), sorted(claims))
        rng = random.Random(7)
        for _ in range(10):
            s2 = list(sources)
            c2 = list(channels)
            cl2 = list(claims)
            rng.shuffle(s2)
            rng.shuffle(c2)
            rng.shuffle(cl2)
            got = solve_allocation(nodes, sorted(s2), sorted(c2), sorted(cl2))
            self.assertEqual(base, got)

    def test_large_capacities_not_expanded(self):
        nodes = ['s', 'm', 't']
        sources = [('s1', 's', 10 ** 9), ('s2', 'm', 10 ** 9 - 1)]
        channels = [('e1', 's', 'm', 10 ** 9), ('e2', 'm', 't', 10 ** 9),
                    ('e3', 's', 't', 5 * 10 ** 8)]
        claims = [('a', 't', 1, 9 * 10 ** 8), ('b', 'm', 2, 10 ** 9)]
        start = time.time()
        planned, targets, groups, withdrawals, flows = solve_allocation(
            nodes, sorted(sources), sorted(channels), sorted(claims))
        elapsed = time.time() - start
        self.assertLess(elapsed, 10)
        self.assertEqual(planned, {'a': 9 * 10 ** 8, 'b': 10 ** 9})
        self.assertEqual(sum(planned.values()), sum(groups.values()))
        self.assertEqual(sum(withdrawals.values()), sum(groups.values()))

    def test_delivery_routing_exhaustive(self):
        nodes = ['s', 'm', 't']
        sources = [('s1', 's', 4), ('s2', 'm', 3)]
        channels = [('e1', 's', 'm', 4), ('e2', 'm', 't', 5), ('e3', 's', 't', 2)]
        claims = [('a', 't', 1, 5), ('b', 'm', 1, 3)]
        claim_nodes = {'a': 't', 'b': 'm'}
        for amounts in ({'a': 3, 'b': 2}, {'a': 2}, {'b': 3}, {'a': 4, 'b': 1}):
            w, f = solve_delivery(nodes, sorted(sources), sorted(channels),
                                  sorted(claims), amounts)
            y_vec, f_vec = brute_delivery(nodes, sorted(sources), sorted(channels),
                                          claim_nodes, amounts)
            got = ([w[s[0]] for s in sorted(sources)], [f[e[0]] for e in sorted(channels)])
            self.assertEqual(got, (list(y_vec), list(f_vec)), amounts)

    def test_delivery_infeasible(self):
        nodes = ['s', 't']
        sources = [('s1', 's', 2)]
        channels = [('e1', 's', 't', 2)]
        claims = [('a', 't', 1, 5)]
        w, f = solve_delivery(nodes, sources, channels, claims, {'a': 3})
        self.assertIsNone(w)
        self.assertIsNone(f)


if __name__ == '__main__':
    unittest.main()
