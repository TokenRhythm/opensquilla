"""Background setup RPC contract for blocked meta-skill launches."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import opensquilla.gateway.rpc_meta_runs as meta_rpc
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_meta_runs import (
    _META_SETUP_JOBS,
    _META_SETUP_LATEST,
    _META_SETUP_TASKS,
    _handle_meta_setup_install,
    _handle_meta_setup_plan,
    _handle_meta_setup_status,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE
from opensquilla.skills.hub.deps import DepResult
from opensquilla.skills.meta.readiness import MetaSetupAction, MetaSkillReadiness
from opensquilla.skills.types import SkillInstallSpec, SkillPlatformMeta, SkillRequires


class _Loader:
    def __init__(self) -> None:
        self.owner = SimpleNamespace(
            name="meta-paper",
            kind="meta",
            disable_model_invocation=False,
            metadata=SkillPlatformMeta(
                install=[
                    SkillInstallSpec(
                        kind="toolchain",
                        id="paper-tex",
                        bins=["xelatex", "bibtex"],
                    )
                ]
            )
        )

    def get_by_name(self, name: str):
        return self.owner if name == "meta-paper" else None

    def load_all(self):
        return [self.owner]


class _EnvLoader(_Loader):
    def __init__(self) -> None:
        self.owner = SimpleNamespace(
            name="meta-paper",
            kind="meta",
            disable_model_invocation=False,
            metadata=SkillPlatformMeta(
                requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
            ),
            composition_raw=None,
        )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(meta_skill=SimpleNamespace(enabled=True))


def _blocked() -> MetaSkillReadiness:
    return MetaSkillReadiness(
        ready=False,
        missing_bins=("bibtex", "xelatex"),
        reasons=("Missing binary: bibtex", "Missing binary: xelatex"),
        setup_actions=(
            MetaSetupAction(
                id="meta-paper:paper-tex",
                skill="meta-paper",
                install_id="paper-tex",
                kind="toolchain",
                label="Install verified TeX toolchain",
                bins=("bibtex", "xelatex"),
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _clear_jobs():
    _META_SETUP_JOBS.clear()
    _META_SETUP_LATEST.clear()
    _META_SETUP_TASKS.clear()
    yield
    _META_SETUP_JOBS.clear()
    _META_SETUP_LATEST.clear()
    _META_SETUP_TASKS.clear()


def test_meta_setup_scope_contract() -> None:
    assert METHOD_SCOPES["meta.setup.plan"] == READ_SCOPE
    assert METHOD_SCOPES["meta.setup.status"] == READ_SCOPE
    assert METHOD_SCOPES["meta.setup.install"] == ADMIN_SCOPE


def test_meta_setup_plan_uses_launch_equivalent_capability_readiness(monkeypatch) -> None:
    calls: list[str] = []

    def plan(name, ctx):
        del ctx
        calls.append(name)
        return MetaSkillReadiness(
            ready=False,
            missing_capabilities=("paper-tex",),
            setup_actions=(
                MetaSetupAction(
                    id="meta-paper:paper-tex",
                    skill="meta-paper",
                    install_id="paper-tex",
                    kind="toolchain",
                    label="Install paper toolchain",
                    bins=("bibtex", "xelatex"),
                ),
            ),
        ), {}

    monkeypatch.setattr(meta_rpc, "_meta_setup_plan", plan)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    payload = asyncio.run(_handle_meta_setup_plan({"name": "meta-paper"}, ctx))

    assert payload["readiness"]["ready"] is False
    assert payload["readiness"]["missing_capabilities"] == ["paper-tex"]
    assert payload["readiness"]["setup_actions"][0]["install_id"] == "paper-tex"
    assert calls == ["meta-paper"]


def test_meta_setup_plan_does_not_project_media_key_into_untrusted_meta(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(
        "OPENSQUILLA_TEST_META_CUSTOM_KEY",
        "synthetic-openrouter-custom-env-key",
    )
    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True),
        llm=SimpleNamespace(
            provider="openrouter",
            api_key="",
            api_key_env="OPENSQUILLA_TEST_META_CUSTOM_KEY",
        ),
    )

    payload = asyncio.run(
        _handle_meta_setup_plan(
            {"name": "meta-paper"},
            RpcContext(conn_id="test", config=config, skill_loader=_EnvLoader()),
        )
    )

    assert payload["ok"] is True
    assert payload["readiness"]["ready"] is False
    assert payload["readiness"]["missing_env_any"] == [["OPENROUTER_API_KEY"]]
    assert payload["readiness"]["manual_setup_actions"] == []
    assert "synthetic-openrouter-custom-env-key" not in repr(payload)


def test_meta_setup_install_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs._meta_setup_plan",
        lambda name, ctx: (_blocked(), {}),
    )
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    with pytest.raises(Exception, match="confirmed=true"):
        asyncio.run(
            _handle_meta_setup_install(
                {"name": "meta-paper", "sessionKey": "agent:main:test"},
                ctx,
            )
        )

    assert not _META_SETUP_JOBS


def test_meta_setup_runs_in_background_and_verifies_before_completion(monkeypatch) -> None:
    calls = 0

    def plan(name, ctx):
        nonlocal calls
        calls += 1
        return (_blocked() if calls == 1 else MetaSkillReadiness(ready=True), {})

    async def install(specs, progress_cb=None):
        assert specs[0].id == "paper-tex"
        assert progress_cb is not None
        progress_cb(specs[0], 25, 100)
        return [
            DepResult(
                kind="toolchain",
                identifier="paper-tex",
                success=True,
                message="Installed",
            )
        ]

    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs._meta_setup_plan", plan)
    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs.install_deps", install)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    async def scenario():
        started = await _handle_meta_setup_install(
            {
                "name": "meta-paper",
                "sessionKey": "agent:main:test",
                "confirmed": True,
                "action_ids": ["meta-paper:paper-tex"],
            },
            ctx,
        )
        assert started["ok"] is True
        assert started["job"]["status"] in {"queued", "running"}
        await asyncio.gather(*list(_META_SETUP_TASKS))
        return await _handle_meta_setup_status(
            {
                "jobId": started["job"]["job_id"],
                "sessionKey": "agent:main:test",
            },
            ctx,
        )

    status = asyncio.run(scenario())

    assert status["job"]["status"] == "completed"
    assert status["job"]["phase"] == "completed"
    assert status["job"]["completed_actions"] == ["meta-paper:paper-tex"]
    assert status["job"]["downloaded_bytes"] == 25
    assert status["job"]["download_total_bytes"] == 100
    assert status["job"]["readiness"]["ready"] is True
    assert calls == 2


def test_concurrent_same_session_setup_reuses_job_after_readiness_await(monkeypatch) -> None:
    planning_barrier = threading.Barrier(2)
    planning_lock = threading.Lock()
    plan_calls = 0
    release_install = asyncio.Event()

    def plan(name, ctx):
        del name, ctx
        nonlocal plan_calls
        with planning_lock:
            plan_calls += 1
            call = plan_calls
        if call <= 2:
            planning_barrier.wait(timeout=5)
            return _blocked(), {}
        return MetaSkillReadiness(ready=True), {}

    async def install(specs, progress_cb=None):
        del progress_cb
        assert specs[0].id == "paper-tex"
        await release_install.wait()
        return [
            DepResult(
                kind="toolchain",
                identifier="paper-tex",
                success=True,
                message="Installed",
            )
        ]

    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs._meta_setup_plan", plan)
    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs.install_deps", install)
    monkeypatch.setattr(meta_rpc, "_META_SETUP_ACTIVE_JOB_LIMIT", 1)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())
    params = {
        "name": "meta-paper",
        "sessionKey": "agent:main:same-session",
        "confirmed": True,
    }

    async def scenario():
        first, second = await asyncio.gather(
            _handle_meta_setup_install(dict(params), ctx),
            _handle_meta_setup_install(dict(params), ctx),
        )
        release_install.set()
        await asyncio.gather(*list(_META_SETUP_TASKS))
        return first, second

    first, second = asyncio.run(scenario())

    assert first["job"]["job_id"] == second["job"]["job_id"]
    assert {first["reused"], second["reused"]} == {False, True}
    assert len(_META_SETUP_JOBS) == 1
    assert next(iter(_META_SETUP_JOBS.values())).status == "completed"
    assert plan_calls == 3


def test_meta_setup_failure_never_reports_completed(monkeypatch) -> None:
    async def install(specs, progress_cb=None):
        return [
            DepResult(
                kind="toolchain",
                identifier="paper-tex",
                success=False,
                message="checksum mismatch",
            )
        ]

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs._meta_setup_plan",
        lambda name, ctx: (_blocked(), {}),
    )
    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs.install_deps", install)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    async def scenario():
        started = await _handle_meta_setup_install(
            {
                "name": "meta-paper",
                "sessionKey": "agent:main:test",
                "confirmed": True,
            },
            ctx,
        )
        await asyncio.gather(*list(_META_SETUP_TASKS))
        return _META_SETUP_JOBS[started["job"]["job_id"]]

    job = asyncio.run(scenario())

    assert job.status == "failed"
    assert job.phase == "failed"
    assert "checksum mismatch" in job.error
    assert job.readiness is not None
    assert job.readiness["ready"] is False


def test_meta_setup_status_is_bound_to_originating_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs._meta_setup_plan",
        lambda name, ctx: (_blocked(), {}),
    )
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    async def scenario():
        started = await _handle_meta_setup_install(
            {
                "name": "meta-paper",
                "sessionKey": "agent:main:one",
                "confirmed": True,
            },
            ctx,
        )
        with pytest.raises(Exception, match="not found"):
            await _handle_meta_setup_status(
                {
                    "jobId": started["job"]["job_id"],
                    "sessionKey": "agent:main:other",
                },
                ctx,
            )
        for task in list(_META_SETUP_TASKS):
            task.cancel()
        await asyncio.gather(*list(_META_SETUP_TASKS), return_exceptions=True)

    asyncio.run(scenario())


def test_meta_setup_enforces_active_job_capacity(monkeypatch) -> None:
    release = asyncio.Event()

    async def install(specs, progress_cb=None):
        await release.wait()
        return [
            DepResult(
                kind="toolchain",
                identifier="paper-tex",
                success=False,
                message="stopped",
            )
        ]

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs._meta_setup_plan",
        lambda name, ctx: (_blocked(), {}),
    )
    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs.install_deps", install)
    monkeypatch.setattr(meta_rpc, "_META_SETUP_ACTIVE_JOB_LIMIT", 1)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    async def scenario():
        first = await _handle_meta_setup_install(
            {
                "name": "meta-paper",
                "sessionKey": "agent:main:first",
                "confirmed": True,
            },
            ctx,
        )
        await asyncio.sleep(0)
        with pytest.raises(Exception, match="capacity is full"):
            await _handle_meta_setup_install(
                {
                    "name": "meta-paper",
                    "sessionKey": "agent:main:second",
                    "confirmed": True,
                },
                ctx,
            )
        release.set()
        await asyncio.gather(*list(_META_SETUP_TASKS))
        return _META_SETUP_JOBS[first["job"]["job_id"]]

    job = asyncio.run(scenario())

    assert job.status == "failed"


def test_meta_setup_ignores_progress_after_terminal_failure(monkeypatch) -> None:
    callbacks = []

    async def install(specs, progress_cb=None):
        assert progress_cb is not None
        callbacks.append(progress_cb)
        return [
            DepResult(
                kind="toolchain",
                identifier="paper-tex",
                success=False,
                message="timed out while worker continued",
            )
        ]

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs._meta_setup_plan",
        lambda name, ctx: (_blocked(), {}),
    )
    monkeypatch.setattr("opensquilla.gateway.rpc_meta_runs.install_deps", install)
    ctx = RpcContext(conn_id="test", config=_cfg(), skill_loader=_Loader())

    async def scenario():
        started = await _handle_meta_setup_install(
            {
                "name": "meta-paper",
                "sessionKey": "agent:main:test",
                "confirmed": True,
            },
            ctx,
        )
        await asyncio.gather(*list(_META_SETUP_TASKS))
        return _META_SETUP_JOBS[started["job"]["job_id"]]

    job = asyncio.run(scenario())
    assert job.status == "failed"
    assert job.downloaded_bytes == 0

    callbacks[0](SimpleNamespace(), 99, 100)

    assert job.status == "failed"
    assert job.downloaded_bytes == 0
    assert job.download_total_bytes == 0
