"""SQLite ledger for one scheduling season of network allocation.

One database file is one frozen scheduling season.  The season input is
validated and frozen at init; balances move only through settled deliveries.
Plans are signed with a per-ledger HMAC secret and carry the balance version
they were issued at; a plan never reserves or deducts water.  Settlement
re-solves the routing for the actual delivered amounts and deducts exactly
those source withdrawals, channel flows and claim amounts inside a single
atomic transaction that also bumps the balance version and stores the
replayable receipt, so the ledger never depends on external JSON files.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path

from .common import read, save, ident
from .network_flow import solve_allocation, solve_delivery
from .network_model import validate_season, validate_delivery, validate_plan_document

SCHEMA = 'network-ledger-v1'
_BUSY = 30  # seconds


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=_BUSY, isolation_level=None)
    conn.execute('PRAGMA busy_timeout = %d' % (_BUSY * 1000))
    return conn


def _open_ledger(db_path):
    if not os.path.lexists(db_path):
        raise ValueError('database does not exist: ' + str(db_path))
    try:
        conn = _connect(db_path)
    except sqlite3.Error as error:
        raise ValueError('cannot open ledger: ' + str(error)) from error
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
    except sqlite3.Error as error:
        conn.close()
        raise ValueError('not a network ledger database') from error
    if not row or row[0] != SCHEMA:
        conn.close()
        raise ValueError('not a network ledger database')
    return conn


def _meta(conn):
    return {k: v for k, v in conn.execute('SELECT k, v FROM meta')}


def _balances(conn):
    sources = [(r[0], r[1], r[2] - r[3])
               for r in conn.execute('SELECT id, node, cap, used FROM sources ORDER BY id')]
    channels = [(r[0], r[1], r[2], r[3] - r[4])
                for r in conn.execute('SELECT id, src, dst, cap, used FROM channels ORDER BY id')]
    claims = [(r[0], r[1], r[2], r[3] - r[4])
              for r in conn.execute('SELECT id, node, priority, requested, delivered FROM claims ORDER BY id')]
    return sources, channels, claims


def _snapshot(conn):
    """Consistent read of meta and remaining balances in one transaction."""
    conn.execute('BEGIN')
    try:
        meta = _meta(conn)
        balances = _balances(conn)
        nodes = json.loads(meta['nodes_json'])
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    return meta, nodes, balances


def init_ledger(source_path, db_path):
    """Create a new season ledger; refuses to overwrite any existing file."""
    if Path(source_path).resolve() == Path(db_path).resolve():
        raise ValueError('input and database must differ')
    data, digest = read(source_path)
    season = validate_season(data)
    dest = Path(db_path)
    if os.path.lexists(dest):
        raise ValueError('database already exists: ' + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    os.close(fd)
    conn = None
    try:
        conn = _connect(dest)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        conn.execute('CREATE TABLE sources (id TEXT PRIMARY KEY, node TEXT NOT NULL,'
                     ' cap INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        conn.execute('CREATE TABLE channels (id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL,'
                     ' cap INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        conn.execute('CREATE TABLE claims (id TEXT PRIMARY KEY, node TEXT NOT NULL, priority INTEGER NOT NULL,'
                     ' requested INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)')
        conn.execute('CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,'
                     ' request_json TEXT NOT NULL, receipt_json TEXT NOT NULL,'
                     ' version_before INTEGER NOT NULL, version_after INTEGER NOT NULL)')
        ledger_id = secrets.token_hex(16)
        meta = {
            'schema': SCHEMA,
            'ledger_id': ledger_id,
            'secret': secrets.token_hex(32),
            'version': '0',
            'input_sha256': digest,
            'input_json': _canonical(data),
            'nodes_json': _canonical(season['nodes']),
        }
        for key, value in meta.items():
            conn.execute('INSERT INTO meta (k, v) VALUES (?, ?)', (key, value))
        for sid, node, cap in season['sources']:
            conn.execute('INSERT INTO sources (id, node, cap) VALUES (?, ?, ?)', (sid, node, cap))
        for eid, fr, to, cap in season['channels']:
            conn.execute('INSERT INTO channels (id, src, dst, cap) VALUES (?, ?, ?, ?)', (eid, fr, to, cap))
        for cid, node, priority, req in season['claims']:
            conn.execute('INSERT INTO claims (id, node, priority, requested) VALUES (?, ?, ?, ?)',
                         (cid, node, priority, req))
        conn.execute('COMMIT')
    except Exception:
        if conn is not None:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            conn.close()
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    conn.close()
    return {
        'schema_version': 1,
        'kind': 'network-init',
        'ledger_id': ledger_id,
        'input_sha256': digest,
        'balance_version': 0,
        'nodes': len(season['nodes']),
        'sources': len(season['sources']),
        'channels': len(season['channels']),
        'claims': len(season['claims']),
        'priorities': len({c[2] for c in season['claims']}),
    }


def _plan_result(season_nodes, sources, channels, claims):
    planned, targets, group_totals, withdrawals, flows = solve_allocation(
        season_nodes, sources, channels, claims)
    claim_rows = []
    for cid, node, priority, req in claims:
        claim_rows.append({
            'id': cid, 'node': node, 'priority': priority, 'requested': req,
            'planned': planned[cid], 'target': targets[cid],
        })
    group_rows = []
    by_priority = {}
    for cid, node, priority, req in claims:
        by_priority.setdefault(priority, [0, 0])
        by_priority[priority][0] += req
        by_priority[priority][1] += targets[cid]
    for priority in sorted(group_totals):
        req_sum, target_sum = by_priority[priority]
        group_rows.append({
            'priority': priority, 'requested': req_sum,
            'planned': group_totals[priority], 'target': target_sum,
        })
    source_rows = [{'id': sid, 'node': node, 'withdrawal': withdrawals[sid]}
                   for sid, node, _ in sources]
    channel_rows = [{'id': eid, 'from': fr, 'to': to, 'flow': flows[eid]}
                    for eid, fr, to, _ in channels]
    return {'claims': claim_rows, 'groups': group_rows,
            'sources': source_rows, 'channels': channel_rows}


def _plan_content(meta, result, version):
    return {
        'ledger_id': meta['ledger_id'],
        'balance_version': version,
        'input_sha256': meta['input_sha256'],
        'result': result,
    }


def _plan_id(content):
    return hashlib.sha256(_canonical(content).encode('utf-8')).hexdigest()


def _plan_tag(secret, plan_id):
    return hmac.new(bytes.fromhex(secret), plan_id.encode('ascii'), hashlib.sha256).hexdigest()


def issue_plan(db_path, output_path):
    """Solve the current snapshot and sign a plan; never deducts anything."""
    if Path(db_path).resolve() == Path(output_path).resolve():
        raise ValueError('database and output must differ')
    conn = _open_ledger(db_path)
    try:
        meta, nodes, (sources, channels, claims) = _snapshot(conn)
    finally:
        conn.close()
    version = int(meta['version'])
    result = _plan_result(nodes, sources, channels, claims)
    content = _plan_content(meta, result, version)
    plan_id = _plan_id(content)
    plan = dict(content)
    plan.update({
        'schema_version': 1,
        'kind': 'network-plan',
        'plan_id': plan_id,
        'tag': _plan_tag(meta['secret'], plan_id),
    })
    save(output_path, plan)
    return plan


def _verify_plan(meta, plan):
    if plan['ledger_id'] != meta['ledger_id']:
        raise ValueError('plan belongs to a different ledger')
    if plan['input_sha256'] != meta['input_sha256']:
        raise ValueError('plan does not match the frozen season input')
    content = {
        'ledger_id': plan['ledger_id'],
        'balance_version': plan['balance_version'],
        'input_sha256': plan['input_sha256'],
        'result': plan['result'],
    }
    plan_id = _plan_id(content)
    if not hmac.compare_digest(plan_id, plan['plan_id']):
        raise ValueError('plan content was modified')
    tag = _plan_tag(meta['secret'], plan_id)
    if not hmac.compare_digest(tag, plan['tag']):
        raise ValueError('plan signature is invalid')


def _receipt(meta, delivery_id, plan_id, version_before, amounts, withdrawals, flows, sources, channels):
    return {
        'schema_version': 1,
        'kind': 'network-receipt',
        'ledger_id': meta['ledger_id'],
        'delivery_id': delivery_id,
        'plan_id': plan_id,
        'balance_version_before': version_before,
        'balance_version_after': version_before + 1,
        'claims': [{'id': cid, 'amount': amount} for cid, amount in amounts],
        'sources': [{'id': sid, 'withdrawal': withdrawals[sid]} for sid, _, _ in sources],
        'channels': [{'id': eid, 'flow': flows[eid]} for eid, _, _, _ in channels],
    }


def settle(db_path, plan_path, delivery_path, delivery_id, output_path):
    """Settle actual deliveries against an issued plan, atomically.

    Returns (receipt, replayed).  The receipt is stored in the ledger in the
    same transaction that deducts balances, so a crash between commit and
    output publication is recovered by retrying with the same delivery id.
    """
    ident(delivery_id)
    db_resolved = Path(db_path).resolve()
    for other in (plan_path, delivery_path, output_path):
        if Path(other).resolve() == db_resolved:
            raise ValueError('database path must not be overwritten by or equal to other paths')
    if Path(plan_path).resolve() == Path(delivery_path).resolve():
        raise ValueError('plan and delivery inputs must differ')
    if Path(output_path).resolve() in (Path(plan_path).resolve(), Path(delivery_path).resolve()):
        raise ValueError('output must not overwrite an input')
    if Path(output_path).is_symlink():
        raise ValueError('output cannot be a symlink')
    plan_doc, _ = read(plan_path)
    plan = validate_plan_document(plan_doc)
    delivery_doc, _ = read(delivery_path)
    entries = validate_delivery(delivery_doc)
    planned_map = {entry['id']: entry['planned'] for entry in plan['result']['claims']}
    for cid, amount in entries:
        if cid not in planned_map:
            raise ValueError('claim not covered by plan: ' + cid)
        if amount > planned_map[cid]:
            raise ValueError('delivery exceeds planned amount for claim: ' + cid)
    amounts = [(cid, amount) for cid, amount in entries if amount]
    request_json = _canonical([{'claim': cid, 'amount': amount} for cid, amount in amounts])
    conn = _open_ledger(db_path)
    try:
        meta = _meta(conn)
        _verify_plan(meta, plan)
        try:
            conn.execute('BEGIN IMMEDIATE')
            version = int(_meta(conn)['version'])
            row = conn.execute(
                'SELECT plan_id, request_json, receipt_json FROM deliveries WHERE delivery_id = ?',
                (delivery_id,)).fetchone()
            if row is not None:
                if row[0] == plan['plan_id'] and row[1] == request_json:
                    receipt = json.loads(row[2])
                    conn.execute('COMMIT')
                    replayed = True
                else:
                    raise ValueError('delivery id already used with a different request: ' + delivery_id)
            else:
                if version != plan['balance_version']:
                    raise ValueError('plan balance version is stale: plan=%d current=%d'
                                     % (plan['balance_version'], version))
                nodes = json.loads(meta['nodes_json'])
                sources, channels, claims = _balances(conn)
                amounts_map = dict(amounts)
                withdrawals, flows = solve_delivery(nodes, sources, channels, claims, amounts_map)
                if withdrawals is None:
                    raise ValueError('insufficient remaining network capacity for delivery')
                for cid, amount in amounts:
                    cur = conn.execute(
                        'UPDATE claims SET delivered = delivered + ?'
                        ' WHERE id = ? AND delivered + ? <= requested', (amount, cid, amount))
                    if cur.rowcount != 1:
                        raise ValueError('insufficient remaining claim balance: ' + cid)
                for sid, withdrawal in withdrawals.items():
                    if withdrawal:
                        cur = conn.execute(
                            'UPDATE sources SET used = used + ? WHERE id = ? AND used + ? <= cap',
                            (withdrawal, sid, withdrawal))
                        if cur.rowcount != 1:
                            raise ValueError('insufficient remaining source balance: ' + sid)
                for eid, flow in flows.items():
                    if flow:
                        cur = conn.execute(
                            'UPDATE channels SET used = used + ? WHERE id = ? AND used + ? <= cap',
                            (flow, eid, flow))
                        if cur.rowcount != 1:
                            raise ValueError('insufficient remaining channel balance: ' + eid)
                conn.execute("UPDATE meta SET v = ? WHERE k = 'version'", (str(version + 1),))
                receipt = _receipt(meta, delivery_id, plan['plan_id'], version,
                                   amounts, withdrawals, flows, sources, channels)
                conn.execute(
                    'INSERT INTO deliveries (delivery_id, plan_id, request_json, receipt_json,'
                    ' version_before, version_after) VALUES (?, ?, ?, ?, ?, ?)',
                    (delivery_id, plan['plan_id'], request_json, _canonical(receipt),
                     version, version + 1))
                conn.execute('COMMIT')
                replayed = False
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()
    try:
        save(output_path, receipt)
    except (ValueError, OSError) as error:
        state = 'already committed earlier' if replayed else 'committed at balance version %d' % receipt['balance_version_after']
        raise ValueError('settlement %s but receipt output failed (%s);'
                         ' retry with the same delivery-id to recover' % (state, error)) from error
    return receipt, replayed


def status_report(db_path, output_path):
    """Cumulative usage, remaining balances and all settlement receipts."""
    if Path(db_path).resolve() == Path(output_path).resolve():
        raise ValueError('database and output must differ')
    conn = _open_ledger(db_path)
    try:
        conn.execute('BEGIN')
        try:
            meta = _meta(conn)
            sources = [{'id': r[0], 'node': r[1], 'cap': r[2], 'used': r[3],
                        'remaining': r[2] - r[3]}
                       for r in conn.execute('SELECT id, node, cap, used FROM sources ORDER BY id')]
            channels = [{'id': r[0], 'from': r[1], 'to': r[2], 'cap': r[3], 'used': r[4],
                         'remaining': r[3] - r[4]}
                        for r in conn.execute('SELECT id, src, dst, cap, used FROM channels ORDER BY id')]
            claims = [{'id': r[0], 'node': r[1], 'priority': r[2], 'requested': r[3],
                       'delivered': r[4], 'remaining': r[3] - r[4]}
                      for r in conn.execute(
                          'SELECT id, node, priority, requested, delivered FROM claims ORDER BY id')]
            deliveries = [json.loads(r[0]) for r in conn.execute(
                'SELECT receipt_json FROM deliveries ORDER BY version_after, delivery_id')]
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()
    report = {
        'schema_version': 1,
        'kind': 'network-status',
        'ledger_id': meta['ledger_id'],
        'balance_version': int(meta['version']),
        'input_sha256': meta['input_sha256'],
        'sources': sources,
        'channels': channels,
        'claims': claims,
        'deliveries': deliveries,
    }
    save(output_path, report)
    return report
