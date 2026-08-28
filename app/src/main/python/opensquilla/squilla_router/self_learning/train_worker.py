"""Subprocess entry point: train one candidate bundle from an exported dataset.

Run as ``python -m opensquilla.squilla_router.self_learning.train_worker`` by
:func:`orchestrator.subprocess_trainer`. Loads the npz dataset, trains, writes the
candidate bundle, and prints a one-line JSON ``{version, bundle_dir}`` to stdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from opensquilla.squilla_router.self_learning.dataset import TrainingDataset
from opensquilla.squilla_router.self_learning.train import build_candidate_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a router self-learning candidate bundle")
    parser.add_argument("--dataset", required=True, help="path to exported .npz dataset")
    parser.add_argument("--base", required=True, help="base bundle directory")
    parser.add_argument("--learned-root", required=True, help="output root for learned bundles")
    parser.add_argument("--parent-version", default=None)
    parser.add_argument("--num-boost-round", type=int, default=60)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    args = parser.parse_args(argv)

    dataset = TrainingDataset.load_npz(Path(args.dataset))
    config = SimpleNamespace(
        num_boost_round=args.num_boost_round,
        learning_rate=args.learning_rate,
    )
    info = build_candidate_bundle(
        dataset,
        base_dir=Path(args.base),
        learned_root=Path(args.learned_root),
        config=config,
        parent_version=args.parent_version,
    )
    print(json.dumps({"version": info.version, "bundle_dir": info.bundle_dir}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
