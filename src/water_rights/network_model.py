"""Frozen season input and delivery document contracts for the network ledger."""
from .common import require, ident, number, rows

MAX_NODES = 60
MAX_CHANNELS = 240
MAX_CLAIMS = 100
MAX_PRIORITIES = 16
MAX_SOURCES = 1000  # defensive bound; the contract limits nodes/channels/claims
MAX_AMOUNT = 10 ** 9
MAX_PRIORITY = 10 ** 6  # same range as the v1 basin allocator


def _require_dag(nodes, channels):
    indegree = {node: 0 for node in nodes}
    adjacent = {node: [] for node in nodes}
    for _, fr, to, _ in channels:
        adjacent[fr].append(to)
        indegree[to] += 1
    queue = [node for node in nodes if indegree[node] == 0]
    seen = 0
    while queue:
        u = queue.pop()
        seen += 1
        for v in adjacent[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                queue.append(v)
    if seen != len(nodes):
        raise ValueError('channels must form a directed acyclic graph')


def validate_season(data):
    """Validate the frozen season input; returns normalized sorted tuples."""
    require(data, 'nodes sources channels claims')
    nodes = []
    seen = set()
    for node in rows(data['nodes'], MAX_NODES):
        ident(node)
        if node in seen:
            raise ValueError('duplicate node: ' + node)
        seen.add(node)
        nodes.append(node)
    known = set(nodes)
    sources = []
    seen = set()
    for entry in rows(data['sources'], MAX_SOURCES):
        require(entry, 'id node cap')
        sid = ident(entry['id'])
        node = ident(entry['node'])
        cap = number(entry['cap'], 0, MAX_AMOUNT)
        if node not in known:
            raise ValueError('unknown source node: ' + node)
        if sid in seen:
            raise ValueError('duplicate source: ' + sid)
        seen.add(sid)
        sources.append((sid, node, cap))
    channels = []
    seen = set()
    for entry in rows(data['channels'], MAX_CHANNELS):
        require(entry, 'id from to cap')
        eid = ident(entry['id'])
        fr = ident(entry['from'])
        to = ident(entry['to'])
        cap = number(entry['cap'], 0, MAX_AMOUNT)
        if fr not in known or to not in known:
            raise ValueError('unknown channel endpoint')
        if eid in seen:
            raise ValueError('duplicate channel: ' + eid)
        seen.add(eid)
        channels.append((eid, fr, to, cap))
    _require_dag(nodes, channels)
    claims = []
    seen = set()
    priorities = set()
    for entry in rows(data['claims'], MAX_CLAIMS):
        require(entry, 'id node priority requested')
        cid = ident(entry['id'])
        node = ident(entry['node'])
        priority = number(entry['priority'], 0, MAX_PRIORITY)
        requested = number(entry['requested'], 0, MAX_AMOUNT)
        if node not in known:
            raise ValueError('unknown claim node: ' + node)
        if cid in seen:
            raise ValueError('duplicate claim: ' + cid)
        seen.add(cid)
        priorities.add(priority)
        claims.append((cid, node, priority, requested))
    if len(priorities) > MAX_PRIORITIES:
        raise ValueError('too many distinct priorities')
    return {
        'nodes': nodes,
        'sources': sorted(sources),
        'channels': sorted(channels),
        'claims': sorted(claims),
    }


def validate_delivery(data):
    """Validate a delivery document; returns sorted [(claim, amount)].

    Every entry is validated (zero amounts included); normalization for
    routing and replay comparison drops zero entries, so equivalent
    requests compare equal.
    """
    require(data, 'deliveries')
    seen = set()
    entries = []
    positive = 0
    for entry in rows(data['deliveries'], MAX_CLAIMS):
        require(entry, 'claim amount')
        claim = ident(entry['claim'])
        amount = number(entry['amount'], 0, MAX_AMOUNT)
        if claim in seen:
            raise ValueError('duplicate claim in delivery: ' + claim)
        seen.add(claim)
        entries.append((claim, amount))
        if amount:
            positive += 1
    if not positive:
        raise ValueError('at least one delivery must be positive')
    return sorted(entries)


def _hex_token(value, field):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError('expected hex token: ' + field)
    return value


def validate_plan_document(doc):
    """Structural validation of a plan document (signature checked later)."""
    require(doc, 'schema_version kind ledger_id plan_id balance_version input_sha256 result tag')
    if doc['schema_version'] != 1 or doc['kind'] != 'network-plan':
        raise ValueError('not a network plan document')
    ident(doc['ledger_id'])
    _hex_token(doc['plan_id'], 'plan_id')
    _hex_token(doc['tag'], 'tag')
    ident(doc['input_sha256'])
    number(doc['balance_version'], 0, 10 ** 18)
    result = doc['result']
    require(result, 'claims groups sources channels')
    for entry in rows(result['claims'], MAX_CLAIMS):
        require(entry, 'id node priority requested planned target')
        ident(entry['id'])
        ident(entry['node'])
        number(entry['priority'], 0, MAX_PRIORITY)
        number(entry['requested'], 0, MAX_AMOUNT)
        number(entry['planned'], 0, MAX_AMOUNT)
        number(entry['target'], 0, MAX_AMOUNT)
    for entry in rows(result['groups'], MAX_PRIORITIES):
        require(entry, 'priority requested planned target')
        number(entry['priority'], 0, MAX_PRIORITY)
        number(entry['requested'], 0, 10 ** 14)
        number(entry['planned'], 0, 10 ** 14)
        number(entry['target'], 0, 10 ** 14)
    for entry in rows(result['sources'], MAX_SOURCES):
        require(entry, 'id node withdrawal')
        ident(entry['id'])
        ident(entry['node'])
        number(entry['withdrawal'], 0, MAX_AMOUNT)
    for entry in rows(result['channels'], MAX_CHANNELS):
        require(entry, 'id from to flow')
        ident(entry['id'])
        ident(entry['from'])
        ident(entry['to'])
        number(entry['flow'], 0, MAX_AMOUNT)
    return doc
