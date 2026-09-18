"""Opt-in same-interpreter comparison using the existing offline Gateway fixture.

Source directories must already exist. This tool never creates checkouts or reads
provider credentials. Durations cover process-to-health and Default send-to-done,
including local client/runtime overhead; they are not model TTFT measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_METRICS = ("startup_to_health_ms", "default_first_turn_ms")


def verify_source_identity(root: Path, sha: str, *, archived: bool = False) -> None:
    """Accept the CI archive's embedded commit identity or an exact clean checkout."""
    marker = root / ".gateway-timing-source-sha"
    if archived and marker.is_file():
        if marker.read_text(encoding="ascii").strip() != sha:
            raise ValueError("baseline archive commit identity does not match")
        return
    top = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True,
    ).strip()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    changed = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        text=True,
    ).strip()
    if Path(top).resolve() != root or actual != sha or changed:
        raise ValueError("source must be the requested exact clean Git checkout")


def validate_sources(baseline: Path, candidate: Path, samples: int) -> None:
    if not 1 <= samples <= 10:
        raise ValueError("samples must be between 1 and 10")
    for root in (baseline, candidate):
        if not (root / "src/opensquilla/gateway/boot.py").is_file():
            raise ValueError("source root must contain the Gateway package")
    for name in ("uv.lock", "pyproject.toml"):
        if (baseline / name).read_bytes() != (candidate / name).read_bytes():
            raise ValueError(f"comparison requires identical {name}")


def sample_environment(root: Path, profile: Path) -> dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP", "TMP",
                "OPENSQUILLA_HOME", "OPENSQUILLA_STATE_DIR", "OPENSQUILLA_LOG_DIR"):
        directory = profile / key.lower()
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory)
    env.update({"PYTHONPATH": str(root / "src"), "PYTHONNOUSERSITE": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    return env


def compare(
    *, baseline: Path, candidate: Path, baseline_sha: str, candidate_sha: str, samples: int,
) -> dict[str, Any]:
    validate_sources(baseline, candidate, samples)
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (baseline_sha, candidate_sha)):
        raise ValueError("source SHAs must be full lowercase commit hashes")
    verify_source_identity(baseline, baseline_sha, archived=True)
    verify_source_identity(candidate, candidate_sha)
    harness = candidate / "tests/functional/test_gateway_silent_reply_process_e2e.py"
    rows: dict[str, list[dict[str, float | int]]] = {"baseline": [], "candidate": []}
    with tempfile.TemporaryDirectory(prefix="opensquilla-gateway-timing-") as temporary:
        scratch = Path(temporary)
        for index in range(samples):
            # Alternate ordering to reduce consistent warm-cache/host-load bias.
            order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for label in order:
                root = baseline if label == "baseline" else candidate
                profile = scratch / f"{label}-{index}"
                env = sample_environment(root, profile)
                result = subprocess.run(
                    [sys.executable, str(harness), "--sample-dir", str(profile / "sample"),
                     "--source-root", str(root)],
                    cwd=root, env=env, capture_output=True, text=True, timeout=90,
                )
                if result.returncode:
                    # The child receives only an isolated synthetic profile. Keep
                    # failure diagnostics in the job log, outside the JSON report.
                    print(result.stderr[-12_000:], file=sys.stderr)
                    raise RuntimeError(f"{label} timing sample {index + 1} failed")
                row = json.loads(result.stdout.strip().splitlines()[-1])
                if set(row) != {*_METRICS, "provider_calls", "terminal_events", "goals"}:
                    raise ValueError("unexpected timing sample fields")
                if (row["provider_calls"], row["terminal_events"], row["goals"]) != (1, 1, 0):
                    raise ValueError("sample must contain exactly one ordinary Default turn")
                for metric in _METRICS:
                    value = row[metric]
                    if not isinstance(value, (int, float)) or not 0 < value < 90_000:
                        raise ValueError("invalid timing duration")
                rows[label].append(row)
    medians = {
        label: {metric: round(statistics.median(row[metric] for row in group), 3)
                for metric in _METRICS}
        for label, group in rows.items()
    }
    return {
        "baseline_sha": baseline_sha, "candidate_sha": candidate_sha,
        "uv_lock_sha256": hashlib.sha256((candidate / "uv.lock").read_bytes()).hexdigest(),
        "pyproject_sha256": hashlib.sha256((candidate / "pyproject.toml").read_bytes()).hexdigest(),
        "samples_per_source": samples, "samples": rows, "median_ms": medians,
        "candidate_over_baseline": {
            metric: round(medians["candidate"][metric] / medians["baseline"][metric], 4)
            for metric in _METRICS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(
        baseline=args.baseline_root.resolve(), candidate=args.candidate_root.resolve(),
        baseline_sha=args.baseline_sha, candidate_sha=args.candidate_sha, samples=args.samples,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
