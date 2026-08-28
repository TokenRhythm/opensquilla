"""Offline LightGBM (re)training and candidate-bundle assembly.

Phase 1 retrains only the LightGBM tabular head, incrementally from the shipped
``lgbm_main.bin`` via ``init_model`` (continue boosting) to avoid catastrophic
forgetting; if the base model is absent/unloadable it trains fresh. The MLP head
and all fitted projections are reused unchanged, so a candidate bundle is the
base bundle with one swapped file.

``lightgbm`` is an optional (``recommended``) dependency, imported lazily so the
base install and the runtime hot path never require it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from opensquilla.squilla_router.self_learning.dataset import TrainingDataset

_NUM_CLASS = 4
_LGBM_FILENAME = "lgbm_main.bin"
_ARTIFACT_MANIFEST = "artifact_manifest.json"


@dataclass
class CandidateInfo:
    """Metadata describing a built candidate bundle."""

    version: str
    bundle_dir: str
    feature_schema_version: str
    parent_version: str | None
    trained_at: str
    n_samples: int
    n_sessions: int
    used_init_model: bool
    class_distribution: dict[int, int]
    cv_metrics: dict[str, Any] | None = None
    # Fingerprint of the base bundle this candidate was built against. When an
    # upgrade replaces the shipped bundle, the mismatch detaches the candidate
    # (its symlinked artifacts now point at different files than the head was
    # trained with). Optional so pre-existing manifests keep loading.
    base_fingerprint: str | None = None


def base_bundle_fingerprint(base_dir: Path) -> str | None:
    """Fingerprint the base bundle a candidate is built from.

    ``sha256(lgbm_main.bin)`` — the one artifact every retrain both consumes
    (``init_model``) and replaces. Version metadata is deliberately not used:
    ``version.json`` may be absent or unchanged across a weight refresh, while
    the binary hash cannot lie. Returns ``None`` when the base model is missing
    (trained-fresh candidates have nothing to pin against).
    """

    path = base_dir / _LGBM_FILENAME
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lgbm_params(config: Any) -> dict[str, Any]:
    return {
        "objective": "multiclass",
        "num_class": _NUM_CLASS,
        "learning_rate": float(getattr(config, "learning_rate", 0.05)),
        "num_leaves": int(getattr(config, "num_leaves", 31)),
        "min_data_in_leaf": int(getattr(config, "min_data_in_leaf", 5)),
        "verbosity": -1,
        "deterministic": True,
        "force_col_wise": True,
    }


def train_booster_arrays(
    x: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    *,
    base_model_path: Path | None,
    config: Any,
):
    """Train (or continue) a LightGBM booster from arrays. Returns ``(booster, used_init)``."""

    import lightgbm as lgb

    if x.shape[0] == 0:
        raise ValueError("cannot train on an empty dataset")

    train_set = lgb.Dataset(
        x.astype(np.float64),
        label=y.astype(np.int64),
        weight=w.astype(np.float64),
        free_raw_data=False,
    )

    init_model = None
    used_init = False
    if base_model_path is not None and base_model_path.is_file():
        try:
            init_model = lgb.Booster(model_file=str(base_model_path))
            used_init = True
        except (lgb.basic.LightGBMError, OSError):
            # Base unloadable (e.g. LFS pointer / version skew) -> train fresh.
            init_model = None
            used_init = False

    booster = lgb.train(
        _lgbm_params(config),
        train_set,
        num_boost_round=int(getattr(config, "num_boost_round", 60)),
        init_model=init_model,
        keep_training_booster=True,
    )
    return booster, used_init


def train_booster(dataset: TrainingDataset, *, base_model_path: Path | None, config: Any):
    """Train (or continue) a booster from a :class:`TrainingDataset`."""

    return train_booster_arrays(
        dataset.X, dataset.y, dataset.w, base_model_path=base_model_path, config=config
    )


def _rewrite_artifact_manifest(out_dir: Path) -> None:
    """Update the copied manifest's ``lgbm_main.bin`` entry for the new head.

    The base manifest pins size/sha256 of the *shipped* model; the runtime's
    bundle validation would otherwise reject every retrained candidate as an
    incomplete artifact. Only the swapped file's entry is touched.
    """

    manifest_path = out_dir / _ARTIFACT_MANIFEST
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lgbm_path = out_dir / _LGBM_FILENAME
    data = lgbm_path.read_bytes()
    for entry in manifest.get("files", []):
        if str(entry.get("path")) == _LGBM_FILENAME:
            if "size_bytes" in entry:
                entry["size_bytes"] = len(data)
            if "sha256" in entry:
                entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["source_note"] = "Self-learning retrained LightGBM head."
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def assemble_bundle(base_dir: Path, out_dir: Path, new_lgbm_path: Path) -> None:
    """Materialize a candidate bundle: reuse base artifacts, swap the LGBM head.

    Unchanged entries are symlinked (tiny, instant candidates); the retrained
    ``lgbm_main.bin`` is a real copy, and the artifact manifest is copied (not
    linked) so its entry can be rewritten for the new head. Symlinks fall back
    to copies where the OS forbids them (e.g. unprivileged Windows).
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in base_dir.iterdir():
        if entry.name == _LGBM_FILENAME:
            continue
        target = out_dir / entry.name
        if target.exists() or target.is_symlink():
            continue
        if entry.name == _ARTIFACT_MANIFEST:
            shutil.copy2(entry, target)
            continue
        try:
            target.symlink_to(entry.resolve())
        except OSError:
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
    shutil.copy2(new_lgbm_path, out_dir / _LGBM_FILENAME)
    _rewrite_artifact_manifest(out_dir)


def build_candidate_bundle(
    dataset: TrainingDataset,
    *,
    base_dir: Path,
    learned_root: Path,
    config: Any,
    parent_version: str | None = None,
) -> CandidateInfo:
    """Train a candidate and write a complete, loadable bundle. Returns metadata."""

    trained_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = f"{dataset.feature_schema_version}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    out_dir = learned_root / version

    # Cross-validate (rolling holdout) before fitting the shipped model, so the
    # promotion gate sees an honest held-out estimate. Deferred import avoids a
    # train<->evaluate import cycle.
    from opensquilla.squilla_router.self_learning.evaluate import cross_validate

    cv_metrics = cross_validate(dataset, config=config)

    # Fingerprint before training: init_model reads the same file, so hashing
    # first guarantees the pin describes the exact bytes the head continued from.
    base_fp = base_bundle_fingerprint(base_dir)

    booster, used_init = train_booster(
        dataset, base_model_path=base_dir / _LGBM_FILENAME, config=config
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_lgbm = out_dir / f"{_LGBM_FILENAME}.tmp"
    booster.save_model(str(tmp_lgbm))
    assemble_bundle(base_dir, out_dir, tmp_lgbm)
    tmp_lgbm.unlink(missing_ok=True)

    info = CandidateInfo(
        version=version,
        bundle_dir=str(out_dir),
        feature_schema_version=dataset.feature_schema_version,
        parent_version=parent_version,
        trained_at=trained_at,
        n_samples=len(dataset),
        n_sessions=dataset.n_sessions,
        used_init_model=used_init,
        class_distribution=dataset.class_distribution(),
        cv_metrics=cv_metrics,
        base_fingerprint=base_fp,
    )
    (out_dir / "learned_manifest.json").write_text(
        json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return info
