"""The versioned store: history is never clobbered, active is atomic + isolated.

The plan's §1.6 requirement is that a same-day re-run bumps the sequence rather
than overwriting a prior version, and that this ``active`` pointer is a different
artifact from ``self_learning``'s ``router/active`` bundle pointer. Reads fail
open to ``None`` so a missing or corrupt profile degrades to the mock baseline
rather than failing a turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.squilla_router.user_profile import store


def _payload(tag: str) -> dict:
    return {"profile_version": tag, "history": {"feedback_count": 1}, "_meta": {"x": 1}}


def test_next_version_starts_at_one_then_bumps(tmp_path: Path) -> None:
    assert store.next_version("2026-07-20", "main", tmp_path) == "2026-07-20.1"
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    assert store.next_version("2026-07-20", "main", tmp_path) == "2026-07-20.2"


def test_a_same_day_rerun_never_overwrites(tmp_path: Path) -> None:
    p1 = store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    p2 = store.write_profile_version(_payload("v2"), "2026-07-20.2", "main", home=tmp_path)
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_an_existing_version_file_is_immutable(tmp_path: Path) -> None:
    path = store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)

    with pytest.raises(FileExistsError):
        store.write_profile_version(_payload("replacement"), "2026-07-20.1", "main", home=tmp_path)

    assert '"profile_version": "v1"' in path.read_text(encoding="utf-8")


def test_active_pointer_round_trips_the_loaded_profile(tmp_path: Path) -> None:
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    loaded = store.load_active_profile("main", tmp_path)
    assert loaded is not None
    assert loaded["profile_version"] == "v1"
    # _meta survives the load; the read seam strips it, not the store.
    assert loaded["_meta"] == {"x": 1}


def test_active_is_independent_of_the_self_learning_bundle_pointer(tmp_path: Path) -> None:
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    # The profiles pointer lives under profiles/, not the router/active bundle.
    pointer = store.active_pointer_path("main", tmp_path)
    assert pointer.parent.name == "profiles"
    assert pointer.read_text().startswith("user_profile.")


def test_missing_pointer_is_none(tmp_path: Path) -> None:
    assert store.load_active_profile("main", tmp_path) is None


def test_a_pointer_with_a_path_separator_is_rejected(tmp_path: Path) -> None:
    pointer = store.active_pointer_path("main", tmp_path)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("../escape.json", encoding="utf-8")
    assert store.load_active_profile("main", tmp_path) is None


def test_a_dangling_pointer_is_none_not_a_raise(tmp_path: Path) -> None:
    store.write_active_atomic("2026-07-20.9", "main", home=tmp_path)
    assert store.load_active_profile("main", tmp_path) is None


def test_corrupt_json_is_none(tmp_path: Path) -> None:
    directory = store.profiles_dir("main", tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / store.version_filename("2026-07-20.1")).write_text("{bad", encoding="utf-8")
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    assert store.load_active_profile("main", tmp_path) is None
