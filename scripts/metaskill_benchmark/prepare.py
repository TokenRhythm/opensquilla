"""Fetch pinned public inputs and prepare the 20 MetaSkill benchmark tasks.

Downloads are data preparation only: no upstream package installation, Skill
execution, or model requests happen here. File fingerprints normalize CRLF to
LF for UTF-8 text, permitting verified inputs on all three desktop platforms.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
from typing import Any
import urllib.request
import urllib.parse
import uuid
import zipfile

from .plans import meta_name, render_plan

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmark_data"
SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024


def _long_path(path: Path) -> Path:
    """Use Windows extended paths for deeply nested upstream input packages."""
    path = path.expanduser().resolve()
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def _digest(data: bytes, mode: str = "raw") -> str:
    if mode == "utf8-lf":
        data.decode("utf-8")
        data = data.replace(b"\r\n", b"\n")
    elif mode != "raw":
        raise ValueError(f"Unknown hash mode: {mode}")
    return hashlib.sha256(data).hexdigest()


def _verify(path: Path, expected: dict[str, str]) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing regular input file: {path}")
    if _digest(path.read_bytes(), expected["mode"]) != expected["sha256"]:
        raise ValueError(f"Input fingerprint mismatch: {path}")


def _relative(raw: str) -> Path:
    """Reject archive traversal and platform-dependent path aliases."""
    if not isinstance(raw, str) or "\\" in raw or "\x00" in raw:
        raise ValueError(f"Unsafe relative path: {raw!r}")
    path = PurePosixPath(raw)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if path.is_absolute() or not path.parts or any(
        part in {".", ".."} or ":" in part or part.endswith((".", " "))
        or part.split(".")[0].upper() in reserved for part in path.parts
    ):
        raise ValueError(f"Unsafe relative path: {raw!r}")
    return Path(*path.parts)


def _json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _manifest() -> dict[str, Any]:
    data = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION or len(data["task_ids"]) != 20:
        raise ValueError("Unsupported benchmark source manifest")
    for name, expected in data["plans"].items():
        _verify(DATA / "plans" / _relative(name), expected)
    return data


def _download(url: str, destination: Path, *, timeout: int = 60) -> None:
    """Use the system TLS downloader when present, else Python's HTTPS stack."""
    curl = shutil.which("curl")
    if curl:
        result = subprocess.run([
            curl, "--fail", "--location", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "20", "--max-time", str(timeout),
            "--retry", "2", "--retry-delay", "1",
            "--max-filesize", str(MAX_ARCHIVE_BYTES), "--output", str(destination), url,
        ], capture_output=True, text=True, timeout=timeout * 3 + 15)
        if result.returncode:
            raise RuntimeError(f"Could not download {url}: {result.stderr.strip()}")
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "MetaSkill-benchmark"})
        with urllib.request.urlopen(request, timeout=30) as response, destination.open("xb") as stream:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_ARCHIVE_BYTES:
                    raise ValueError("Source archive exceeds download limit")
                stream.write(chunk)
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Source archive exceeds download limit")


def _extract_selected(archive: Path, destination: Path,
                      expected: dict[str, dict[str, str]]) -> None:
    """Extract only hash-pinned regular files; never trust archive paths."""
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 100_000:
            raise ValueError("Source archive contains too many entries")
        selected: dict[str, zipfile.ZipInfo] = {}
        seen: set[str] = set()
        roots: set[str] = set()
        for info in entries:
            relative = _relative(info.filename.rstrip("/"))
            roots.add(relative.parts[0])
            if len(roots) != 1:
                raise ValueError("Expected one archive root directory")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Symlinks are not accepted in source archives")
            if len(relative.parts) <= 1 or info.is_dir():
                continue
            name = Path(*relative.parts[1:]).as_posix()
            key = name.casefold()
            if key in seen:
                raise ValueError(f"Duplicate archive path: {name}")
            seen.add(key)
            if name in expected:
                if info.file_size > MAX_FILE_BYTES:
                    raise ValueError(f"Source file exceeds size limit: {name}")
                selected[name] = info
        missing = set(expected) - set(selected)
        if missing:
            raise ValueError(f"Pinned archive files are missing: {sorted(missing)[:5]}")
        if sum(info.file_size for info in selected.values()) > MAX_EXTRACTED_BYTES:
            raise ValueError("Selected archive content exceeds size limit")
        for name, info in selected.items():
            data = zipped.read(info)
            if _digest(data, expected[name]["mode"]) != expected[name]["sha256"]:
                raise ValueError(f"Public source differs from pinned input: {name}")
            output = destination / _relative(name)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(data)


def _copy_verified(source: Path, destination: Path,
                   expected: dict[str, dict[str, str]]) -> None:
    source = _long_path(source)
    for name, fingerprint in expected.items():
        original = source / _relative(name)
        if not original.resolve().is_relative_to(source):
            raise ValueError(f"Input escapes its source directory: {name}")
        _verify(original, fingerprint)
        target = destination / _relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(original.read_bytes())


def _fetch_selected(spec: dict[str, Any], destination: Path) -> None:
    """Download only the pinned files, avoiding large unrelated repository assets."""
    def fetch(item: tuple[str, dict[str, str]]) -> None:
        name, expected = item
        relative = _relative(name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        url = (f"https://raw.githubusercontent.com/{spec['repository']}/{spec['revision']}/"
               + urllib.parse.quote(name, safe="/"))
        _download(url, target)
        _verify(target, expected)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, spec["files"].items()))


def _fetch_manim_source(spec: dict[str, Any], destination: Path, archive: Path) -> None:
    """Fall back from archive transport failures to the same pinned raw files."""
    url = f"https://codeload.github.com/{spec['repository']}/zip/{spec['revision']}"
    try:
        _download(url, archive, timeout=120)
    except RuntimeError:
        _fetch_selected(spec, destination)
    else:
        # Content errors must fail preparation, not select another transport.
        _extract_selected(archive, destination, spec["files"])


def _source_files(manifest: dict[str, Any], skill: dict[str, Any]) -> dict[str, dict[str, str]]:
    if skill["source"] == "bundled":
        return skill["files"]
    prefix = skill["path"] + "/"
    return {name[len(prefix):]: expected
            for name, expected in manifest["sources"][skill["source"]]["files"].items()
            if name.startswith(prefix)}


def _assets(upstream: Path, task_id: str) -> list[dict[str, str]]:
    data = upstream / "benchmark/AgentSkillOS_bench/task_data"
    selected: list[tuple[Path, str]] = []
    if task_id == "data_computation_task1":
        selected = [(data / task_id / "penguins.csv", "artifacts/penguins.csv")]
    elif task_id == "document_creation_task2":
        source = data / task_id / "paper_src"
        selected = [(p, "paper_src/" + p.relative_to(source).as_posix())
                    for p in sorted(source.rglob("*")) if p.is_file()]
    elif task_id == "document_creation_task3":
        selected = [(data / task_id / "attention_is_all_you_need.pdf", "attention_is_all_you_need.pdf")]
    return [{"source": str(path), "relative_destination": dest} for path, dest in selected]


def prepare(cache: Path, *, upstream: Path | None = None,
            skills: Path | None = None) -> Path:
    """Prepare immutable inputs in a dedicated cache, without running a model.

    Optional local roots are read-only sources for offline verification. Their
    contents must still match every pinned fingerprint. Existing prepared
    caches are validated, never silently refreshed or overwritten.
    """
    cache = _long_path(cache)
    marker = cache / "prepared.json"
    if marker.exists():
        validate_prepared(marker)
        return marker
    manifest = _manifest()
    cache.mkdir(parents=True, exist_ok=True)
    inputs = cache / "inputs"
    if inputs.exists():
        raise ValueError("Cache has inputs without a valid prepared.json; choose a new cache directory")
    # Work in a unique staging directory; no recursive deletion of user paths.
    stage = cache / (".prepare-" + uuid.uuid4().hex)
    stage.mkdir()
    repos = stage / "sources"
    common = stage / "skills"
    origins: dict[str, str] = {}

    def acquire(item: tuple[str, dict[str, Any]]) -> None:
        source_id, spec = item
        target = repos / source_id
        target.mkdir(parents=True)
        if source_id == "agentskillos" and upstream is not None:
            _copy_verified(upstream, target, spec["files"])
        elif source_id != "agentskillos" and skills is not None:
            for name, skill in manifest["skills"].items():
                if skill["source"] == source_id:
                    _copy_verified(skills / name, target / skill["path"], _source_files(manifest, skill))
        else:
            # The Manim repository is small. Larger upstream repositories
            # contain hundreds of MB of unrelated assets; fetch their pinned
            # file allowlist directly instead of unpacking the entire project.
            if source_id == "manim-skill":
                archive = stage / f"{source_id}.zip"
                _fetch_manim_source(spec, target, archive)
            else:
                _fetch_selected(spec, target)
        origins[source_id] = spec["revision"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(acquire, manifest["sources"].items()))
    for name, skill in manifest["skills"].items():
        source = DATA / skill["path"] if skill["source"] == "bundled" else repos / skill["source"] / skill["path"]
        if skills is not None and skill["source"] != "bundled":
            source = skills / name
        _copy_verified(source, common / name, _source_files(manifest, skill))
    names = {name: skill["runtime_name"] for name, skill in manifest["skills"].items()}
    tasks: dict[str, Any] = {}
    for task_id in manifest["task_ids"]:
        task_path = repos / "agentskillos/benchmark/AgentSkillOS_bench/tasks" / f"{task_id}.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        canonical_path = DATA / "plans" / f"{task_id}.json"
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        task_skills = stage / "tasks" / task_id / "skills"
        for name in task["skills"]:
            if name not in manifest["skills"]:
                raise ValueError(f"Unmapped Skill: {name}")
            _copy_verified(common / name, task_skills / name, _source_files(manifest, manifest["skills"][name]))
        plan_path = stage / "tasks" / task_id / "canonical_plan.json"
        plan_path.write_bytes(canonical_path.read_bytes())
        rendered = task_skills / meta_name(task_id) / "SKILL.md"
        rendered.parent.mkdir(parents=True)
        rendered.write_text(render_plan(task, canonical, names), encoding="utf-8", newline="\n")
        tasks[task_id] = {
            "task_config_path": str(task_path), "prompt": task["prompt"],
            "skills_dir": str(task_skills), "meta_name": meta_name(task_id),
            "plan_path": str(plan_path), "input_assets": _assets(repos / "agentskillos", task_id),
        }
    # ZIP downloads are not run inputs; leave them in staging but do not count
    # them in the input fingerprint set or expose them to an Agent.
    fingerprints = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.suffix != ".zip":
            fingerprints[path.relative_to(stage).as_posix()] = _digest(path.read_bytes())
    stage.rename(inputs)
    stage_prefix, inputs_prefix = str(stage), str(inputs)
    for task in tasks.values():
        for key in ("task_config_path", "skills_dir", "plan_path"):
            task[key] = inputs_prefix + task[key][len(stage_prefix):]
        for asset in task["input_assets"]:
            asset["source"] = inputs_prefix + asset["source"][len(stage_prefix):]
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "upstream_root": str(inputs / "sources/agentskillos"),
        "tasks": tasks, "source_revisions": origins,
        "source_manifest_sha256": _digest((DATA / "sources.json").read_bytes()),
        "fingerprints": fingerprints,
    }
    _json_write(marker, prepared)
    validate_prepared(marker)
    return marker


def validate_prepared(prepared_path: Path) -> dict[str, Any]:
    """Reject stale or modified prepared inputs before a benchmark run."""
    prepared_path = _long_path(prepared_path)
    data = json.loads(prepared_path.read_text(encoding="utf-8"))
    manifest = _manifest()
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported prepared input schema")
    if data.get("source_manifest_sha256") != _digest((DATA / "sources.json").read_bytes()):
        raise ValueError("Prepared inputs use a different source manifest; choose a new cache")
    if list(data.get("tasks", {})) != manifest["task_ids"]:
        raise ValueError("Prepared task set or order differs from the frozen 20 tasks")
    inputs = prepared_path.parent / "inputs"
    expected_files: set[str] = set()

    def check_files(prefix: Path, files: dict[str, dict[str, str]]) -> None:
        for relative, expected in files.items():
            path = prefix / _relative(relative)
            if not path.resolve().is_relative_to(inputs.resolve()):
                raise ValueError("Prepared input escapes its cache")
            _verify(path, expected)
            expected_files.add(path.relative_to(inputs).as_posix())

    revisions = {key: value["revision"] for key, value in manifest["sources"].items()}
    if data.get("source_revisions") != revisions:
        raise ValueError("Prepared source revisions differ from the pinned sources")
    for source_id, source in manifest["sources"].items():
        check_files(inputs / "sources" / source_id, source["files"])
    for name, skill in manifest["skills"].items():
        check_files(inputs / "skills" / name, _source_files(manifest, skill))
    names = {name: skill["runtime_name"] for name, skill in manifest["skills"].items()}
    for task_id in manifest["task_ids"]:
        task_root = inputs / "tasks" / task_id
        task_path = inputs / "sources/agentskillos/benchmark/AgentSkillOS_bench/tasks" / f"{task_id}.json"
        config = json.loads(task_path.read_text(encoding="utf-8"))
        for name in config["skills"]:
            check_files(task_root / "skills" / name, _source_files(manifest, manifest["skills"][name]))
        check_files(task_root, {"canonical_plan.json": manifest["plans"][f"{task_id}.json"]})
        canonical = json.loads((task_root / "canonical_plan.json").read_text(encoding="utf-8"))
        rendered = render_plan(config, canonical, names).encode("utf-8")
        check_files(task_root / "skills" / meta_name(task_id), {
            "SKILL.md": {"mode": "utf8-lf", "sha256": _digest(rendered, "utf8-lf")},
        })
    if set(data.get("fingerprints", {})) != expected_files:
        raise ValueError("Prepared fingerprint file set differs from the pinned inputs")
    allowed_archives = {f"{name}.zip" for name in manifest["sources"]}
    actual_files = set()
    for path in inputs.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Unexpected link in prepared inputs: {path}")
        if path.is_file():
            actual_files.add(path.relative_to(inputs).as_posix())
    if actual_files - allowed_archives != expected_files:
        raise ValueError("Prepared inputs contain missing or untracked files")
    for name, digest in data["fingerprints"].items():
        path = inputs / _relative(name)
        if not path.resolve().is_relative_to(inputs.resolve()):
            raise ValueError("Prepared fingerprint path escapes its cache")
        _verify(path, {"mode": "raw", "sha256": digest})
    if Path(data["upstream_root"]).resolve() != (inputs / "sources/agentskillos").resolve():
        raise ValueError("Prepared upstream root is outside the managed cache")
    for task_id, task in data["tasks"].items():
        expected_paths = {
            "task_config_path": inputs / "sources/agentskillos/benchmark/AgentSkillOS_bench/tasks" / f"{task_id}.json",
            "skills_dir": inputs / "tasks" / task_id / "skills",
            "plan_path": inputs / "tasks" / task_id / "canonical_plan.json",
        }
        for key, expected in expected_paths.items():
            if not Path(task[key]).is_absolute() or Path(task[key]).resolve() != expected.resolve():
                raise ValueError(f"Prepared task path differs from its managed input: {task_id}/{key}")
        config = json.loads(Path(task["task_config_path"]).read_text(encoding="utf-8"))
        if config["task_id"] != task_id or task["prompt"] != config["prompt"]:
            raise ValueError(f"Prepared task prompt or ID changed: {task_id}")
        if task["meta_name"] != meta_name(task_id):
            raise ValueError(f"Prepared MetaSkill name changed: {task_id}")
        if task["input_assets"] != _assets(Path(data["upstream_root"]), task_id):
            raise ValueError(f"Prepared input staging changed: {task_id}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, help="Optional verified local upstream snapshot")
    parser.add_argument("--skills", type=Path, help="Optional verified local Skills snapshot")
    args = parser.parse_args()
    print(prepare(args.cache, upstream=args.upstream, skills=args.skills))


if __name__ == "__main__":
    main()
