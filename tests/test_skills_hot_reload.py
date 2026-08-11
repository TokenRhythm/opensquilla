from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.skills import loader as skill_loader_module
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


def test_loader_normalizes_crlf_before_parsing_yaml_block_scalars(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_file = root / "crlf-meta" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    source = """---
name: crlf-meta
description: CRLF fixture
kind: meta
composition:
  steps:
    - id: deliver
      kind: llm_chat
      with:
        task: |
          first line
          second line
---
body
"""
    skill_file.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    loader = _loader(root, tmp_path)
    loader.load_all()
    spec = loader.get_by_name("crlf-meta")

    assert spec is not None
    assert spec.composition_raw is not None
    step = spec.composition_raw["steps"][0]
    assert step["with"]["task"] == "first line\nsecond line"


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


def test_supporting_resource_change_publishes_new_tree_digest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_file = _write_skill(root, "alpha")
    resource = skill_file.parent / "references" / "guide.md"
    resource.parent.mkdir()
    resource.write_text("first\n", encoding="utf-8")
    loader = _loader(root, tmp_path)
    loader.load_all()
    old = loader.snapshot()
    old_digest = old.get_by_name("alpha").tree_digest  # type: ignore[union-attr]

    resource.write_text("second and longer\n", encoding="utf-8")
    result = loader.refresh_if_changed("resource update")

    assert result.modified == ("alpha",)
    assert result.generation == old.generation + 1
    assert loader.get_by_name("alpha").tree_digest != old_digest  # type: ignore[union-attr]


def test_verified_reload_stays_hidden_until_durable_barrier_commit(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "old")
    loader = _loader(root, tmp_path)
    loader.load_all()
    old = loader.snapshot()

    with loader.catalog_publication_barrier("test") as publication:
        with loader.mutation_guard("test"):
            _write_skill(root, "alpha", "new")
        result = loader.reload_verified(lambda candidate: None, reason="test")

        assert result.success is True
        assert result.generation == old.generation + 1
        assert loader.snapshot() is old
        assert loader.get_by_name("alpha").description == "old"  # type: ignore[union-attr]
        assert loader.refresh_if_changed("concurrent-turn").generation == old.generation
        publication.commit()

    assert loader.snapshot().generation == old.generation + 1
    assert loader.get_by_name("alpha").description == "new"  # type: ignore[union-attr]


def test_same_layer_symlinked_manifests_keep_distinct_candidate_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    shared = root / "shared.md"
    shared.write_text(
        "---\nname: shared\ndescription: shared internal manifest\n---\nBody.\n",
        encoding="utf-8",
    )
    try:
        for directory_name in ("a", "b"):
            directory = root / directory_name
            directory.mkdir()
            (directory / "SKILL.md").symlink_to(shared)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    loader = _loader(root, tmp_path)
    loader.load_all()
    snapshot = loader.snapshot()

    assert len(snapshot.candidates) == 2
    assert len(snapshot.shadowed) == 1
    assert len({candidate.instance_id for candidate in snapshot.candidates}) == 2
    assert len({candidate.file_path for candidate in snapshot.candidates}) == 2


def test_symlinked_manifest_outside_layer_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "escaped"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: escaped\ndescription: outside manifest\n---\nBody.\n",
        encoding="utf-8",
    )
    try:
        (directory / "SKILL.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    loader = _loader(root, tmp_path)
    loader.load_all()

    assert loader.snapshot().skills == ()
    assert any("manifest escapes layer root" in error.message for error in loader.snapshot().errors)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux permits byte-oriented filenames that macOS and Windows reject",
)
def test_local_non_utf8_supporting_filename_does_not_break_catalog(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_file = _write_skill(root, "byte-name")
    raw_path = os.fsencode(skill_file.parent) + b"/asset-\xff.bin"
    descriptor = os.open(raw_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"payload")
    finally:
        os.close(descriptor)

    loader = _loader(root, tmp_path)
    loader.load_all()

    assert loader.get_by_name("byte-name") is not None
    assert loader.snapshot().errors == ()


def test_rejected_verified_reload_never_replaces_visible_catalog(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "old")
    loader = _loader(root, tmp_path)
    loader.load_all()
    old = loader.snapshot()

    def reject(_candidate) -> None:
        raise RuntimeError("synthetic postflight rejection")

    with loader.catalog_publication_barrier("test"):
        with loader.mutation_guard("test"):
            _write_skill(root, "alpha", "rejected")
        result = loader.reload_verified(reject, reason="test")
        assert result.success is False
        assert result.generation == old.generation
        assert loader.snapshot() is old

    assert loader.snapshot() is old
    assert loader.get_by_name("alpha").description == "old"  # type: ignore[union-attr]


def test_concurrent_reload_cannot_report_provisional_generation(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "old")
    loader = _loader(root, tmp_path)
    loader.load_all()
    old = loader.snapshot()
    verifier_entered = threading.Event()
    release_verifier = threading.Event()
    verified_results = []
    reader_results = []

    def blocked_verifier(_candidate) -> None:
        verifier_entered.set()
        assert release_verifier.wait(timeout=5)

    with loader.catalog_publication_barrier("test") as publication:
        with loader.mutation_guard("test"):
            _write_skill(root, "alpha", "new")
        verified_thread = threading.Thread(
            target=lambda: verified_results.append(
                loader.reload_verified(blocked_verifier, reason="test")
            )
        )
        verified_thread.start()
        assert verifier_entered.wait(timeout=5)
        reader_thread = threading.Thread(
            target=lambda: reader_results.append(loader.reload(reason="concurrent-rpc"))
        )
        reader_thread.start()
        release_verifier.set()
        verified_thread.join(timeout=5)
        reader_thread.join(timeout=5)

        assert verified_results[0].generation == old.generation + 1
        assert reader_results[0].changed is False
        assert reader_results[0].generation == old.generation
        assert loader.snapshot() is old
        publication.commit()

    assert loader.snapshot().generation == old.generation + 1


def test_hidden_resource_change_is_part_of_catalog_tree_digest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_file = _write_skill(root, "alpha")
    hidden = skill_file.parent / ".runtime-policy"
    hidden.write_text("first\n", encoding="utf-8")
    loader = _loader(root, tmp_path)
    loader.load_all()
    generation = loader.snapshot().generation

    hidden.write_text("changed\n", encoding="utf-8")
    result = loader.refresh_if_changed("hidden resource update")

    assert result.modified == ("alpha",)
    assert result.generation == generation + 1


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


def test_managed_recovery_quarantine_keeps_lkg_but_refreshes_other_layers(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    _write_skill(managed, "managed-skill", "managed old")
    _write_skill(workspace, "workspace-skill", "workspace old")
    loader = SkillLoader(
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()

    _write_skill(managed, "managed-skill", "managed uncommitted")
    _write_skill(managed, "managed-new", "managed uncommitted")
    _write_skill(workspace, "workspace-skill", "workspace new")
    loader.freeze_catalog_for_recovery(reason="test.recovery")

    refreshed = loader.refresh_if_changed("test.non-managed-refresh")

    assert refreshed.modified == ("workspace-skill",)
    assert loader.get_by_name("managed-skill").description == "managed old"  # type: ignore[union-attr]
    assert loader.get_by_name("managed-new") is None
    assert loader.get_by_name("workspace-skill").description == "workspace new"  # type: ignore[union-attr]

    loader.clear_catalog_recovery_freeze()
    loader.refresh_if_changed("test.recovery-cleared")
    assert loader.get_by_name("managed-skill").description == "managed uncommitted"  # type: ignore[union-attr]
    assert loader.get_by_name("managed-new") is not None


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
    monotonic_now = 10.0
    monkeypatch.setattr(
        skill_loader_module,
        "time",
        SimpleNamespace(monotonic=lambda: monotonic_now),
    )
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

    monotonic_now += skill_loader_module._COMPAT_PROBE_INTERVAL_SECONDS
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


def test_snapshot_v12_is_invalid_and_v15_round_trips_atomically(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"version": 12}), encoding="utf-8")
    loader = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    assert loader.load_snapshot() is None

    loader.load_all()
    loader.save_snapshot()
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["version"] == 15
    assert all("mtime_ns" in entry for entry in data["manifest"].values())
    assert all("tree_state" in entry for entry in data["manifest"].values())
    assert data["skills"][0]["tree_digest"]
    assert not list(tmp_path.glob(".snapshot.json.*.tmp"))

    restored = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    restored_skills = restored.load_snapshot() or []
    assert [skill.name for skill in restored_skills] == ["alpha"]
    assert restored_skills[0].tree_digest == data["skills"][0]["tree_digest"]


def test_description_zh_is_parsed_and_survives_snapshot_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_file = root / "alpha" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(
        '---\nname: alpha\ndescription: English summary\n'
        'description_zh: "中文摘要"\ntriggers: [alpha]\n---\nbody',
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "snapshot.json"

    loader = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    loaded = loader.load_all()
    assert [s.description_zh for s in loaded] == ["中文摘要"]

    loader.save_snapshot()
    restored = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    from_snapshot = restored.load_snapshot() or []
    assert [s.description_zh for s in from_snapshot] == ["中文摘要"]


def test_malformed_v13_snapshot_falls_back_to_full_scan(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    probe = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 13,
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


def test_v13_snapshot_with_unhashable_skill_name_falls_back_to_scan(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    snapshot_path = tmp_path / "snapshot.json"
    probe = SkillLoader(workspace_dir=root, snapshot_path=snapshot_path)
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 13,
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
