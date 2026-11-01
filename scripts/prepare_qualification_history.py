"""Prepare offline history requests from qualified metadata and reviewed identities."""

import argparse
from pathlib import Path

from stock_analyser.qualification_history import prepare_history_inputs
from stock_analyser.qualification_workflow import WorkloadManifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enrichment-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--instruments",
        type=Path,
        required=True,
        help="Private JSON object keyed by reference ticker, with reviewed Instrument values",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = WorkloadManifest.model_validate_json(args.manifest.read_text())
        path = prepare_history_inputs(args.enrichment_dir, manifest, args.instruments, args.output)
    except Exception:
        parser.exit(
            1,
            "Preparation blocked. Check rights, metadata, identities and the new output path.\n",
        )
    print(f"History requests saved: {path}")
    print(f"Review the exact manifest before measurement: {args.output / 'history-workload.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
