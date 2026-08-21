from __future__ import annotations

from types import SimpleNamespace

from opensquilla.bootstrap_types import BootstrapFileReport
from opensquilla.engine.runtime import BootstrapSnapshot, MemorySnapshot, TurnRunner
from opensquilla.engine.turn_runner.harness import _TurnRunnerSystemPromptRefreshAdapter
from opensquilla.identity.workspace import filter_workspace_filenames_for_session
from opensquilla.tools.types import ToolContext


def test_bootstrap_write_evicts_only_matching_agent_snapshots() -> None:
    runner = TurnRunner(provider_selector=None)
    report = [BootstrapFileReport(filename="USER.md", raw_chars=4, injected_chars=4)]
    runner._bootstrap_snapshots[("main", "session-a", "full")] = BootstrapSnapshot(
        workspace_files={"USER.md": "main"},
        report=report,
    )
    runner._bootstrap_snapshots[("main", "session-b", "minimal")] = BootstrapSnapshot(
        workspace_files={"USER.md": "main"},
        report=report,
    )
    runner._bootstrap_snapshots[("other", "session-a", "full")] = BootstrapSnapshot(
        workspace_files={"USER.md": "other"},
        report=report,
    )
    runner._memory_snapshots[("main", "session-a")] = object()  # type: ignore[assignment]

    runner._handle_bootstrap_source_write("main", "USER.md")

    assert ("main", "session-a", "full") not in runner._bootstrap_snapshots
    assert ("main", "session-b", "minimal") not in runner._bootstrap_snapshots
    assert ("other", "session-a", "full") in runner._bootstrap_snapshots
    assert ("main", "session-a") in runner._memory_snapshots
    assert "USER.md" not in filter_workspace_filenames_for_session(None, "subagent:worker")


def test_public_profile_snapshot_invalidation_evicts_only_matching_agent() -> None:
    runner = TurnRunner(provider_selector=None)
    runner._bootstrap_snapshots[("main", "session-a", "full")] = BootstrapSnapshot(
        workspace_files={"USER.md": "main"},
        report=[],
    )
    runner._bootstrap_snapshots[("other", "session-a", "full")] = BootstrapSnapshot(
        workspace_files={"USER.md": "other"},
        report=[],
    )

    runner.invalidate_profile_snapshot("main")

    assert ("main", "session-a", "full") not in runner._bootstrap_snapshots
    assert ("other", "session-a", "full") in runner._bootstrap_snapshots


def test_runtime_write_callbacks_are_composed() -> None:
    runner = TurnRunner(provider_selector=None)
    memory_calls: list[tuple[str, str]] = []
    bootstrap_calls: list[tuple[str, str]] = []
    ctx = ToolContext(
        agent_id="main",
        on_memory_source_write=lambda agent_id, path: memory_calls.append((agent_id, path)),
        on_bootstrap_source_write=lambda agent_id, path: bootstrap_calls.append((agent_id, path)),
    )
    runner._bootstrap_snapshots[("main", "session-a", "full")] = BootstrapSnapshot(
        workspace_files={"USER.md": "main"},
        report=[],
    )

    updated = runner._with_runtime_write_callbacks(ctx, "main")

    assert updated.on_memory_source_write is not None
    assert updated.on_bootstrap_source_write is not None
    updated.on_memory_source_write("main", "MEMORY.md")
    updated.on_bootstrap_source_write("main", "USER.md")

    assert memory_calls == [("main", "MEMORY.md")]
    assert bootstrap_calls == [("main", "USER.md")]
    assert ("main", "session-a", "full") not in runner._bootstrap_snapshots


def test_unattended_bootstrap_context_skips_only_bootstrap_md(tmp_path) -> None:
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "BOOTSTRAP.md",
    ):
        (tmp_path / filename).write_text(f"{filename} body\n", encoding="utf-8")
    (tmp_path / "IDENTITY.md").write_text("name: Test Agent\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    metadata: dict[str, object] = {}

    runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:auto",
        prompt_metadata=metadata,
        bootstrap_context_mode="unattended",
    )

    report = metadata["bootstrap_files"]
    filenames = {item.filename for item in report}  # type: ignore[attr-defined]
    assert "BOOTSTRAP.md" not in filenames
    assert {"AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md"} <= filenames


def test_prompt_reports_the_effective_execution_workspace(tmp_path) -> None:
    default_workspace = tmp_path / "default-workspace"
    project_workspace = tmp_path / "project-workspace"
    default_workspace.mkdir()
    project_workspace.mkdir()
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(default_workspace),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:webchat:project-task",
        workspace_dir=str(project_workspace),
    )

    base_prompt = assembled[0] if isinstance(assembled, tuple) else assembled
    assert f"Working directory: {project_workspace}" in base_prompt
    assert f"Working directory: {default_workspace}" not in base_prompt


def test_restricted_prompt_projection_reads_no_workspace_or_local_paths(tmp_path) -> None:
    secret_marker = "PROMPT_ANNOTATION_WORKSPACE_SECRET"
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
    ):
        (tmp_path / filename).write_text(
            f"{secret_marker}:{filename}\n",
            encoding="utf-8",
        )
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:webchat:prompt-annotation",
        prompt_metadata=metadata,
        bootstrap_context_mode="restricted_tool_boundary",
        workspace_dir=str(tmp_path / "selected-project"),
    )

    full_prompt = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    assert secret_marker not in full_prompt
    assert str(tmp_path) not in full_prompt
    assert "Working directory:" not in full_prompt
    assert "## Workspace Files (injected)" not in full_prompt
    assert "## Runtime" not in full_prompt
    assert metadata["injected_workspace_files_count"] == 0
    assert metadata["bootstrap_files"] == []
    assert metadata["memory_md_present"] is False
    assert (
        "main",
        "agent:main:webchat:prompt-annotation",
        "restricted_tool_boundary",
    ) not in runner._bootstrap_snapshots


def test_restricted_compaction_refresh_keeps_workspace_projection_empty(tmp_path) -> None:
    secret_marker = "COMPACTION_WORKSPACE_SECRET"
    (tmp_path / "AGENTS.md").write_text(secret_marker, encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    refreshed: list[str] = []
    agent = SimpleNamespace(
        _tool_context=ToolContext(
            workspace_dir=str(tmp_path),
            exclusive_tools={"document_inspect", "document_apply"},
        ),
        config=SimpleNamespace(workspace_dir=str(tmp_path)),
        refresh_system_prompt=refreshed.append,
    )

    _TurnRunnerSystemPromptRefreshAdapter(runner).refresh_system_prompt(
        agent=agent,
        agent_id="main",
        tool_defs=[],
        session_key="agent:main:webchat:prompt-annotation",
        bootstrap_context_mode="full",
    )

    assert len(refreshed) == 1
    assert secret_marker not in refreshed[0]
    assert str(tmp_path) not in refreshed[0]
    assert "Working directory:" not in refreshed[0]


def test_prompt_metadata_uses_effective_memory_retrieval_metadata(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")

    class _Retriever:
        def effective_retrieval_metadata(self) -> dict[str, str]:
            return {
                "configured_retrieval_mode": "hybrid",
                "retrieval_mode": "hybrid",
                "embedding_requested_provider": "auto",
                "embedding_effective_provider": "local",
                "embedding_model": "BAAI/bge-small-zh-v1.5",
                "vector_weight": "0.7",
                "text_weight": "0.3",
            }

    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace", retrieval_mode="hybrid"),
            tools=SimpleNamespace(profile=None),
        ),
        memory_retrievers={"main": _Retriever()},
    )
    metadata: dict[str, object] = {}

    runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:auto",
        prompt_metadata=metadata,
    )

    assert metadata["retrieval_mode"] == "hybrid"
    assert metadata["embedding_effective_provider"] == "local"
    assert metadata["embedding_model"] == "BAAI/bge-small-zh-v1.5"
    assert metadata["memory_retrieval_vector_weight"] == "0.7"
    assert metadata["memory_retrieval_text_weight"] == "0.3"


def test_fresh_user_session_omits_live_daily_notes_from_dynamic_context(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    runner._load_daily_notes = lambda _workspace_dir: {"2026-05-31.md": "stale Labubu context"}
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:fresh",
        prompt_metadata=metadata,
        fresh_user_session=True,
    )

    dynamic = assembled[1] if isinstance(assembled, tuple) else ""
    assert "## Recent Notes" not in dynamic
    assert "stale Labubu context" not in dynamic
    assert metadata["daily_notes_fresh_session_omitted"] is True
    assert metadata["daily_notes_count_before_omit"] == 1


def test_fresh_user_session_omits_snapshot_daily_notes_from_dynamic_context(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    runner._memory_snapshots[("main", "agent:main:fresh")] = MemorySnapshot(
        memory_md="stable memory",
        daily_notes={"2026-05-31.md": "snapshot daily context"},
    )
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:fresh",
        prompt_metadata=metadata,
        fresh_user_session=True,
    )

    dynamic = assembled[1] if isinstance(assembled, tuple) else ""
    full_prompt = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    assert "snapshot daily context" not in dynamic
    assert "stable memory" in full_prompt
    assert metadata["daily_notes_fresh_session_omitted"] is True
    assert metadata["daily_notes_count_before_omit"] == 1


def test_non_fresh_user_session_omits_live_daily_notes_from_dynamic_context(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    runner._load_daily_notes = lambda _workspace_dir: {"2026-05-31.md": "daily context"}
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:existing",
        prompt_metadata=metadata,
    )

    dynamic = assembled[1] if isinstance(assembled, tuple) else ""
    assert "## Recent Notes" not in dynamic
    assert "daily context" not in dynamic
    assert metadata["daily_notes_omitted"] is True
    assert metadata["daily_notes_policy_reason"] == "auto_injection_disabled"
    assert metadata["daily_notes_count_before_omit"] == 1


def test_daily_notes_auto_omit_preserves_memory_md(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("stable curated memory\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    runner._load_daily_notes = lambda _workspace_dir: {"2026-05-31.md": "daily context"}
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:existing",
        prompt_metadata=metadata,
    )

    full_prompt = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    dynamic = assembled[1] if isinstance(assembled, tuple) else ""
    assert "stable curated memory" in full_prompt
    assert "daily context" not in dynamic
    assert metadata["memory_md_present"] is True
    assert metadata["daily_notes_omitted"] is True


def test_stateless_bootstrap_context_skips_persona_memory_and_bootstrap(tmp_path) -> None:
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "MEMORY.md",
        "HEARTBEAT.md",
        "BOOTSTRAP.md",
    ):
        (tmp_path / filename).write_text(f"{filename} body\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    metadata: dict[str, object] = {}

    runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:auto",
        prompt_metadata=metadata,
        bootstrap_context_mode="stateless",
    )

    report = metadata["bootstrap_files"]
    filenames = {item.filename for item in report}  # type: ignore[attr-defined]
    assert filenames == {"TOOLS.md"}
    assert metadata["memory_md_present"] is False


def test_guest_webchat_namespace_skips_host_bootstrap_and_private_memory(tmp_path) -> None:
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "MEMORY.md",
        "HEARTBEAT.md",
        "BOOTSTRAP.md",
    ):
        (tmp_path / filename).write_text(f"host secret from {filename}\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    metadata: dict[str, object] = {}
    session_key = f"agent:main:webchat:guest:{'a' * 64}:browser-session"

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key=session_key,
        prompt_metadata=metadata,
    )

    full_prompt = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    assert "host secret" not in full_prompt
    assert metadata["bootstrap_files"] == []
    assert metadata["memory_md_present"] is False
    assert metadata["memory_prompt_injection_skipped"] == "session-scope"


def test_stateless_keep_project_rules_preserves_only_agents_md(tmp_path) -> None:
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "MEMORY.md",
        "HEARTBEAT.md",
        "BOOTSTRAP.md",
    ):
        (tmp_path / filename).write_text(f"{filename} body\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    metadata: dict[str, object] = {}

    runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:auto",
        prompt_metadata=metadata,
        bootstrap_context_mode="stateless_keep_project_rules",
    )

    report = metadata["bootstrap_files"]
    filenames = {item.filename for item in report}  # type: ignore[attr-defined]
    assert filenames == {"AGENTS.md", "TOOLS.md"}
    assert metadata["memory_md_present"] is False


def test_full_and_unattended_bootstrap_snapshots_use_distinct_keys(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "BOOTSTRAP.md").write_text("bootstrap\n", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    session_key = "agent:main:auto"

    runner._assemble_prompt("main", [], session_key=session_key)
    runner._assemble_prompt(
        "main",
        [],
        session_key=session_key,
        bootstrap_context_mode="unattended",
    )

    assert ("main", session_key, "full") in runner._bootstrap_snapshots
    assert ("main", session_key, "unattended") in runner._bootstrap_snapshots
    full_snapshot = runner._bootstrap_snapshots[("main", session_key, "full")]
    unattended_snapshot = runner._bootstrap_snapshots[("main", session_key, "unattended")]
    assert "BOOTSTRAP.md" in full_snapshot.workspace_files
    assert (
        "BOOTSTRAP.md"
        not in unattended_snapshot.workspace_files
    )
