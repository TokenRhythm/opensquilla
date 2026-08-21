"""RoutingHistoryStore exposes a bounded per-session history with eviction."""

from __future__ import annotations

from opensquilla.engine.steps.squilla_router import RoutingHistoryStore


def test_get_set_setdefault() -> None:
    store = RoutingHistoryStore()
    assert store.get("agent:main:main") is None
    store.set("agent:main:main", [{"turn_index": 0}])
    assert store.get("agent:main:main") == [{"turn_index": 0}]
    same = store.setdefault("agent:main:main", [])
    assert same == [{"turn_index": 0}]
    fresh = store.setdefault("agent:other:main", [])
    assert fresh == []


def test_length_reports_zero_for_unknown_keys() -> None:
    store = RoutingHistoryStore()
    assert store.length("never:set") == 0
    store.set("agent:main:main", [{"turn_index": 0}, {"turn_index": 1}])
    assert store.length("agent:main:main") == 2


def test_evict_removes_only_the_named_session() -> None:
    store = RoutingHistoryStore()
    store.set("agent:main:main", [{"turn_index": 0}])
    store.set("agent:other:main", [{"turn_index": 0}])
    assert store.evict("agent:main:main") is True
    assert store.get("agent:main:main") is None
    assert store.get("agent:other:main") == [{"turn_index": 0}]
    assert store.evict("agent:main:main") is False  # idempotent


def test_clear_drops_all_entries() -> None:
    store = RoutingHistoryStore()
    store.set("a", [])
    store.set("b", [])
    store.clear()
    assert store.get("a") is None
    assert store.get("b") is None


def test_turn_index_sequence_is_independent_of_bounded_history_length() -> None:
    store = RoutingHistoryStore(max_entries=2)
    store.set(
        "agent:main:main",
        [{"turn_index": 38}, {"turn_index": 39}],
    )
    store.seed_next_turn_index("agent:main:main", 41)

    assert store.reserve_turn_index("agent:main:main") == 41
    store.set("agent:main:main", [{"turn_index": 39}])
    assert store.reserve_turn_index("agent:main:main") == 42


def test_evict_drops_history_but_preserves_turn_index_high_water() -> None:
    store = RoutingHistoryStore()
    assert store.reserve_turn_index("a") == 0
    assert store.evict("a") is False
    assert store.reserve_turn_index("a") == 1
    assert store.reserve_turn_index("b") == 0
    store.clear()
    assert store.reserve_turn_index("a") == 0
    assert store.reserve_turn_index("b") == 0


def test_archive_evict_then_resume_continues_persisted_sequence() -> None:
    store = RoutingHistoryStore()
    store.set("agent:resumed", [{"turn_index": 40}])
    store.seed_next_turn_index("agent:resumed", 41)

    assert store.evict("agent:resumed") is True
    assert store.get("agent:resumed") is None
    assert store.reserve_turn_index("agent:resumed") == 41


def test_append_is_bounded_without_losing_concurrent_entries() -> None:
    import concurrent.futures

    store = RoutingHistoryStore(max_entries=100)

    def append(index: int) -> None:
        store.append("agent:parallel", {"turn_index": index})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(40)))

    history = store.get("agent:parallel")
    assert history is not None
    assert len(history) == 40
    assert {entry["turn_index"] for entry in history} == set(range(40))
    assert store.reserve_turn_index("agent:parallel") == 40


def test_persisted_sequence_can_seed_without_recent_policy_history(monkeypatch) -> None:
    from opensquilla.engine.steps import squilla_router

    monkeypatch.setattr(squilla_router, "_history_store", RoutingHistoryStore())
    assert squilla_router.seed_routing_history(
        {},
        next_turn_indexes={"agent:old": 73},
    ) == 0
    assert squilla_router._history_store.reserve_turn_index("agent:old") == 73
