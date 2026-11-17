"""Exact lexicographic integer network allocation (standard library only).

The four-level lexicographic plan objective is evaluated with three exact
min-cost flow computations over the full remaining network, so no level ever
freezes the routing chosen by an earlier level:

1. Group totals: one min-cost max-flow whose collector edges carry dominating
   negative weights per priority group yields the lexicographically maximal
   vector S of group delivery totals.  Feasible sink vectors of a network form
   a polymatroid, hence the lexicographic maximum is a base (total = max flow)
   and coincides with the unique max-weight vector; only the totals S are
   kept, the routes that produced them are discarded.
2. Deviations and per-claim amounts: group totals are fixed by collector
   capacities S.  Each claim edge is split into two segments whose marginal
   costs encode |planned - hamilton_target| weighted per group (dominant) plus
   a small negative per-unit weight encoding claim-id lexicographic
   maximisation.  One min-cost flow solves both levels jointly and exactly.
3. Routing: with claim amounts fixed, one min-cost flow with dominating
   positive weights on source and channel edges minimises the concatenated
   source-withdrawal / channel-flow vector lexicographically.

All weights are exact big integers derived from global bounds, comparisons
never use floats, and bottleneck augmentation pushes whole capacities so
values up to 1e9 are never expanded unit by unit.
"""
import heapq


class MinCostFlow:
    """Successive shortest paths with Johnson potentials on exact integers."""

    def __init__(self, nodes):
        self.graph = [[] for _ in range(nodes)]

    def add_edge(self, fr, to, cap, cost):
        fwd = [to, cap, cost, None]
        rev = [fr, 0, -cost, None]
        fwd[3] = rev
        rev[3] = fwd
        self.graph[fr].append(fwd)
        self.graph[to].append(rev)
        return fwd

    @staticmethod
    def flow(edge):
        """Flow carried by an edge returned from add_edge."""
        return edge[3][1]

    def min_cost_flow(self, source, sink, limit=None):
        """Min-cost flow: maximal while worthwhile, or exactly ``limit`` units.

        Every prefix of augmentations is min-cost for its flow value, so the
        result is min-cost among maximum flows (no limit) or min-cost among
        flows of value ``limit``.  Returns (flow, cost); flow may fall short
        of ``limit`` when the network is infeasible.
        """
        graph = self.graph
        size = len(graph)
        # Feasible potentials from an all-zero start (virtual super source),
        # valid even with negative edge costs.
        potential = [0] * size
        for _ in range(size - 1):
            updated = False
            for fr in range(size):
                base = potential[fr]
                for edge in graph[fr]:
                    if edge[1] > 0:
                        cand = base + edge[2]
                        if cand < potential[edge[0]]:
                            potential[edge[0]] = cand
                            updated = True
            if not updated:
                break
        flow = 0
        cost = 0
        while limit is None or flow < limit:
            dist = [None] * size
            dist[source] = 0
            prev = [None] * size
            heap = [(0, source)]
            while heap:
                du, u = heapq.heappop(heap)
                if dist[u] is not None and du != dist[u]:
                    continue
                for edge in graph[u]:
                    if edge[1] <= 0:
                        continue
                    to = edge[0]
                    cand = du + edge[2] + potential[u] - potential[to]
                    if dist[to] is None or cand < dist[to]:
                        dist[to] = cand
                        prev[to] = edge
                        heapq.heappush(heap, (cand, to))
            if dist[sink] is None:
                break
            for node in range(size):
                if dist[node] is not None:
                    potential[node] += dist[node]
            add = limit - flow if limit is not None else None
            edge = prev[sink]
            while edge is not None:
                if add is None or edge[1] < add:
                    add = edge[1]
                edge = prev[edge[3][0]]
            edge = prev[sink]
            while edge is not None:
                edge[1] -= add
                edge[3][1] += add
                cost += add * edge[2]
                edge = prev[edge[3][0]]
            flow += add
        return flow, cost


def largest_remainder(requested, total):
    """Hamilton apportionment of ``total`` over {claim: requested}.

    Same rule as the v1 basin allocator: floor shares, remaining units by
    largest remainder descending, ties by claim id ascending; zero-demand
    claims never receive a remainder unit.  Requires 0 <= total <= sum.
    """
    demand = sum(requested.values())
    if demand == 0:
        return {claim: 0 for claim in requested}
    if total >= demand:
        return dict(requested)
    shares = {claim: total * req // demand for claim, req in requested.items()}
    remaining = total - sum(shares.values())
    order = sorted(requested, key=lambda claim: (-(total * requested[claim] % demand), claim))
    for claim in order[:remaining]:
        shares[claim] += 1
    return shares


def _add_network(mcf, index, sources, channels, source_costs=None, channel_costs=None):
    src_edges = {}
    for pos, (sid, node, cap) in enumerate(sources):
        if cap:
            cost = source_costs[pos] if source_costs else 0
            src_edges[sid] = mcf.add_edge(0, index[node], cap, cost)
    ch_edges = {}
    for pos, (eid, fr, to, cap) in enumerate(channels):
        if cap:
            cost = channel_costs[pos] if channel_costs else 0
            ch_edges[eid] = mcf.add_edge(index[fr], index[to], cap, cost)
    return src_edges, ch_edges


def _route(nodes, sources, channels, claim_nodes, amounts):
    """Lexicographically minimal (source, channel) vector for fixed amounts.

    sources/channels must be sorted by id.  Returns (withdrawals, flows)
    keyed by id, or (None, None) when the amounts are infeasible.
    """
    total = sum(amounts.values())
    withdrawals = {sid: 0 for sid, _, _ in sources}
    flows = {eid: 0 for eid, _, _, _ in channels}
    if total == 0:
        return withdrawals, flows
    index = {node: pos + 1 for pos, node in enumerate(nodes)}
    sink = len(nodes) + 1
    items = len(sources) + len(channels)
    base = total + 1
    source_costs = [base ** (items - 1 - pos) for pos in range(len(sources))]
    channel_costs = [base ** (items - 1 - len(sources) - pos) for pos in range(len(channels))]
    mcf = MinCostFlow(sink + 1)
    src_edges, ch_edges = _add_network(mcf, index, sources, channels, source_costs, channel_costs)
    for cid, amount in amounts.items():
        if amount:
            mcf.add_edge(index[claim_nodes[cid]], sink, amount, 0)
    flow, _ = mcf.min_cost_flow(0, sink, limit=total)
    if flow != total:
        return None, None
    for sid, edge in src_edges.items():
        withdrawals[sid] = MinCostFlow.flow(edge)
    for eid, edge in ch_edges.items():
        flows[eid] = MinCostFlow.flow(edge)
    return withdrawals, flows


def solve_allocation(nodes, sources, channels, claims):
    """Four-level lexicographic optimal plan over the remaining balances.

    nodes: list of node ids; sources: [(id, node, cap)] sorted by id;
    channels: [(id, from, to, cap)] sorted by id;
    claims: [(id, node, priority, requested)] sorted by id.
    Returns (planned, targets, group_totals, withdrawals, flows) keyed by id.
    """
    planned = {cid: 0 for cid, _, _, _ in claims}
    targets = dict(planned)
    withdrawals = {sid: 0 for sid, _, _ in sources}
    flows = {eid: 0 for eid, _, _, _ in channels}
    by_priority = {}
    for cid, node, priority, req in claims:
        by_priority.setdefault(priority, []).append((cid, node, req))
    priorities = sorted(by_priority)
    group_totals = {priority: 0 for priority in priorities}
    total_req = sum(req for _, _, _, req in claims)
    total_cap = sum(cap for _, _, cap in sources)
    if not claims or total_req == 0 or total_cap == 0:
        return planned, targets, group_totals, withdrawals, flows
    index = {node: pos + 1 for pos, node in enumerate(nodes)}
    groups = len(priorities)
    sink = len(nodes) + groups + 1
    # Level 1: lexicographically maximal group totals; routes stay free.
    bound = min(total_req, total_cap)
    mcf = MinCostFlow(sink + 1)
    _add_network(mcf, index, sources, channels)
    collector = {}
    for pos, priority in enumerate(priorities):
        node_col = len(nodes) + 1 + pos
        weight = (bound + 1) ** (groups - 1 - pos)
        demand = 0
        for cid, node, req in by_priority[priority]:
            if req:
                mcf.add_edge(index[node], node_col, req, 0)
            demand += req
        collector[priority] = mcf.add_edge(node_col, sink, demand, -weight)
    mcf.min_cost_flow(0, sink)
    for priority in priorities:
        group_totals[priority] = MinCostFlow.flow(collector[priority])
    # Levels 2+3: group totals fixed; joint deviation / claim-id objective.
    for priority in priorities:
        reqs = {cid: req for cid, _, req in by_priority[priority]}
        targets.update(largest_remainder(reqs, group_totals[priority]))
    base_claim = total_req + 1
    claim_weight = {}
    for pos, (cid, _, _, _) in enumerate(claims):
        claim_weight[cid] = base_claim ** (len(claims) - 1 - pos)
    claim_bound = base_claim ** len(claims)
    base_group = 2 * total_req + 1
    mcf = MinCostFlow(sink + 1)
    _add_network(mcf, index, sources, channels)
    segments = {}
    for pos, priority in enumerate(priorities):
        node_col = len(nodes) + 1 + pos
        weight = 2 * claim_bound * base_group ** (groups - 1 - pos)
        for cid, node, req in by_priority[priority]:
            target = targets[cid]
            cw = claim_weight[cid]
            low = mcf.add_edge(index[node], node_col, target, -(weight + cw)) if target else None
            high = mcf.add_edge(index[node], node_col, req - target, weight - cw) if req > target else None
            segments[cid] = (low, high)
        mcf.add_edge(node_col, sink, group_totals[priority], 0)
    total = sum(group_totals.values())
    flow, _ = mcf.min_cost_flow(0, sink, limit=total)
    if flow != total:
        raise ValueError('network cannot realize optimal group totals')
    for cid, (low, high) in segments.items():
        amount = 0
        if low is not None:
            amount += MinCostFlow.flow(low)
        if high is not None:
            amount += MinCostFlow.flow(high)
        planned[cid] = amount
    # Level 4: claim amounts fixed; lexicographically minimal routing vector.
    claim_nodes = {cid: node for cid, node, _, _ in claims}
    amounts = {cid: planned[cid] for cid in planned if planned[cid]}
    withdrawals, flows = _route(nodes, sources, channels, claim_nodes, amounts)
    if withdrawals is None:
        raise ValueError('network cannot route optimal claim amounts')
    return planned, targets, group_totals, withdrawals, flows


def solve_delivery(nodes, sources, channels, claims, amounts):
    """Route actual delivery amounts with the plan's source/channel rule.

    amounts: {claim id: positive amount}.  Returns (withdrawals, flows)
    keyed by id, or (None, None) when infeasible.
    """
    claim_nodes = {cid: node for cid, node, _, _ in claims}
    return _route(nodes, sources, channels, claim_nodes, amounts)
