"""Dream linkage when enabling router self-learning via config RPCs.

Self-learning's training trigger rides the post-dream hook, so switching it on
must atomically pull the dream chain (enabled + auto_schedule) up with it —
while disabling self-learning must never touch dream, and explicit dream
values in the same edit must win over the linkage. Offline and synthetic.
"""

from __future__ import annotations

import pytest

import opensquilla.gateway.rpc_config  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import _handle_config_patch, _handle_config_set


def _ctx(tmp_path, **config_kw) -> RpcContext:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"), **config_kw)
    return RpcContext(
        conn_id="t",
        config=cfg,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


async def test_set_enable_selflearning_links_dream(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.config.memory.dream.enabled is False

    res = await _handle_config_set(
        {"path": "squilla_router.self_learning.enabled", "value": True}, ctx
    )

    assert ctx.config.squilla_router.self_learning.enabled is True
    assert ctx.config.memory.dream.enabled is True
    assert ctx.config.memory.dream.auto_schedule is True
    assert sorted(res["linked"]) == [
        "memory.dream.auto_schedule",
        "memory.dream.enabled",
    ]


async def test_patch_enable_selflearning_links_dream(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    res = await _handle_config_patch(
        {"patches": {"squilla_router.self_learning.enabled": True}}, ctx
    )

    assert ctx.config.memory.dream.enabled is True
    assert ctx.config.memory.dream.auto_schedule is True
    assert sorted(res["linked"]) == [
        "memory.dream.auto_schedule",
        "memory.dream.enabled",
    ]


async def test_disable_selflearning_never_touches_dream(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        memory={"dream": {"enabled": True, "auto_schedule": True}},
        squilla_router={"self_learning": {"enabled": True}},
    )

    res = await _handle_config_set(
        {"path": "squilla_router.self_learning.enabled", "value": False}, ctx
    )

    assert ctx.config.squilla_router.self_learning.enabled is False
    assert ctx.config.memory.dream.enabled is True  # untouched
    assert ctx.config.memory.dream.auto_schedule is True
    assert "linked" not in res


async def test_explicit_dream_value_wins_over_linkage(tmp_path) -> None:
    """An operator explicitly keeping dream off in the same patch is obeyed."""
    ctx = _ctx(tmp_path)

    res = await _handle_config_patch(
        {
            "patches": {
                "squilla_router.self_learning.enabled": True,
                "memory.dream.enabled": False,
            }
        },
        ctx,
    )

    assert ctx.config.squilla_router.self_learning.enabled is True
    assert ctx.config.memory.dream.enabled is False  # explicit value respected
    # auto_schedule alone is still linked (it wasn't explicit), but with
    # enabled=False the chain stays inert; the boot warning covers this state.
    assert "memory.dream.enabled" not in res.get("linked", [])


async def test_already_enabled_selflearning_does_not_relink(tmp_path) -> None:
    """Re-asserting enabled=true must not resurrect a since-disabled dream."""
    ctx = _ctx(
        tmp_path,
        squilla_router={"self_learning": {"enabled": True}},
    )
    assert ctx.config.memory.dream.enabled is False

    res = await _handle_config_set(
        {"path": "squilla_router.self_learning.enabled", "value": True}, ctx
    )

    assert ctx.config.memory.dream.enabled is False  # no off->on transition
    assert "linked" not in res


async def test_unrelated_patch_never_links(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    res = await _handle_config_patch(
        {"patches": {"squilla_router.confidence_threshold": 0.6}}, ctx
    )

    assert ctx.config.memory.dream.enabled is False
    assert "linked" not in res


async def test_linked_dream_keys_persist_to_toml(tmp_path) -> None:
    """Linkage must survive a restart: linked keys land in the config file."""
    import tomllib

    ctx = _ctx(tmp_path)
    await _handle_config_set(
        {"path": "squilla_router.self_learning.enabled", "value": True}, ctx
    )

    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert persisted["squilla_router"]["self_learning"]["enabled"] is True
    assert persisted["memory"]["dream"]["enabled"] is True
    assert persisted["memory"]["dream"]["auto_schedule"] is True


async def test_nested_dict_set_triggers_linkage(tmp_path) -> None:
    """config.set with a dict value on the parent path must link too."""
    ctx = _ctx(tmp_path)

    res = await _handle_config_set(
        {"path": "squilla_router.self_learning", "value": {"enabled": True}}, ctx
    )

    assert ctx.config.memory.dream.enabled is True
    assert "memory.dream.enabled" in res["linked"]


async def test_merge_patch_triggers_linkage(tmp_path) -> None:
    """config.patch merge form (nested dict) must link too."""
    ctx = _ctx(tmp_path)

    res = await _handle_config_patch(
        {"patch": {"squilla_router": {"self_learning": {"enabled": True}}}}, ctx
    )

    assert ctx.config.memory.dream.enabled is True
    assert "memory.dream.enabled" in res["linked"]


async def test_linkage_reports_live_reconcile_state(tmp_path) -> None:
    """Without a registered reconciler the response must flag the restart."""
    from opensquilla.gateway.dream_bridge import (
        register_dream_reconciler,
        reset_dream_reconciler,
    )

    reset_dream_reconciler()
    ctx = _ctx(tmp_path)
    res = await _handle_config_set(
        {"path": "squilla_router.self_learning.enabled", "value": True}, ctx
    )
    assert res["linkedLive"] is False
    assert res["restartRequired"] is True

    # With a reconciler wired (as boot does), the linkage goes live in-process.
    calls = {"n": 0}

    async def fake_reconciler() -> None:
        calls["n"] += 1

    register_dream_reconciler(fake_reconciler)
    try:
        ctx2 = _ctx(tmp_path)
        res2 = await _handle_config_set(
            {"path": "squilla_router.self_learning.enabled", "value": True}, ctx2
        )
        assert res2["linkedLive"] is True
        assert calls["n"] == 1
    finally:
        reset_dream_reconciler()


async def test_patch_safe_allows_memory_learning_toggles(tmp_path) -> None:
    """The Settings > Advanced group writes through config.patch.safe
    (operator.write): the three boolean opt-ins must be allowlisted, and the
    safe path must still run the dream linkage (it delegates to the full
    patch handler)."""

    from opensquilla.application.app_settings import _SAFE_WRITE_PATCH_PATHS
    from opensquilla.gateway.rpc_config import _handle_config_patch_safe

    assert "squilla_router.self_learning.enabled" in _SAFE_WRITE_PATCH_PATHS
    assert "memory.dream.enabled" in _SAFE_WRITE_PATCH_PATHS
    assert "memory.dream.auto_schedule" in _SAFE_WRITE_PATCH_PATHS
    # Thresholds/schedules stay admin-only.
    assert "squilla_router.self_learning.train_min_samples" not in _SAFE_WRITE_PATCH_PATHS

    ctx = _ctx(tmp_path)
    res = await _handle_config_patch_safe(
        {"patches": {"squilla_router.self_learning.enabled": True}}, ctx
    )
    assert ctx.config.squilla_router.self_learning.enabled is True
    assert ctx.config.memory.dream.enabled is True
    assert sorted(res["linked"]) == [
        "memory.dream.auto_schedule",
        "memory.dream.enabled",
    ]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
