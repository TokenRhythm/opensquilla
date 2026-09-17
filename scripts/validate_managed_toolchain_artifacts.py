#!/usr/bin/env python3
"""Install and smoke-test the real pinned managed-toolchain artifacts.

This is intentionally an opt-in release/CI check rather than an offline unit
test: it downloads the catalog artifacts and, on macOS, may install the pinned
Homebrew formula selected by the catalog.  Use an isolated root so validation
receipts and extracted payloads never enter the normal user state directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from opensquilla.skills.toolchains import (
    component_ids,
    describe_component,
    install_component,
    invalidate_probe_cache,
    probe_component,
)

_VALIDATION_ROOT_ENV = "OPENSQUILLA_TOOLCHAIN_VALIDATION_ROOT"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download, verify, install, and smoke-test pinned toolchain artifacts."
    )
    parser.add_argument(
        "--component",
        action="append",
        dest="components",
        choices=component_ids(),
        help="Component to validate; repeat as needed (default: every component).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help=(
            "Isolated validation root. Defaults to "
            f"${_VALIDATION_ROOT_ENV} or the system temporary directory."
        ),
    )
    parser.add_argument(
        "--expect-platform-key",
        help=(
            "Fail unless native host detection resolves this catalog platform key. "
            "This assertion never overrides platform detection."
        ),
    )
    parser.add_argument(
        "--check-runtime-hot-path",
        action="store_true",
        help=(
            "After install/probe, prove that repeated managed_env calls use the "
            "bounded activation cache instead of reparsing or walking the payload."
        ),
    )
    return parser


def _validation_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = os.environ.get(_VALIDATION_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(tempfile.gettempdir()).resolve() / "opensquilla-toolchain-artifact-validation"


def _progress(component_id: str):
    last_bucket = -1

    def report(downloaded: int, total: int) -> None:
        nonlocal last_bucket
        if total <= 0:
            return
        bucket = min(10, max(0, int(downloaded * 10 / total)))
        if bucket == last_bucket:
            return
        last_bucket = bucket
        print(
            json.dumps(
                {
                    "event": "download_progress",
                    "component_id": component_id,
                    "downloaded": downloaded,
                    "total": total,
                    "percent": bucket * 10,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    return report


def _is_beneath(path: Path, root: Path) -> bool:
    candidate = os.path.normcase(os.path.abspath(path))
    selected_root = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath((candidate, selected_root)) == selected_root
    except ValueError:
        return False


def _check_runtime_hot_path(component_id: str, *, root: Path) -> bool:
    """Exercise the real activation twice and reject unbounded warm work."""

    from opensquilla.skills.toolchains import runtime

    descriptor = describe_component(component_id)
    active_path = root / "active" / f"{component_id}.json"
    try:
        receipt = json.loads(active_path.read_text(encoding="utf-8"))
        package_relpath = receipt["package_relpath"]
        resources = receipt["resources"]
        if not isinstance(package_relpath, str) or not isinstance(resources, dict):
            raise ValueError("activation receipt has invalid runtime fields")
        package = root.joinpath(*Path(package_relpath).parts).resolve(strict=True)
        package.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "event": "runtime_hot_path_result",
                    "component_id": component_id,
                    "ready": False,
                    "reason": f"activation receipt unavailable: {exc}",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return False

    counts = {
        "cold_lstat": 0,
        "cold_stat": 0,
        "cold_mapping_reads": 0,
        "cold_payload_validations": 0,
        "warm_lstat": 0,
        "warm_stat": 0,
        "warm_mapping_reads": 0,
        "warm_payload_validations": 0,
    }
    phase = "cold"
    real_lstat = Path.lstat
    real_stat = Path.stat
    real_read_mapping = runtime._read_mapping
    real_payload_matches = runtime.package_payload_matches

    def tracked_lstat(path: Path) -> os.stat_result:
        if _is_beneath(path, package):
            counts[f"{phase}_lstat"] += 1
        return real_stat(path, follow_symlinks=False)

    def tracked_stat(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if _is_beneath(path, package):
            counts[f"{phase}_stat"] += 1
        return real_stat(path, follow_symlinks=follow_symlinks)

    def tracked_read_mapping(path: Path):
        counts[f"{phase}_mapping_reads"] += 1
        return real_read_mapping(path)

    def tracked_payload_matches(package_path: Path, selected_descriptor):
        counts[f"{phase}_payload_validations"] += 1
        return real_payload_matches(package_path, selected_descriptor)

    warm_iterations = 3
    # Per call, the cache rechecks only package-local fixed sentinels. managed_env
    # then resolves and verifies the selected font path, which performs at most
    # two containment stats plus the final file stat on CPython 3.12.
    sentinel_budget_per_call = (
        2
        + len(descriptor.bin_relpaths)
        + len(descriptor.probe_commands)
        + len(resources)
    )
    resource_stat_budget_per_call = 3 if "noto-cjk-font" in resources else 0
    runtime.invalidate_payload_validation_cache(component_id, root=root)
    setattr(Path, "lstat", tracked_lstat)
    setattr(Path, "stat", tracked_stat)
    runtime._read_mapping = tracked_read_mapping
    runtime.package_payload_matches = tracked_payload_matches
    reason = ""
    try:
        cold_env = runtime.managed_env({"PATH": ""}, root=root)
        for command in descriptor.probe_commands:
            if not command:
                reason = "catalog contains an empty probe command"
                break
            if shutil.which(command[0], path=cold_env.get("PATH", "")) is None:
                reason = f"cold managed_env omitted probe executable: {command[0]}"
                break
        phase = "warm"
        for _ in range(warm_iterations):
            warm_env = runtime.managed_env({"PATH": ""}, root=root)
            if warm_env.get("PATH") != cold_env.get("PATH"):
                reason = "warm managed_env changed the activated PATH"
                break
    except Exception as exc:
        reason = f"runtime hot-path check failed: {type(exc).__name__}: {exc}"
    finally:
        runtime.package_payload_matches = real_payload_matches
        runtime._read_mapping = real_read_mapping
        setattr(Path, "stat", real_stat)
        setattr(Path, "lstat", real_lstat)

    stat_budget = (
        sentinel_budget_per_call + resource_stat_budget_per_call
    ) * warm_iterations
    lstat_budget = sentinel_budget_per_call * warm_iterations
    if not reason and counts["cold_lstat"] == 0:
        reason = "cold validation did not inspect the package payload"
    if not reason and counts["cold_payload_validations"] != 1:
        reason = "cold activation did not perform exactly one complete payload validation"
    if not reason and not 0 < counts["warm_lstat"] <= lstat_budget:
        reason = "warm validation exceeded the bounded activation-sentinel lstat budget"
    if not reason and counts["warm_mapping_reads"] != 0:
        reason = "warm validation reparsed an activation receipt or package marker"
    if not reason and counts["warm_payload_validations"] != 0:
        reason = "warm validation repeated complete payload validation"
    if not reason and counts["warm_stat"] > stat_budget:
        reason = "warm validation exceeded the bounded activation-sentinel budget"

    ready = not reason
    print(
        json.dumps(
            {
                "event": "runtime_hot_path_result",
                "component_id": component_id,
                "ready": ready,
                "reason": reason or None,
                "warm_iterations": warm_iterations,
                "warm_lstat_budget": lstat_budget,
                "warm_stat_budget": stat_budget,
                **counts,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return ready


def validate(
    components: Sequence[str],
    *,
    root: Path,
    expected_platform_key: str | None = None,
    check_runtime_hot_path: bool = False,
) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for component_id in components:
        descriptor = describe_component(component_id)
        if (
            expected_platform_key is not None
            and descriptor.platform_key != expected_platform_key
        ):
            print(
                json.dumps(
                    {
                        "event": "platform_mismatch",
                        "component_id": component_id,
                        "actual_platform_key": descriptor.platform_key,
                        "expected_platform_key": expected_platform_key,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 1
        print(
            json.dumps(
                {
                    "event": "validation_start",
                    "component_id": component_id,
                    "platform_key": descriptor.platform_key,
                    "version": descriptor.version,
                    "download_size_bytes": descriptor.total_download_size,
                    "install_backend": descriptor.install_backend,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        receipt = install_component(
            component_id,
            root=root,
            progress_cb=_progress(component_id),
        )
        invalidate_probe_cache(component_id)
        report = probe_component(component_id, root=root)
        print(
            json.dumps(
                {
                    "event": "validation_result",
                    "component_id": component_id,
                    "platform_key": report.platform_key,
                    "version": report.version,
                    "ready": report.ready,
                    "reason": report.reason,
                    "receipt_id": receipt.receipt_id,
                    "artifact_sha256": receipt.sha256,
                    "install_backend": receipt.install_backend,
                    "binaries": sorted(report.binaries),
                    "resources": sorted(report.resources),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if not report.ready:
            return 1
        if check_runtime_hot_path and not _check_runtime_hot_path(component_id, root=root):
            return 1
    return 0


def main() -> int:
    args = _parser().parse_args()
    components = tuple(args.components or component_ids())
    root = _validation_root(args.root)
    print(json.dumps({"event": "validation_root", "path": str(root)}, sort_keys=True))
    return validate(
        components,
        root=root,
        expected_platform_key=args.expect_platform_key,
        check_runtime_hot_path=args.check_runtime_hot_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
