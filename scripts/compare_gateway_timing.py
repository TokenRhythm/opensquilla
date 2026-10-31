"""Opt-in same-Python-version comparison using the offline Gateway fixture.

Source directories must already exist. This tool never creates checkouts or reads
provider credentials. By default dependency inputs must match; an explicit
baseline interpreter allows each source to use its own locked environment.
Durations cover process-to-health and Default send-to-done,
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


def validate_sources(
    baseline: Path, candidate: Path, samples: int, *, separate_environments: bool = False,
) -> None:
    if not 1 <= samples <= 10:
        raise ValueError("samples must be between 1 and 10")
    for root in (baseline, candidate):
        if not (root / "src/opensquilla/gateway/boot.py").is_file():
            raise ValueError("source root must contain the Gateway package")
    for name in ("uv.lock", "pyproject.toml"):
        if not separate_environments and (
            (baseline / name).read_bytes() != (candidate / name).read_bytes()
        ):
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


def _dependency_inputs(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("uv.lock", "pyproject.toml")
    }


def _interpreter_identity(executable: str) -> dict[str, Any]:
    # Isolated startup excludes inherited PYTHONPATH and user-site packages.
    # Prefix is compared privately and never included in the report.
    code = (
        "import json,platform,sys; "
        "print(json.dumps({'version':list(sys.version_info[:3]),"
        "'implementation':sys.implementation.name,'machine':platform.machine(),"
        "'prefix':sys.prefix}))"
    )
    return json.loads(subprocess.check_output([executable, "-I", "-c", code], text=True))


def compare(
    *, baseline: Path, candidate: Path, baseline_sha: str, candidate_sha: str, samples: int,
    baseline_python: Path | None = None,
) -> dict[str, Any]:
    validate_sources(
        baseline, candidate, samples, separate_environments=baseline_python is not None,
    )
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (baseline_sha, candidate_sha)):
        raise ValueError("source SHAs must be full lowercase commit hashes")
    verify_source_identity(baseline, baseline_sha, archived=True)
    verify_source_identity(candidate, candidate_sha)
    dependency_inputs = {
        label: _dependency_inputs(root)
        for label, root in (("baseline", baseline), ("candidate", candidate))
    }
    interpreters = {"baseline": str(baseline_python or sys.executable), "candidate": sys.executable}
    identities = {
        label: _interpreter_identity(executable) for label, executable in interpreters.items()
    }
    for field in ("version", "implementation", "machine"):
        if identities["baseline"][field] != identities["candidate"][field]:
            raise ValueError(f"comparison requires identical Python {field}")
    if (
        dependency_inputs["baseline"] != dependency_inputs["candidate"]
        and Path(identities["baseline"]["prefix"]).resolve()
        == Path(identities["candidate"]["prefix"]).resolve()
    ):
        raise ValueError("different dependency inputs require separate Python environments")
    for identity in identities.values():
        identity.pop("prefix")
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
                    [interpreters[label], str(harness), "--sample-dir", str(profile / "sample"),
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
        "dependency_mode": "per-source-environments" if baseline_python else "shared-environment",
        "dependency_inputs": dependency_inputs, "interpreters": identities,
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
    parser.add_argument(
        "--baseline-python", type=Path,
        help="Baseline's separately installed locked environment (same Python version)",
    )
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(
        baseline=args.baseline_root.resolve(), candidate=args.candidate_root.resolve(),
        baseline_sha=args.baseline_sha, candidate_sha=args.candidate_sha, samples=args.samples,
        baseline_python=args.baseline_python,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
