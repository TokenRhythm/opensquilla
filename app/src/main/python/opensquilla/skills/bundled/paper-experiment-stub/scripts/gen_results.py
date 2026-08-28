"""Stub experiment generator: deterministic CSV seeded from topic hash."""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
from pathlib import Path


def _seed_from_topic(topic: str) -> int:
    digest = hashlib.sha256(topic.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rng = random.Random(_seed_from_topic(args.topic))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["x", "y_baseline", "y_ours"])
        for x in range(1, 21):
            baseline = rng.uniform(0.40, 0.55) + x * 0.005
            improvement = rng.uniform(0.05, 0.15)
            ours = min(baseline + improvement, 0.99)
            writer.writerow([x, round(baseline, 4), round(ours, 4)])
    print(f"wrote {out_path} (20 rows)")


if __name__ == "__main__":
    main()
