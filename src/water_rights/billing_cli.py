"""Command line for the standalone time-of-use water billing audit.

Usage::

    python -m water_rights.billing_cli --source INPUT --output REPORT

Business errors (contract violations, infeasible cycles) exit with code 2 and
never touch an existing report.  The output must differ from the input and may
not be a symbolic link.
"""
import argparse
import json
import sys
from pathlib import Path

from .billing_pipeline import audit
from .common import read, save

DESCRIPTION = 'Exact feasible bill bounds from cumulative meter registers'


def run(source):
    data, digest = read(source)
    report = audit(data)
    report['source_sha256'] = digest
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
