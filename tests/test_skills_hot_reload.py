from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from opensquilla.skills.loader import MAX_SKILL_FILE_BYTES, SkillLoader


def _write_skill(root: Path, name: str, description: str = "description") -> Path:
    skill_file = root / name / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\ntriggers: [{name}]\n---\nbody",
        encoding="utf-8",
    )
    stat = skill_file.stat()
    bumped = stat.st_mtime_ns + 1_000_000
    os.utime(skill_file, ns=(bumped, bumped))
    return skill_file


def _loader(root: Path, tmp_path: Path) -> SkillLoader:
    return SkillLoader(workspace_dir=root, snapshot_path=tmp_path / "snapshot.json")


def test_external_add_modify_delete_publish_on_next_probe(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    loader = _loader(root, tmp_path)

    initial = loader.refresh_if_changed("test")
    assert initial.generation == 1
    assert loader.snapshot().skills == ()

    skill_file = _write_skill(root, "alpha", "first")
    added = loader.refresh_if_changed("test")
    assert added.added == ("alpha",)
    assert loader.get_by_name("alpha").description == "first"  # type: ignore[union-attr]

    _write_skill(root, "alpha", "second and longer")
    modified = loader.refresh_if_changed("test")
    assert modified.modified == ("alpha",)
    assert loader.get_by_name("alpha").description == "second and longer"  # type: ignore[union-attr]

    skill_file.unlink()
    removed = loader.refresh_if_changed("test")
    assert removed.removed == ("alpha",)
    assert loader.get_by_name("alpha") is None


def test_invalid_new_is_ignored_and_invalid_existing_keeps_last_good(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    alpha_file = _write_skill(root, "alpha", "good")
    loader = _loader(root, tmp_path)
    loader.load_all()

    broken = root / "broken" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("not frontmatter", encoding="utf-8")
    result = loader.refresh_if_changed("test")
    assert result.success is True
    assert result.partial is True
    assert loader.get_by_name("broken") is None

    alpha_file.write_text("not frontmatter either", encoding="utf-8")
    stat = alpha_file.stat()
    os.utime(alpha_file, ns=(stat.st_mtime_ns + 1_000_000,) * 2)
    result = loader.refresh_if_changed("test")
    assert result.partial is True
    assert any(error.name == "alpha" and error.kept_previous for error in result.errors)
    assert loader.get_by_name("alpha").description == "good"  # type: ignore[union-attr]

    _write_skill(root, "alpha", "repaired")
    repaired = loader.refresh_if_changed("test")
    assert repaired.partial is True  # the unrelated broken source remains
    assert loader.get_by_name("alpha").description == "repaired"  # type: ignore[union-attr]


@pytest.mark.parametrize("invalid_name", ["[bad]", "{bad: value}", "null", "123", "''"])
def test_non_string_or_empty_skill_name_is_structured_partial_failure(
    tmp_path: Path,
    invalid_name: str,
) -> None:
    root = tmp_path / "skills"
    alpha_file = _write_skill(root, "alpha", "last known good")
    loader = _loader(root, tmp_path)
    loader.load_all()

    alpha_file.write_text(
        f"---\nname: {invalid_name}\ndescription: invalid\n---\nbody",
        encoding="utf-8",
    )
    result = loader.reload(reason="test")

    assert result.success is True
    assert result.partial is True
    assert result.modified == ("alpha",)
    assert result.errors[0].kept_previous is True
    assert loader.get_by_name("alpha").description == "last known good"  # type: ignore[union-attr]


def test_oversized_existing_skill_keeps_last_good_without_unbounded_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    alpha_file = _write_skill(root, "alpha", "good")
    loader = _loader(root, tmp_path)
    loader.load_all()

    alpha_file.write_bytes(b"x" * (MAX_SKILL_FILE_BYTES + 1))
    result = loader.reload(reason="test")

    assert result.partial is True
    assert any(error.name == "alpha" and error.kept_previous for error in result.errors)
    assert loader.get_by_name("alpha").description == "good"  # type: ignore[union-attr]


def test_new_override_and_removal_restore_lower_layer(tmp_path: Path) -> None:
    low = tmp_path / "low"
    high = tmp_path / "high"
    _write_skill(low, "alpha", "low")
    loader = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=tmp_path / "snapshot.json",
    )
    assert loader.get_by_name("alpha").description == "low"  # type: ignore[union-attr]

    high_file = _write_skill(high, "alpha", "high")
    result = loader.refresh_if_changed("test")
    assert result.modified == ("alpha",)
    assert loader.get_by_name("alpha").description == "high"  # type: ignore[union-attr]

    high_file.unlink()
    result = loader.refresh_if_changed("test")
    assert result.modified == ("alpha",)
    assert loader.get_by_name("alpha").description == "low"  # type: ignore[union-attr]


def test_missing_root_created_after_start_is_discovered(tmp_path: Path) -> None:
    root = tmp_path / "not-created-yet"
    loader = _loader(root, tmp_path)
    loader.load_all()

    _write_skill(root, "late")
    assert loader.refresh_if_changed("test").added == ("late",)


def test_unchanged_probe_does_not_call_parser(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    loader = _loader(root, tmp_path)
    loader.load_all()

    calls = 0
    original = loader._load_skill

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "_load_skill", counted)
    result = loader.refresh_if_changed("test")
    assert result.changed is False
    assert calls == 0


def test_concurrent_changed_reload_executes_one_rebuild(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    loader = _loader(root, tmp_path)
    loader.load_all()
    _write_skill(root, "alpha")

    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = loader._build_catalog

    def blocked(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "_build_catalog", blocked)
    results = []

    first = threading.Thread(target=lambda: results.append(loader.reload(reason="test")))
    second = threading.Thread(target=lambda: results.append(loader.reload(reason="test")))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert calls == 1
    assert len(results) == 2
    assert all(result.added == ("alpha",) for result in results)


def test_force_reload_is_not_swallowed_by_concurrent_lightweight_probe(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    skill_file = _write_skill(root, "alpha", "before")
    loader = _loader(root, tmp_path)
    loader.load_all()

    manifest_entry = loader.snapshot().manifest[str(skill_file.resolve())]
    skill_file.write_text(
        "---\nname: alpha\ndescription: latest\ntriggers: [alpha]\n---\nbody",
        encoding="utf-8",
    )
    assert skill_file.stat().st_size == manifest_entry["size"]
    original_mtime = manifest_entry["mtime_ns"]
    os.utime(skill_file, ns=(original_mtime, original_mtime))

    entered = threading.Event()
    release = threading.Event()
    original_manifest = loader._build_manifest

    def blocked_manifest():
        if threading.current_thread().name == "lightweight-probe":
            entered.set()
            assert release.wait(timeout=5)
        return original_manifest()

    monkeypatch.setattr(loader, "_build_manifest", blocked_manifest)
    results = {}
    probe = threading.Thread(
        name="lightweight-probe",
        target=lambda: results.setdefault("probe", loader.refresh_if_changed("test")),
    )
    forced = threading.Thread(
        name="forced-reload",
        target=lambda: results.setdefault("forced", loader.reload(reason="test")),
    )
    probe.start()
    assert entered.wait(timeout=5)
    forced.start()
    release.set()
    probe.join(timeout=5)
    forced.join(timeout=5)

    assert results["probe"].changed is False
    assert results["forced"].modified == ("alpha",)
    assert loader.get_by_name("alpha").description == "latest"  # type: ignore[union-attr]


def test_manifest_change_during_scan_retries_once(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "initial")
    loader = _loader(root, tmp_path)
    loader.load_all()
    _write_skill(root, "alpha", "first candidate")

    original_manifest = loader._build_manifest
    manifest_calls = 0
    build_calls = 0
    original_build = loader._build_catalog

    def changing_manifest():
        nonlocal manifest_calls
        manifest_calls += 1
        if manifest_calls == 2:
            _write_skill(root, "alpha", "stable second candidate")
        return original_manifest()

    def counted_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(loader, "_build_manifest", changing_manifest)
    monkeypatch.setattr(loader, "_build_catalog", counted_build)

    result = loader.refresh_if_changed("test")

    assert result.success is True
    assert build_calls == 2
    assert loader.get_by_name("alpha").description == "stable second candidate"  # type: ignore[union-attr]


def test_twice_unstable_scan_keeps_last_known_good(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "last known good")
    loader = _loader(root, tmp_path)
    loader.load_all()
    generation = loader.snapshot().generation
    _write_skill(root, "alpha", "first candidate")

    original_manifest = loader._build_manifest
    manifest_calls = 0

    def always_changing_manifest():
        nonlocal manifest_calls
        manifest_calls += 1
        if manifest_calls == 2:
            _write_skill(root, "alpha", "second candidate")
        elif manifest_calls == 3:
            _write_skill(root, "alpha", "third candidate")
        return original_manifest()

    monkeypatch.setattr(loader, "_build_manifest", always_changing_manifest)

    result = loader.refresh_if_changed("test")

    assert result.success is False
    assert result.generation == generation
    assert loader.get_by_name("alpha").description == "last known good"  # type: ignore[union-attr]


def test_mutation_guard_hides_in_progress_write_until_next_access(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "before")
    loader = _loader(root, tmp_path)
    loader.load_all()

    with loader.mutation_guard("test mutation"):
        _write_skill(root, "alpha", "after")
        result = loader.refresh_if_changed("concurrent access")
        assert result.changed is False
        assert loader.snapshot().skills[0].description == "before"

    assert loader._dirty is True
    loader.refresh_if_changed("next access")
    assert loader.snapshot().skills[0].description == "after"


def test_load_all_compatibility_probe_is_monotonic_throttled(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    loader = _loader(root, tmp_path)
    loader.load_all()
    original_manifest = loader._build_manifest
    calls = 0

    def counted_manifest():
        nonlocal calls
        calls += 1
        return original_manifest()

    monkeypatch.setattr(loader, "_build_manifest", counted_manifest)

    loader.load_all()
    loader.load_all()
    assert calls == 0

    loader._last_probe_at = 0.0
    loader.load_all()
    assert calls == 1


def test_independent_gateways_converge_on_their_next_access(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    first = SkillLoader(
        workspace_dir=root,
        snapshot_path=tmp_path / "first-snapshot.json",
    )
    second = SkillLoader(
        workspace_dir=root,
        snapshot_path=tmp_path / "second-snapshot.json",
    )
    first.load_all()
    second.load_all()

    _write_skill(root, "alpha")
    first.refresh_if_changed("first gateway")

    assert [skill.name for skill in first.snapshot().skills] == ["alpha"]
    assert second.snapshot().skills == ()

    second.refresh_if_changed("second gateway")
    assert [skill.name for skill in second.snapshot().skills] == ["alpha"]


def test_global_scan_failure_keeps_last_known_good(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    loader = _loader(root, tmp_path)
    loader.load_all()
    generation = loader.snapshot().generation

    def fail_manifest():
        raise OSError("cannot scan root")

    monkeypatch.setattr(loader, "_build_manifest", fail_manifest)
    result = loader.reload(reason="test")
    assert result.success is False
    assert result.generation == generation
    assert [skill.name for skill in loader.snapshot().skills] == ["alpha"]


def test_publish_writes_snapshot_without_reentering_loader(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    loader = _loader(root, tmp_path)
    loader.load_all()
    _write_skill(root, "alpha")

    def unexpected_load_all():
        raise AssertionError("catalog publication must not probe through load_all")

    monkeypatch.setattr(loader, "load_all", unexpected_load_all)
    result = loader.refresh_if_changed("test")

    assert result.added == ("alpha",)
    assert [skill.name for skill in loader.snapshot().skills] == ["alpha"]


def test_snapshot_v11_is_invalid_and_v12_round_trips_atomically(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"version": 11}), encoding="utf-8")
    loader = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    assert loader.load_snapshot() is None

    loader.load_all()
    loader.save_snapshot()
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["version"] == 12
    assert all("mtime_ns" in entry for entry in data["manifest"].values())
    assert not list(tmp_path.glob(".snapshot.json.*.tmp"))

    restored = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    assert [skill.name for skill in restored.load_snapshot() or []] == ["alpha"]


def test_malformed_v12_snapshot_falls_back_to_full_scan(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    probe = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 12,
                "generation": "not-an-integer",
                "manifest": probe._build_manifest(),
                "source_digests": {},
                "errors": [],
                "skills": [None],
            }
        ),
        encoding="utf-8",
    )

    loader = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    assert loader.load_snapshot() is None
    assert [skill.name for skill in loader.load_all()] == ["alpha"]


def test_v12_snapshot_with_unhashable_skill_name_falls_back_to_scan(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    probe = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 12,
                "generation": 4,
                "manifest": probe._build_manifest(),
                "source_digests": {},
                "errors": [],
                "skills": [{"name": []}],
            }
        ),
        encoding="utf-8",
    )

    loader = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    assert [skill.name for skill in loader.load_all()] == ["alpha"]
