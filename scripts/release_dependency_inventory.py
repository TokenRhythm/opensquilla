"""Bind wheelhouse and frozen Python dependencies to the release's uv.lock."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import re
import tomllib
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from zipfile import ZipFile


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def locked_versions(repo: Path) -> dict[str, set[str]]:
    lock = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    versions: dict[str, set[str]] = {}
    for package in lock["package"]:
        versions.setdefault(canonical_name(package["name"]), set()).add(package["version"])
    return versions


def require_locked(name: str, version: str, versions: dict[str, set[str]]) -> None:
    if version not in versions.get(canonical_name(name), set()):
        raise ValueError(f"Release dependency does not match uv.lock: {name}=={version}")


def wheel_packages(directory: Path, versions: dict[str, set[str]]) -> list[dict[str, Any]]:
    packages = []
    seen: set[str] = set()
    for wheel in sorted(directory.glob("*.whl")):
        with ZipFile(wheel) as archive:
            metadata_files = [p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ValueError(f"Expected one distribution metadata file in {wheel.name}")
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        name, version = metadata.get("Name", ""), metadata.get("Version", "")
        if not name or not version:
            raise ValueError(f"Missing distribution identity in {wheel.name}")
        name = canonical_name(name)
        require_locked(name, version, versions)
        if name in seen:
            raise ValueError(f"Multiple wheels for the same release dependency: {name}")
        seen.add(name)
        packages.append({
            "name": name, "version": version, "bundled": True,
            "files": [{"path": wheel.name, "sha256": digest(wheel)}],
        })
    if not packages:
        raise ValueError("Wheelhouse dependency inventory is empty")
    return packages


def toc_records(value: object) -> Iterator[tuple[str, str, str]]:
    """Read PyInstaller's literal TOC; never execute generated build inputs."""
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and all(isinstance(item, str) for item in value):
            name, source, kind = value
            if re.fullmatch(r"(?:PYMODULE|PYSOURCE)(?:-[12])?", kind) or kind in {
                "EXTENSION", "BINARY", "DATA"
            }:
                yield name, source, kind
                return
        for item in value:
            yield from toc_records(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from toc_records(item)


def file_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def worker_count(value: str | int) -> int:
    count = int(value)
    if not 1 <= count <= 4:
        raise ValueError("Inventory workers must be between 1 and 4")
    return count


def frozen_packages(
    repo: Path,
    analysis: Path,
    versions: dict[str, set[str]],
    distributions: Iterable[Any] | None = None,
    *,
    workers: int = 1,
) -> list[dict[str, Any]]:
    workers = worker_count(workers)
    # Keep release callers serial by default. Parallel workers only read paths
    # and file bytes; ordered map results preserve package/file ownership order.
    context = ThreadPoolExecutor(max_workers=workers) if workers > 1 else nullcontext()
    with context as pool:
        return _frozen_packages(
            repo, analysis, versions, distributions, map if pool is None else pool.map,
            parallel=pool is not None,
        )


def _frozen_packages(
    repo: Path,
    analysis: Path,
    versions: dict[str, set[str]],
    distributions: Iterable[Any] | None,
    ordered_map: Any,
    *,
    parallel: bool,
) -> list[dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    owners: dict[str, list[tuple[str, str]]] = {}

    owner_inputs: list[tuple[Path, str, str]] = []

    def resolve_owner(item: tuple[Path, str, str]) -> tuple[str, str, str]:
        path, name, relative = item
        return file_key(path), name, relative

    for distribution in (
        importlib.metadata.distributions() if distributions is None else distributions
    ):
        name = canonical_name(distribution.metadata["Name"])
        version = distribution.version
        require_locked(name, version, versions)
        if name in packages:
            if packages[name]["version"] != version:
                raise ValueError(f"Conflicting installed distribution versions: {name}")
            continue
        packages[name] = {"name": name, "version": version, "bundled": False, "files": []}
        files = (
            (Path(distribution.locate_file(relative)), name, str(relative).replace("\\", "/"))
            for relative in distribution.files or ()
        )
        if parallel:
            owner_inputs.extend(files)
        else:
            for key, owner_name, relative in map(resolve_owner, files):
                owners.setdefault(key, []).append((owner_name, relative))

    # One ordered batch avoids per-distribution thread scheduling overhead.
    # Metadata and lock/duplicate-version validation above stay on this thread.
    if parallel:
        for key, name, relative in ordered_map(resolve_owner, owner_inputs):
            owners.setdefault(key, []).append((name, relative))

    records = list(toc_records(ast.literal_eval(analysis.read_text(encoding="utf-8"))))
    if not records:
        raise ValueError("PyInstaller analysis contains no collected file records")
    recorded: set[tuple[str, str, str]] = set()
    source_root = (repo / "src").resolve()
    def resolve_record(record: tuple[str, str, str]):
        module, filename, kind = record
        if not filename:
            return None
        path = Path(filename).resolve()
        return module, filename, kind, path, file_key(path)

    def pending_files():
        for resolved in ordered_map(resolve_record, records):
            if resolved is None:
                continue
            module, filename, kind, path, source_key = resolved
            matches = owners.get(source_key, [])
            if not matches and path.is_relative_to(source_root):
                matches = [("opensquilla", str(path.relative_to(source_root)).replace("\\", "/"))]
            if not matches and "site-packages" in path.parts:
                raise ValueError(f"Collected dependency has no distribution ownership: {filename}")
            for name, relative in matches:
                if name not in packages:
                    raise ValueError(f"Missing installed distribution metadata for {name}")
                key = (name, relative, kind)
                if key in recorded:
                    continue
                recorded.add(key)
                yield name, relative, module, kind, path

    def hash_record(item):
        name, relative, module, kind, path = item
        return name, {"path": relative, "module": module, "kind": kind, "sha256": digest(path)}

    # Finish path resolution before starting the hash batch, so the same pool
    # does not contend between two independent ordered-map queues.
    pending = list(pending_files()) if parallel else pending_files()
    for name, record in ordered_map(hash_record, pending):
        packages[name]["bundled"] = True
        packages[name]["files"].append(record)
    if not any(p["bundled"] and p["name"] != "opensquilla" for p in packages.values()):
        raise ValueError("PyInstaller inventory contains no bundled third-party dependencies")
    return [packages[name] for name in sorted(packages)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--kind", choices=("wheelhouse", "pyinstaller"), required=True)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--profile", default="desktop")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--workers", type=worker_count, default=1,
        help="Frozen inventory read workers, 1..4 (default: 1; wheelhouse remains serial)",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    versions = locked_versions(repo)
    if args.kind == "wheelhouse":
        if args.packages is None:
            parser.error("--packages is required for a wheelhouse")
        packages = wheel_packages(args.packages, versions)
    else:
        if args.analysis is None:
            parser.error("--analysis is required for a frozen package")
        packages = frozen_packages(repo, args.analysis, versions, workers=args.workers)
    result = {
        "schemaVersion": 1, "kind": args.kind, "profile": args.profile,
        "lockSha256": digest(repo / "uv.lock"), "packages": packages,
    }
    if args.analysis:
        result["analysisSha256"] = digest(args.analysis)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
