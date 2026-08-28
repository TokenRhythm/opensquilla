"""Plot results.csv as a two-line matplotlib PDF."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs: list[float] = []
    baseline: list[float] = []
    ours: list[float] = []
    with Path(args.csv).open(encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            xs.append(float(row["x"]))
            baseline.append(float(row["y_baseline"]))
            ours.append(float(row["y_ours"]))

    fig, ax = plt.subplots(figsize=(4.5, 3.0))
    ax.plot(xs, baseline, label="baseline", marker="o", linewidth=1.2)
    ax.plot(xs, ours, label="ours", marker="^", linewidth=1.2)
    ax.set_xlabel("step")
    ax.set_ylabel("score")
    ax.set_title("baseline vs. ours")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
