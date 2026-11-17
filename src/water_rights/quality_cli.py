"""Command line for the persistent water-quality exposure warehouse.

Usage::

    python -m water_rights.quality_cli init   --source CONFIG --db DB
    python -m water_rights.quality_cli ingest --source CHANGES --db DB
    python -m water_rights.quality_cli report --db DB --start S --end E --output REPORT

The database is a new, independent warehouse.  ``init`` refuses to overwrite
an existing file.  ``ingest`` commits accepted facts, all derived intervals and
the input breakpoint in one transaction; replaying already committed seqs is
idempotent.  ``report`` only reads the committed derived snapshot; it never
rescans the raw change stream.  Business errors exit 2 and never touch an
existing report or database.
"""
import argparse
import json
import sys
from pathlib import Path

from .quality_warehouse import ingest_changes, init_warehouse, publish_report

DESCRIPTION = 'Persistent continuous water-quality exposure warehouse'


def _init(args):
    return init_warehouse(args.source, args.db)


def _ingest(args):
    return ingest_changes(args.source, args.db)


def _report(args):
    return publish_report(args.db, args.start, args.end, args.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='create a new warehouse')
    init.add_argument('--source', type=Path, required=True)
    init.add_argument('--db', type=Path, required=True)
    ingest = commands.add_parser('ingest', help='apply one change file')
    ingest.add_argument('--source', type=Path, required=True)
    ingest.add_argument('--db', type=Path, required=True)
    report = commands.add_parser('report', help='render a query report')
    report.add_argument('--db', type=Path, required=True)
    report.add_argument('--start', type=int, required=True)
    report.add_argument('--end', type=int, required=True)
    report.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    handlers = {'init': _init, 'ingest': _ingest, 'report': _report}
    try:
        result = handlers[args.command](args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
