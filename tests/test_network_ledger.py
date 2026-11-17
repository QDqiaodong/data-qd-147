"""End-to-end acceptance tests for the network allocation ledger CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from tests.test_network import brute_allocation
except ImportError:  # unittest discover puts tests/ on sys.path
    from test_network import brute_allocation

SEASON = {
    'nodes': ['n1', 'n2'],
    'sources': [
        {'id': 's1', 'node': 'n1', 'cap': 10},
        {'id': 's2', 'node': 'n1', 'cap': 5},
    ],
    'channels': [
        {'id': 'e1', 'from': 'n1', 'to': 'n2', 'cap': 15},
    ],
    'claims': [
        {'id': 'a', 'node': 'n2', 'priority': 1, 'requested': 5},
        {'id': 'b', 'node': 'n2', 'priority': 1, 'requested': 5},
    ],
}


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'water_rights.network_cli', *args],
                          capture_output=True, text=True)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.source = self.td / 'season.json'
        self.source.write_text(json.dumps(SEASON))
        self.db = self.td / 'season.db'

    def write(self, name, obj):
        path = self.td / name
        path.write_text(json.dumps(obj))
        return path

    def init(self, source=None, db=None):
        return cli('init', '--source', str(source or self.source),
                   '--db', str(db or self.db))

    def plan(self, output='plan.json', db=None):
        return cli('plan', '--db', str(db or self.db), '--output', str(self.td / output))

    def settle(self, delivery, delivery_id, plan='plan.json', output='receipt.json'):
        return cli('settle', '--db', str(self.db), '--plan', str(self.td / plan),
                   '--source', str(delivery), '--delivery-id', delivery_id,
                   '--output', str(self.td / output))

    def status(self, output='status.json'):
        return cli('status', '--db', str(self.db), '--output', str(self.td / output))

    def read(self, name):
        return json.loads((self.td / name).read_text())

    def init_ok(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def plan_ok(self, output='plan.json', db=None):
        r = self.plan(output, db=db)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def settle_ok(self, delivery, delivery_id, plan='plan.json', output='receipt.json'):
        r = self.settle(delivery, delivery_id, plan, output)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def status_ok(self, output='status.json'):
        r = self.status(output)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class Init( LedgerCase):
    def test_init_creates_ledger(self):
        report = self.init_ok()
        self.assertEqual(report['kind'], 'network-init')
        self.assertEqual(report['balance_version'], 0)
        self.assertTrue(self.db.exists())

    def test_init_refuses_overwrite(self):
        self.init_ok()
        before = self.db.read_bytes()
        r = self.init()
        self.assertEqual(r.returncode, 2)
        self.assertIn('already exists', r.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_init_refuses_symlink_db(self):
        link = self.td / 'link.db'
        link.symlink_to(self.td / 'missing-target')
        r = self.init(db=link)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_init_input_equals_db_rejected(self):
        r = self.init(db=self.source)
        self.assertEqual(r.returncode, 2)

    def test_init_validation_failures_leave_no_db(self):
        bad = [
            dict(SEASON, extra=1),
            dict(SEASON, nodes=['n1', 'n1']),
            dict(SEASON, nodes=['n%d' % i for i in range(61)]),
            dict(SEASON, sources=[{'id': 's1', 'node': 'nope', 'cap': 1}]),
            dict(SEASON, sources=[{'id': 's1', 'node': 'n1', 'cap': 1},
                                  {'id': 's1', 'node': 'n1', 'cap': 2}]),
            dict(SEASON, channels=[{'id': 'e1', 'from': 'n2', 'to': 'n1', 'cap': 1},
                                   {'id': 'e2', 'from': 'n1', 'to': 'n2', 'cap': 1}]),
            dict(SEASON, channels=[{'id': 'e1', 'from': 'n1', 'to': 'n1', 'cap': 1}]),
            dict(SEASON, channels=[{'id': 'e%d' % i, 'from': 'n1', 'to': 'n2', 'cap': 1}
                                   for i in range(241)]),
            dict(SEASON, claims=[{'id': 'c%d' % i, 'node': 'n2', 'priority': 1, 'requested': 1}
                                 for i in range(101)]),
            dict(SEASON, claims=[{'id': 'c%d' % i, 'node': 'n2', 'priority': i, 'requested': 1}
                                 for i in range(17)]),
            dict(SEASON, sources=[{'id': 's1', 'node': 'n1', 'cap': 10 ** 9 + 1}]),
            dict(SEASON, claims=[{'id': 'c', 'node': 'n2', 'priority': 1, 'requested': -1}]),
            dict(SEASON, claims=[{'id': 'c', 'node': 'n2', 'priority': 10 ** 6 + 1,
                                  'requested': 1}]),
        ]
        for i, data in enumerate(bad):
            src = self.write('bad%d.json' % i, data)
            db = self.td / ('bad%d.db' % i)
            r = self.init(source=src, db=db)
            self.assertEqual(r.returncode, 2, (i, r.stderr))
            self.assertFalse(db.exists(), i)

    def test_plan_requires_ledger(self):
        r = self.plan()
        self.assertEqual(r.returncode, 2)
        self.db.write_text('not sqlite')
        r = self.plan()
        self.assertEqual(r.returncode, 2)

    def test_init_accepts_parallel_channels_and_shared_nodes(self):
        data = {
            'nodes': ['n%d' % i for i in range(60)],
            'sources': [{'id': 's%d' % i, 'node': 'n0', 'cap': i}
                        for i in range(4)],
            'channels': [{'id': 'e%d' % i, 'from': 'n0', 'to': 'n1', 'cap': 3}
                         for i in range(4)],
            'claims': [{'id': 'c%d' % i, 'node': 'n1', 'priority': i % 16, 'requested': 2}
                       for i in range(4)],
        }
        src = self.write('wide.json', data)
        db = self.td / 'wide.db'
        r = self.init(source=src, db=db)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['nodes'], 60)
        self.assertEqual(report['priorities'], 4)

    def test_failure_preserves_existing_output(self):
        self.init_ok()
        out = self.td / 'receipt.json'
        out.write_text('prior')
        delivery = self.write('bad.json', {'deliveries': [{'claim': 'a', 'amount': 99}]})
        r = self.settle(delivery, 'd-1')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(out.read_text(), 'prior')
        plan_out = self.td / 'plan.json'
        plan_out.write_text('prior')
        r = cli('plan', '--db', str(self.td / 'missing.db'), '--output', str(plan_out))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(plan_out.read_text(), 'prior')


class PlanFlow(LedgerCase):
    def test_plan_values_and_no_deduction(self):
        self.init_ok()
        plan = self.plan_ok()
        result = plan['result']
        self.assertEqual(plan['balance_version'], 0)
        planned = {c['id']: c['planned'] for c in result['claims']}
        self.assertEqual(planned, {'a': 5, 'b': 5})
        targets = {c['id']: c['target'] for c in result['claims']}
        self.assertEqual(targets, {'a': 5, 'b': 5})
        self.assertEqual({g['priority']: g['planned'] for g in result['groups']}, {1: 10})
        withdrawals = {s['id']: s['withdrawal'] for s in result['sources']}
        self.assertEqual(withdrawals, {'s1': 5, 's2': 5})
        flows = {c['id']: c['flow'] for c in result['channels']}
        self.assertEqual(flows, {'e1': 10})
        # plan does not reserve or deduct: balances and version unchanged
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 0)
        self.assertTrue(all(s['used'] == 0 for s in status['sources']))
        self.assertTrue(all(c['used'] == 0 for c in status['channels']))
        self.assertTrue(all(c['delivered'] == 0 for c in status['claims']))
        # plan is deterministic
        again = self.plan_ok('plan2.json')
        self.assertEqual(again, plan)

    def test_plan_order_independence(self):
        self.init_ok()
        plan_a = self.plan_ok()
        shuffled = dict(SEASON)
        shuffled['sources'] = list(reversed(SEASON['sources']))
        shuffled['claims'] = list(reversed(SEASON['claims']))
        shuffled['channels'] = list(reversed(SEASON['channels']))
        shuffled['nodes'] = list(reversed(SEASON['nodes']))
        src2 = self.write('season2.json', shuffled)
        db2 = self.td / 'season2.db'
        r = self.init(source=src2, db=db2)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan_b = self.plan_ok('plan_b.json', db=db2)
        self.assertEqual(plan_a['result'], plan_b['result'])

    def test_fixture_season(self):
        src = ROOT / 'fixtures' / 'network_input.json'
        db = self.td / 'fixture.db'
        r = cli('init', '--source', str(src), '--db', str(db))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.td / 'fixture_plan.json'
        r = cli('plan', '--db', str(db), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(out.read_text())
        planned = {c['id']: c['planned'] for c in plan['result']['claims']}
        self.assertEqual(planned, {'farm-x': 30, 'farm-y': 30, 'town': 10})
        r = cli('settle', '--db', str(db), '--plan', str(out),
                '--source', str(ROOT / 'fixtures' / 'network_delivery.json'),
                '--delivery-id', 'fixture-1', '--output', str(self.td / 'fixture_receipt.json'))
        self.assertEqual(r.returncode, 0, r.stderr)
        receipt = json.loads(r.stdout)
        self.assertEqual({s['id']: s['withdrawal'] for s in receipt['sources']},
                         {'s-a': 0, 's-b': 25})
        self.assertEqual({c['id']: c['flow'] for c in receipt['channels']},
                         {'e1': 0, 'e2': 25, 'e3': 25})
        self.assertEqual({c['id']: c['amount'] for c in receipt['claims']},
                         {'farm-y': 20, 'town': 5})


class Settle(LedgerCase):
    def prepare(self):
        self.init_ok()
        return self.plan_ok()

    def test_settle_deducts_actual_routing_not_plan(self):
        plan = self.prepare()
        # plan routes 5/5 from s1/s2; delivering only a=3 reroutes to s2 only
        self.assertEqual({s['id']: s['withdrawal'] for s in plan['result']['sources']},
                         {'s1': 5, 's2': 5})
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        receipt = self.settle_ok(delivery, 'd-1')
        self.assertEqual(receipt['plan_id'], plan['plan_id'])
        self.assertEqual(receipt['balance_version_before'], 0)
        self.assertEqual(receipt['balance_version_after'], 1)
        self.assertEqual(receipt['claims'], [{'id': 'a', 'amount': 3}])
        self.assertEqual({s['id']: s['withdrawal'] for s in receipt['sources']},
                         {'s1': 0, 's2': 3})
        self.assertEqual({c['id']: c['flow'] for c in receipt['channels']}, {'e1': 3})
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual({s['id']: s['used'] for s in status['sources']}, {'s1': 0, 's2': 3})
        self.assertEqual({c['id']: c['used'] for c in status['channels']}, {'e1': 3})
        self.assertEqual({c['id']: c['delivered'] for c in status['claims']}, {'a': 3, 'b': 0})
        self.assertEqual(len(status['deliveries']), 1)
        self.assertEqual(status['deliveries'][0], receipt)

    def test_replay_same_id_returns_original_without_double_deduct(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3},
                                                         {'claim': 'b', 'amount': 0}]})
        first = self.settle_ok(delivery, 'd-1')
        # normalized request drops the zero entry; reordered equivalent request replays
        equivalent = self.write('d2.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        second = self.settle_ok(equivalent, 'd-1', output='receipt2.json')
        self.assertEqual(first, second)
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual(len(status['deliveries']), 1)
        self.assertEqual({c['id']: c['delivered'] for c in status['claims']}, {'a': 3, 'b': 0})

    def test_same_id_different_request_conflicts(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        self.settle_ok(delivery, 'd-1')
        other = self.write('d2.json', {'deliveries': [{'claim': 'a', 'amount': 2}]})
        r = self.settle(other, 'd-1')
        self.assertEqual(r.returncode, 2)
        self.assertIn('different request', r.stderr)
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)

    def test_stale_plan_rejected_with_zero_deduction(self):
        plan = self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        self.settle_ok(delivery, 'd-1')
        # the old plan must not authorize further deliveries
        delivery2 = self.write('d2.json', {'deliveries': [{'claim': 'b', 'amount': 2}]})
        r = self.settle(delivery2, 'd-2')
        self.assertEqual(r.returncode, 2)
        self.assertIn('stale', r.stderr)
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual({c['id']: c['delivered'] for c in status['claims']}, {'a': 3, 'b': 0})

    def test_tampered_foreign_and_unsigned_plans_rejected(self):
        plan = self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        tampered = dict(plan)
        tampered['result'] = json.loads(json.dumps(plan['result']))
        tampered['result']['claims'][0]['planned'] += 1
        bad_plan = self.write('tampered.json', tampered)
        r = self.settle(delivery, 'd-1', plan='tampered.json')
        self.assertEqual(r.returncode, 2)
        # foreign ledger plan
        db2 = self.td / 'other.db'
        r = self.init(db=db2)
        self.assertEqual(r.returncode, 0, r.stderr)
        foreign = self.plan_ok('foreign.json', db=db2)
        r = self.settle(delivery, 'd-1', plan='foreign.json')
        self.assertEqual(r.returncode, 2)
        self.assertIn('different ledger', r.stderr)
        # unsigned fabrication
        fake = dict(plan)
        fake['tag'] = '0' * 64
        self.write('fake.json', fake)
        r = self.settle(delivery, 'd-1', plan='fake.json')
        self.assertEqual(r.returncode, 2)
        # nothing was deducted by any of the rejections
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 0)
        self.assertTrue(all(s['used'] == 0 for s in status['sources']))
        self.assertEqual(status['deliveries'], [])

    def test_invalid_deliveries_rejected_cleanly(self):
        self.prepare()
        cases = [
            {'deliveries': []},
            {'deliveries': [{'claim': 'a', 'amount': 0}]},
            {'deliveries': [{'claim': 'a', 'amount': 6}]},          # exceeds planned 5
            {'deliveries': [{'claim': 'zz', 'amount': 1}]},         # unknown claim
            {'deliveries': [{'claim': 'zz', 'amount': 0}, {'claim': 'a', 'amount': 1}]},
            {'deliveries': [{'claim': 'a', 'amount': 1}, {'claim': 'a', 'amount': 1}]},
            {'deliveries': [{'claim': 'a', 'amount': -1}]},
            {'deliveries': [{'claim': 'a', 'amount': 1.5}]},
            {'deliveries': [{'claim': 'a'}]},
            {},
        ]
        for i, body in enumerate(cases):
            delivery = self.write('bad%d.json' % i, body)
            r = self.settle(delivery, 'd-%d' % i)
            self.assertEqual(r.returncode, 2, (i, r.stderr))
            self.assertFalse((self.td / 'receipt.json').exists())
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 0)
        self.assertEqual(status['deliveries'], [])

    def test_concurrent_settles_single_commit(self):
        self.prepare()
        deliveries = [self.write('dc%d.json' % i,
                                 {'deliveries': [{'claim': 'a', 'amount': i + 1}]})
                      for i in range(3)]
        procs = [subprocess.Popen([sys.executable, '-m', 'water_rights.network_cli',
                                   'settle', '--db', str(self.db),
                                   '--plan', str(self.td / 'plan.json'),
                                   '--source', str(deliveries[i]),
                                   '--delivery-id', 'race-%d' % i,
                                   '--output', str(self.td / ('race%d.json' % i))],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
                 for i in range(3)]
        codes = []
        for p in procs:
            p.communicate()
            codes.append(p.returncode)
        self.assertEqual(sorted(codes), [0, 2, 2], codes)
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual(len(status['deliveries']), 1)
        delivered = {c['id']: c['delivered'] for c in status['claims']}
        self.assertEqual(delivered['b'], 0)
        self.assertIn(delivered['a'], (1, 2, 3))

    def test_concurrent_plan_and_settle_consistent_snapshots(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        procs = []
        for i in range(4):
            procs.append(subprocess.Popen(
                [sys.executable, '-m', 'water_rights.network_cli',
                 'plan', '--db', str(self.db), '--output', str(self.td / ('cp%d.json' % i))],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        procs.append(subprocess.Popen(
            [sys.executable, '-m', 'water_rights.network_cli',
             'settle', '--db', str(self.db), '--plan', str(self.td / 'plan.json'),
             '--source', str(delivery), '--delivery-id', 'd-1',
             '--output', str(self.td / 'cs.json')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        plans = []
        for p in procs[:4]:
            p.communicate()
            self.assertEqual(p.returncode, 0)
        procs[4].communicate()
        for i in range(4):
            plan = json.loads((self.td / ('cp%d.json' % i)).read_text())
            # every plan is one coherent snapshot: version 0 or 1, and its
            # withdrawals/flows never exceed the caps of that version
            self.assertIn(plan['balance_version'], (0, 1))
            for s in plan['result']['sources']:
                self.assertLessEqual(s['withdrawal'], 10 if s['id'] == 's1' else 5)
            if plan['balance_version'] == 1:
                remaining = {'s1': 10, 's2': 2}
                for s in plan['result']['sources']:
                    self.assertLessEqual(s['withdrawal'], remaining[s['id']])

    def test_commit_then_output_failure_recovers_by_replay(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        outdir = self.td / 'blocked'
        outdir.mkdir()
        r = cli('settle', '--db', str(self.db), '--plan', str(self.td / 'plan.json'),
                '--source', str(delivery), '--delivery-id', 'd-1', '--output', str(outdir))
        self.assertEqual(r.returncode, 2)
        self.assertIn('retry with the same delivery-id', r.stderr)
        # the settlement committed despite the output failure
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual({c['id']: c['delivered'] for c in status['claims']}, {'a': 3, 'b': 0})
        committed = status['deliveries'][0]
        # the ledger alone decides; deleting external JSON changes nothing
        retry = self.settle_ok(delivery, 'd-1', output='recovered.json')
        self.assertEqual(retry, committed)
        status = self.status_ok('status2.json')
        self.assertEqual(status['balance_version'], 1)
        self.assertEqual(len(status['deliveries']), 1)

    def test_output_symlink_rejected_before_commit(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        link = self.td / 'link.json'
        link.symlink_to(self.td / 'elsewhere.json')
        r = cli('settle', '--db', str(self.db), '--plan', str(self.td / 'plan.json'),
                '--source', str(delivery), '--delivery-id', 'd-1', '--output', str(link))
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.td / 'elsewhere.json').exists())
        status = self.status_ok()
        self.assertEqual(status['balance_version'], 0)
        self.assertEqual(status['deliveries'], [])

    def test_output_must_not_overwrite_db_or_inputs(self):
        self.prepare()
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        r = cli('settle', '--db', str(self.db), '--plan', str(self.td / 'plan.json'),
                '--source', str(delivery), '--delivery-id', 'd-1', '--output', str(self.db))
        self.assertEqual(r.returncode, 2)
        r = cli('settle', '--db', str(self.db), '--plan', str(self.td / 'plan.json'),
                '--source', str(delivery), '--delivery-id', 'd-1', '--output', str(delivery))
        self.assertEqual(r.returncode, 2)
        r = cli('plan', '--db', str(self.db), '--output', str(self.db))
        self.assertEqual(r.returncode, 2)
        r = cli('status', '--db', str(self.db), '--output', str(self.db))
        self.assertEqual(r.returncode, 2)


class Linkage(LedgerCase):
    def test_partial_settlement_then_replan(self):
        self.init_ok()
        plan = self.plan_ok()
        # settle only part of claim a; the actual routing differs from the plan
        delivery = self.write('d1.json', {'deliveries': [{'claim': 'a', 'amount': 3}]})
        receipt = self.settle_ok(delivery, 'd-1')
        self.assertEqual({s['id']: s['withdrawal'] for s in receipt['sources']},
                         {'s1': 0, 's2': 3})
        self.assertNotEqual(
            {s['id']: s['withdrawal'] for s in receipt['sources']},
            {s['id']: s['withdrawal'] for s in plan['result']['sources']})
        # old plan is now invalid
        r = self.settle(delivery, 'd-2')
        self.assertEqual(r.returncode, 2)
        # replan on remaining balances; undelivered quota of a is preserved
        plan2 = self.plan_ok('plan2.json')
        self.assertEqual(plan2['balance_version'], 1)
        planned = {c['id']: c['planned'] for c in plan2['result']['claims']}
        self.assertEqual(planned, {'a': 2, 'b': 5})
        # independent exhaustive check of the solve on remaining balances
        nodes = SEASON['nodes']
        sources = sorted((s['id'], s['node'], 10 if s['id'] == 's1' else 2)
                         for s in SEASON['sources'])
        channels = sorted((c['id'], c['from'], c['to'], 12) for c in SEASON['channels'])
        claims = sorted([('a', 'n2', 1, 2), ('b', 'n2', 1, 5)])
        d_vec, y_vec, f_vec, totals, targets = brute_allocation(nodes, sources, channels, claims)
        self.assertEqual([planned['a'], planned['b']], list(d_vec))
        got_y = {s['id']: s['withdrawal'] for s in plan2['result']['sources']}
        self.assertEqual([got_y['s1'], got_y['s2']], list(y_vec))
        got_f = {c['id']: c['flow'] for c in plan2['result']['channels']}
        self.assertEqual([got_f['e1']], list(f_vec))
        # settle the remainder against the new plan
        delivery2 = self.write('d2.json', {'deliveries': [{'claim': 'a', 'amount': 2},
                                                          {'claim': 'b', 'amount': 5}]})
        receipt2 = self.settle_ok(delivery2, 'd-2', plan='plan2.json', output='r2.json')
        self.assertEqual(receipt2['balance_version_before'], 1)
        self.assertEqual(receipt2['balance_version_after'], 2)
        status = self.status_ok()
        self.assertEqual({c['id']: c['remaining'] for c in status['claims']}, {'a': 0, 'b': 0})
        self.assertEqual({s['id']: s['used'] for s in status['sources']}, {'s1': 5, 's2': 5})
        self.assertEqual({c['id']: c['used'] for c in status['channels']}, {'e1': 10})
        self.assertEqual(len(status['deliveries']), 2)


if __name__ == '__main__':
    unittest.main()
