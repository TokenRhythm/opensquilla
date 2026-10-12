"""Active-bundle pointer, atomic promotion, quarantine, and rollback.

The shipped baseline under ``site-packages`` is never mutated. A single pointer
file ``~/.opensquilla/router/active`` selects which bundle the router loads:

    baseline            -> the packaged/configured base bundle
    learned/<version>   -> a promoted candidate under router/learned/<version>

Promotion and rollback are just atomic rewrites of that one pointer, so a bad
candidate is reverted by pointing back to ``baseline``. A promoted candidate is
additionally pinned to the base bundle it was trained from
(``learned_manifest.json: base_fingerprint``); when a package upgrade replaces
the shipped weights, :func:`verify_active_bundle` detects the mismatch, resets
the pointer, and quarantines the stale candidate instead of serving a hybrid of
new projections and an old head.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.squilla_router.self_learning.store import router_data_root

log = structlog.get_logger(__name__)

_BASELINE = "baseline"


def active_pointer_path(home: Path | None = None) -> Path:
    return router_data_root(home) / "active"


def learned_root(home: Path | None = None) -> Path:
    return router_data_root(home) / "learned"


def learned_bundle_dir(version: str, home: Path | None = None) -> Path:
    return learned_root(home) / version


def read_active(home: Path | None = None) -> str:
    path = active_pointer_path(home)
    if not path.is_file():
        return _BASELINE
    value = path.read_text(encoding="utf-8").strip()
    return value or _BASELINE


def write_active_atomic(value: str, home: Path | None = None) -> None:
    """Atomically set the active pointer (write temp + os.replace)."""

    path = active_pointer_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def resolve_active_bundle_dir(home: Path | None = None) -> Path | None:
    """Return the learned bundle dir to load, or ``None`` to use the baseline.

    Falls back to baseline if the pointer references a missing/incomplete bundle.
    """

    active = read_active(home)
    if active == _BASELINE:
        return None
    if active.startswith("learned/"):
        version = active.split("/", 1)[1]
        bundle = learned_bundle_dir(version, home)
        if (bundle / "lgbm_main.bin").is_file():
            return bundle
    return None


@dataclass
class ActiveBundleCheck:
    """Outcome of verifying the active learned bundle against the base."""

    detached: bool  # True when the candidate was reset to baseline
    version: str | None = None
    reason: str | None = None  # "base_upgraded"
    pinned_fingerprint: str | None = None
    current_fingerprint: str | None = None


# The strategy cache key calls verify_active_bundle on every turn, and hashing
# a 39MB model per turn would put real IO on the hot path. Two memo layers:
# the fingerprint itself is cached by the base file's (path, mtime_ns, size)
# stat — one cheap stat per turn, one hash per actual file change — and the
# verification verdict is cached per (home, pointer, fingerprint) so the
# manifest parse also runs once per swap/upgrade rather than per turn.
_verify_lock = threading.Lock()
_verify_key: tuple[str, str, str | None] | None = None
_verify_result: ActiveBundleCheck | None = None
_fp_cache: tuple[tuple[str, int, int], str] | None = None


def _cached_base_fingerprint(base_dir: Path) -> str | None:
    """``base_bundle_fingerprint`` memoized on the file's stat signature."""

    global _fp_cache

    path = base_dir / "lgbm_main.bin"
    try:
        st = path.stat()
    except OSError:
        return None
    stat_key = (str(path), st.st_mtime_ns, st.st_size)
    with _verify_lock:
        if _fp_cache is not None and _fp_cache[0] == stat_key:
            return _fp_cache[1]

    from opensquilla.squilla_router.self_learning.train import base_bundle_fingerprint

    fp = base_bundle_fingerprint(base_dir)
    if fp is None:
        return None
    with _verify_lock:
        _fp_cache = (stat_key, fp)
    return fp


def _learned_manifest_fingerprint(bundle: Path) -> str | None:
    """Return the base fingerprint pinned in a bundle's manifest, if any.

    ``None`` covers a missing/unreadable manifest and a manifest without the
    pin (pre-fingerprint candidates). All of those are *trusted*: the detach
    guard acts only on positive evidence of a base upgrade, never on absence
    of evidence — genuinely broken bundles are the load-failure fallback
    chain's job, and legacy candidates must not be mass-detached the first
    time this code runs.
    """

    manifest = bundle / "learned_manifest.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("base_fingerprint")
    return str(value) if value else None


def verify_active_bundle(
    base_dir: Path,
    home: Path | None = None,
    *,
    force: bool = False,
) -> ActiveBundleCheck:
    """Detach the active learned bundle when its base has changed underneath it.

    Compares the fingerprint pinned at training time against the current base
    bundle. On mismatch (a package upgrade replaced the shipped weights) the
    pointer is reset to baseline and the stale candidate quarantined — its
    symlinked projections now belong to a different model generation than the
    retrained head, and that hybrid must never serve traffic. The caller
    (engine strategy resolution) treats a detach like any other pointer state:
    the next load simply uses the baseline. Fail-open: errors leave the pointer
    untouched.
    """

    global _verify_key, _verify_result

    active = read_active(home)
    if not active.startswith("learned/"):
        return ActiveBundleCheck(detached=False)

    try:
        current_fp = _cached_base_fingerprint(base_dir)
    except Exception:  # noqa: BLE001 — fingerprinting must not break routing
        return ActiveBundleCheck(detached=False)

    home_key = str(home) if home is not None else ""
    with _verify_lock:
        key = (home_key, active, current_fp)
        if not force and _verify_key == key and _verify_result is not None:
            return _verify_result

        version = active.split("/", 1)[1]
        bundle = learned_bundle_dir(version, home)
        pinned_fp = _learned_manifest_fingerprint(bundle)

        detach_reason: str | None = None
        if pinned_fp is not None and current_fp is not None and pinned_fp != current_fp:
            detach_reason = "base_upgraded"

        if detach_reason is None:
            result = ActiveBundleCheck(
                detached=False,
                version=version,
                pinned_fingerprint=pinned_fp,
                current_fingerprint=current_fp,
            )
            _verify_key, _verify_result = key, result
            return result

        try:
            rollback_active(home)
            quarantine_candidate(version, home)
        except Exception as exc:  # noqa: BLE001 — never take routing down
            # NOT memoized: the pointer still names a bundle that is known to
            # be stale, so every subsequent turn must retry the detach until
            # it succeeds rather than trusting the hybrid until restart.
            log.warning(
                "router_self_learning.detach_failed", version=version, error=str(exc)
            )
            return ActiveBundleCheck(detached=False, version=version)

        log.warning(
            "router_self_learning.base_upgraded",
            version=version,
            pinned_fingerprint=pinned_fp,
            current_fingerprint=current_fp,
            action="reset_to_baseline_and_quarantined",
        )
        result = ActiveBundleCheck(
            detached=True,
            version=version,
            reason=detach_reason,
            pinned_fingerprint=pinned_fp,
            current_fingerprint=current_fp,
        )
        # Key by the *post-detach* pointer so the next call short-circuits.
        _verify_key, _verify_result = (
            home_key,
            read_active(home),
            current_fp,
        ), ActiveBundleCheck(detached=False)
        return result


def promote_candidate(version: str, home: Path | None = None) -> str:
    """Point active at ``learned/<version>``. Returns the previous pointer value."""

    previous = read_active(home)
    write_active_atomic(f"learned/{version}", home)
    return previous


def rollback_active(home: Path | None = None, *, to: str = _BASELINE) -> str:
    """Revert the active pointer (default: back to baseline). Returns previous."""

    previous = read_active(home)
    write_active_atomic(to, home)
    return previous


def quarantine_candidate(version: str, home: Path | None = None) -> Path | None:
    """Move a rejected/bad candidate out of ``learned/`` so it is never loaded."""

    src = learned_bundle_dir(version, home)
    if not src.exists():
        return None
    dest_root = learned_root(home) / ".quarantine"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / version
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(src), str(dest))
    return dest


def should_rollback(
    *,
    pre_complaint_rate: float | None,
    post_complaint_rate: float,
    post_n: int,
    config: Any,
    pre_downvote_rate: float | None = None,
    post_downvote_rate: float = 0.0,
    post_feedback_n: int = 0,
) -> bool:
    """Decide whether a live candidate has regressed enough to revert.

    Two independent triggers, either of which reverts:

    * Complaint-rate regression over the pre-swap baseline, after a minimum
      post-swap sample count (the original M4 monitor).
    * Explicit down-vote-rate regression (single-model feedback only —
      ensemble ratings co-vary with candidate/aggregator changes, not the
      promoted classifier). Feedback is far sparser than samples, so it has
      its own minimum count and a wider delta.
    """

    if not bool(getattr(config, "auto_rollback", True)):
        return False

    if pre_complaint_rate is not None and post_n >= int(
        getattr(config, "min_monitor_samples", 30)
    ):
        delta = float(getattr(config, "complaint_regression_delta", 0.05))
        if post_complaint_rate > pre_complaint_rate + delta:
            return True

    if pre_downvote_rate is not None and post_feedback_n >= int(
        getattr(config, "min_feedback_monitor_samples", 5)
    ):
        fb_delta = float(getattr(config, "downvote_regression_delta", 0.15))
        if post_downvote_rate > pre_downvote_rate + fb_delta:
            return True

    return False
