"""Offline network allocation and delivery settlement ledger.

Subcommands:
  init    --source INPUT --db DB            create a frozen season ledger
  plan    --db DB --output PLAN             sign a plan for the current balances
  settle  --db DB --plan PLAN --source DELIVERY --delivery-id ID --output RECEIPT
  status  --db DB --output REPORT           cumulative usage, balances, receipts

Business errors exit 2 and preserve existing outputs and the ledger.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import network_ledger

DESCRIPTION = 'Offline network water allocation and delivery settlement ledger'


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)
    p_init = sub.add_parser('init', help='create a frozen season ledger')
    p_init.add_argument('--source', type=Path, required=True)
    p_init.add_argument('--db', type=Path, required=True)
    p_plan = sub.add_parser('plan', help='sign an allocation plan for current balances')
    p_plan.add_argument('--db', type=Path, required=True)
    p_plan.add_argument('--output', type=Path, required=True)
    p_settle = sub.add_parser('settle', help='settle actual deliveries against a plan')
    p_settle.add_argument('--db', type=Path, required=True)
    p_settle.add_argument('--plan', type=Path, required=True)
    p_settle.add_argument('--source', type=Path, required=True)
    p_settle.add_argument('--delivery-id', required=True)
    p_settle.add_argument('--output', type=Path, required=True)
    p_status = sub.add_parser('status', help='cumulative usage, balances and receipts')
    p_status.add_argument('--db', type=Path, required=True)
    p_status.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            report = network_ledger.init_ledger(args.source, args.db)
        elif args.command == 'plan':
            report = network_ledger.issue_plan(args.db, args.output)
        elif args.command == 'settle':
            report, _ = network_ledger.settle(
                args.db, args.plan, args.source, args.delivery_id, args.output)
        else:
            report = network_ledger.status_report(args.db, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except sqlite3.Error as error:
        print('ledger error: ' + str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
