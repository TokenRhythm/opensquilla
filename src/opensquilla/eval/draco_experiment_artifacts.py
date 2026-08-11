"""Private, crash-consistent DRACO experiment-config artifact publication."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_io import fsync_directory
from opensquilla.eval.draco_experiment_config import DracoExperimentConfigBundle


def publish_experiment_config_artifacts(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
    canonical_json_sha256: Callable[[Any], str],
    replay_validation_contract: Callable[[], dict[str, Any]],
) -> dict[str, str]:
    """Publish private effective config artifacts without overwriting paths."""

    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    if not isinstance(bundle, DracoExperimentConfigBundle):
        return {}

    effective_config = bundle.config.model_dump(mode="json")
    effective_path = output_dir / f"draco_run_{stamp}.experiment-config.effective.json"
    effective_resolved_path = effective_path.expanduser().resolve()
    artifacts = {"experiment_config_effective_json": str(effective_path)}
    artifact_payloads: list[tuple[Path, Any]] = [(effective_path, effective_config)]
    g1_contract = getattr(args, "_g1_registry_contract", None)
    ranking_resolution = (
        g1_contract.get("ranking_config_resolution")
        if isinstance(g1_contract, Mapping)
        else None
    )
    if isinstance(ranking_resolution, Mapping):
        ranking_effective = ranking_resolution.get("effective_config")
        if isinstance(ranking_effective, Mapping):
            ranking_path = output_dir / (
                f"draco_run_{stamp}.experiment-config.ranking-effective.json"
            )
            artifacts["ranking_config_effective_json"] = str(ranking_path)
            artifact_payloads.append((ranking_path, dict(ranking_effective)))
    ranking_hashes = (
        {
            str(key): value
            for key, value in ranking_resolution.items()
            if isinstance(key, str)
            and "sha256" in key.casefold()
            and isinstance(value, str)
        }
        if isinstance(ranking_resolution, Mapping)
        else {}
    )
    resolution = {
        "profile_id": bundle.config.profile_id,
        "provenance": bundle.provenance(),
        "effective_config": {
            "path": str(effective_resolved_path),
            "sha256": canonical_json_sha256(effective_config),
        },
        "ranking_config_hashes": ranking_hashes,
        "input_validation": getattr(args, "_draco_input_validation", None),
        "artifact_keys": sorted([*artifacts, "experiment_config_resolution_json"]),
        "replay_validation": replay_validation_contract(),
    }
    resolution_path = output_dir / f"draco_run_{stamp}.experiment-config.resolution.json"
    artifacts["experiment_config_resolution_json"] = str(resolution_path)
    artifact_payloads.append((resolution_path, resolution))

    staged: list[tuple[Path, Path]] = []
    published: list[tuple[Path, Path]] = []
    try:
        for path, payload in artifact_payloads:
            document = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            fd: int | None = None
            created = False
            try:
                fd = os.open(temporary, flags, 0o600)
                created = True
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = None
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                if fd is not None:
                    os.close(fd)
                if created:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                raise
            staged.append((path, temporary))

        for path, temporary in staged:
            os.link(temporary, path, follow_symlinks=False)
            published.append((path, temporary))
    except BaseException:
        for path, temporary in reversed(published):
            try:
                published_stat = os.stat(path, follow_symlinks=False)
                temporary_stat = os.stat(temporary, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                published_stat.st_dev == temporary_stat.st_dev
                and published_stat.st_ino == temporary_stat.st_ino
            ):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        raise
    finally:
        for _, temporary in staged:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        fsync_directory(output_dir)

    args._effective_experiment_config_path = effective_resolved_path
    return artifacts
