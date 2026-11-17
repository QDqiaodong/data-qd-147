"""Command line for the independent terrain drainage base-map product.

Usage::

    python -m water_rights.terrain_cli --source INPUT --output REPORT

The input is an 8 MB restricted JSON raster (elevation and rainfall layers,
equal-area cells, explicit outlets).  The report records the minimum ponding
elevation per cell, the deterministic plateau routing receiver, the final
outlet partition and exact integer runoff/fill volumes in litres.

The product is fully offline: it reads no network dispatch, billing, quality or
permit state, opens no external service and keeps no persistent ledger.  Any
business, validation, computation or I/O failure exits 2 with the reason on
stderr and leaves an existing report byte-for-byte untouched; success exits 0.
"""
import argparse
import json
import sys
from pathlib import Path

from . import common
from .terrain_analysis import analyze, build_report
from .terrain_model import TerrainError, parse_input

DESCRIPTION = 'DEM ponding, plateau routing and catchment partition base map'


def run(source, output):
    """Read, validate, compute and atomically publish one terrain report."""
    source = Path(source)
    output = Path(output)
    # Reject same-path publication before any parsing, including the decoded
    # document being written back to its own path.
    if source.resolve() == output.resolve():
        raise TerrainError('input and output must be different paths')
    data, digest = common.read(source)
    parsed = parse_input(data)
    result = analyze(parsed)
    report = build_report(parsed, digest, result)
    common.save(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.source, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
