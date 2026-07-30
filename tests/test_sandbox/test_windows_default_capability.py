from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_capability_sid_is_stable_per_root(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
        load_capability_store,
    )

    store_path = tmp_path / "cap_sids.json"
    generator = iter(
        [
            "S-1-5-21-100-101-102-103",
            "S-1-5-21-200-201-202-203",
        ]
    ).__next__

    first = capability_sid_for_root(store_path, tmp_path / "workspace", sid_factory=generator)
    second = capability_sid_for_root(store_path, tmp_path / "workspace", sid_factory=generator)
    loaded = load_capability_store(store_path)

    assert first == "S-1-5-21-100-101-102-103"
    assert second == first
    assert list(loaded.root_sids.values()) == [first]
    assert next(iter(loaded.root_sids)).startswith("rwx|")


def test_command_capabilities_only_include_current_roots(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
        capability_sids_for_command,
    )

    store_path = tmp_path / "cap_sids.json"
    generator = iter(
        [
            "S-1-5-21-100-101-102-103",
            "S-1-5-21-200-201-202-203",
        ]
    ).__next__
    workspace_sid = capability_sid_for_root(
        store_path,
        tmp_path / "workspace",
        sid_factory=generator,
    )
    other_sid = capability_sid_for_root(store_path, tmp_path / "other", sid_factory=generator)

    command_sids = capability_sids_for_command(store_path, (tmp_path / "workspace",))

    assert command_sids == (workspace_sid,)
    assert other_sid not in command_sids


def test_generated_restricting_sid_uses_create_restricted_token_compatible_form(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import capability_sid_for_root

    sid = capability_sid_for_root(tmp_path / "cap_sids.json", tmp_path / "workspace")

    assert sid.startswith("S-1-5-21-")
    assert len(sid.split("-")) == 8


def test_legacy_app_capability_sids_are_not_reused(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
        load_capability_store,
    )

    store_path = tmp_path / "cap_sids.json"
    root = tmp_path / "workspace"
    root_key = str(root).replace("\\", "\\\\")
    store_path.write_text(
        f'{{"rootSids": {{"{root_key}": "S-1-15-3-100-101-102-103-104-105-106-107"}}}}',
        encoding="utf-8",
    )

    loaded = load_capability_store(store_path)
    sid = capability_sid_for_root(
        store_path,
        root,
        sid_factory=lambda: "S-1-5-21-200-201-202-203",
    )

    assert loaded.root_sids == {}
    assert sid == "S-1-5-21-200-201-202-203"


def test_capability_sid_is_isolated_by_access_level_on_downgrade(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
        capability_sids_for_command,
    )

    store_path = tmp_path / "cap_sids.json"
    root = tmp_path / "workspace"
    generator = iter(
        [
            "S-1-5-21-100-101-102-103",
            "S-1-5-21-200-201-202-203",
        ]
    ).__next__

    write_sid = capability_sid_for_root(
        store_path,
        root,
        access="RWX",
        sid_factory=generator,
    )
    read_sid = capability_sid_for_root(
        store_path,
        root,
        access="RX",
        sid_factory=generator,
    )

    assert read_sid != write_sid
    assert capability_sids_for_command(
        store_path,
        (root,),
        accesses=("RX",),
    ) == (read_sid,)


def test_capability_sid_key_is_windows_case_insensitive(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
    )

    store_path = tmp_path / "cap_sids.json"
    generator = iter(
        [
            "S-1-5-21-100-101-102-103",
            "S-1-5-21-200-201-202-203",
        ]
    ).__next__

    first = capability_sid_for_root(
        store_path,
        Path(r"C:\\Users\\LRK\\Workspace"),
        access="RWX",
        sid_factory=generator,
    )
    second = capability_sid_for_root(
        store_path,
        Path(r"c:\\users\\lrk\\workspace\\"),
        access="RWX",
        sid_factory=generator,
    )

    assert second == first


def test_legacy_path_only_sid_is_not_reused_for_access_namespaced_key(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import capability_sid_for_root

    store_path = tmp_path / "cap_sids.json"
    root = tmp_path / "workspace"
    store_path.write_text(
        json.dumps({"rootSids": {str(root): "S-1-5-21-100-101-102-103"}}),
        encoding="utf-8",
    )

    sid = capability_sid_for_root(
        store_path,
        root,
        access="RX",
        sid_factory=lambda: "S-1-5-21-200-201-202-203",
    )

    assert sid == "S-1-5-21-200-201-202-203"


def test_concurrent_capability_store_updates_do_not_lose_roots(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_capability import (
        capability_sid_for_root,
        load_capability_store,
    )

    store = tmp_path / "cap_sids.json"
    distinct_roots = tuple(tmp_path / f"root-{index}" for index in range(12))
    roots = (*distinct_roots, *([distinct_roots[0]] * 8))
    with ThreadPoolExecutor(max_workers=6) as pool:
        sids = tuple(pool.map(lambda root: capability_sid_for_root(store, root), roots))

    loaded = load_capability_store(store)
    assert len(set(sids)) == len(distinct_roots)
    assert len(loaded.root_sids) == len(distinct_roots)
    assert not tuple(tmp_path.glob(".cap_sids.json.tmp-*"))


def test_capability_store_atomic_failure_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_capability as mod

    store = tmp_path / "cap_sids.json"
    monkeypatch.setattr(mod.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(OSError, match="replace"):
        mod.save_capability_store(
            store,
            mod.CapabilityStore(root_sids={"rx|root": "S-1-5-21-1-2-3-4"}),
        )
    assert not tuple(tmp_path.glob(".cap_sids.json.tmp-*"))
