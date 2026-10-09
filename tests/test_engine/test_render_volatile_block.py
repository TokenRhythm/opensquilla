from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.subagent_delegation import DELEGATION_OWNERSHIP_PROMPT
from opensquilla.tools.types import CallerKind, ToolContext


def test_bootstrap_md_renders_under_named_heading() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"BOOTSTRAP.md": "do the setup ritual"},
        extra_context=None,
    )

    assert "### One-Shot Workspace Bootstrap" in rendered
    assert "do the setup ritual" in rendered
    assert "Workspace Context" not in rendered


def test_bootstrap_md_absent_emits_no_bootstrap_heading() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"USER.md": "user profile"},
        extra_context=None,
    )

    assert "One-Shot Workspace Bootstrap" not in rendered
    assert "### Workspace Context 1" in rendered
    assert "<untrusted source='workspace:USER.md'>user profile</untrusted>" in rendered


def test_bootstrap_md_alongside_other_files_keeps_named_heading_and_renumbers_others() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={
            "AGENTS.md": "agents body",
            "BOOTSTRAP.md": "bootstrap body",
            "USER.md": "user body",
        },
        extra_context=None,
    )

    assert "### One-Shot Workspace Bootstrap\n\nbootstrap body" in rendered
    assert (
        "### Workspace Context 1\n\n"
        "<untrusted source='workspace:AGENTS.md'>agents body</untrusted>"
    ) in rendered
    assert (
        "### Workspace Context 2\n\n"
        "<untrusted source='workspace:USER.md'>user body</untrusted>"
    ) in rendered
    # BOOTSTRAP.md must not consume an index slot.
    assert "### Workspace Context 3" not in rendered


def test_bootstrap_md_suppressed_in_minimal_mode() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"BOOTSTRAP.md": "do the setup ritual"},
        extra_context=None,
        prompt_mode="minimal",
    )

    assert rendered == ""


def test_workspace_untrusted_wrapping_can_be_disabled() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"USER.md": "user profile"},
        extra_context=None,
        wrap_untrusted_workspace=False,
    )

    assert "### Workspace Context 1\n\nuser profile" in rendered
    assert "<untrusted" not in rendered


def test_subagent_prompt_compact_keeps_only_agents_and_tools(tmp_path) -> None:
    for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"):
        (tmp_path / name).write_text(f"{name} body", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=MagicMock(),
        config=SimpleNamespace(
            agent_name=None,
            tools=SimpleNamespace(profile=None),
            safety=SimpleNamespace(injection_scan_mode="off", wrap_untrusted_workspace=False),
            subagents=SimpleNamespace(prompt_compact=True),
            memory=SimpleNamespace(inject_limit=4000),
            heartbeat_prompt=None,
        ),
    )
    runner._resolve_bootstrap_workspace_dir = lambda _agent_id: tmp_path
    runner._resolve_memory_source_dir = lambda _agent_id: tmp_path

    assembled = runner._assemble_prompt(
        "main",
        [],
        session_key="agent:main:subagent:run-1",
    )

    assert isinstance(assembled, tuple)
    dynamic_suffix = assembled[1]
    assert "AGENTS.md body" in dynamic_suffix
    assert "TOOLS.md body" in dynamic_suffix
    assert "USER.md body" not in dynamic_suffix
    assert "SOUL.md body" not in dynamic_suffix


def test_assemble_prompt_injects_ownership_only_when_delegate_is_visible(tmp_path) -> None:
    (tmp_path / "USER.md").write_text("profile", encoding="utf-8")
    runner = TurnRunner(
        provider_selector=MagicMock(),
        config=SimpleNamespace(
            agent_name=None,
            tools=SimpleNamespace(profile=None),
            safety=SimpleNamespace(injection_scan_mode="off", wrap_untrusted_workspace=False),
            subagents=SimpleNamespace(prompt_compact=False, delegation_policy="aggressive"),
            memory=SimpleNamespace(inject_limit=4000),
            heartbeat_prompt=None,
        ),
    )
    runner._resolve_bootstrap_workspace_dir = lambda _agent_id: tmp_path
    runner._resolve_memory_source_dir = lambda _agent_id: tmp_path

    without_delegate = runner._assemble_prompt("main", [], session_key="agent:main:run-1")
    with_delegate = runner._assemble_prompt(
        "main",
        [SimpleNamespace(name="delegate_task")],
        session_key="agent:main:run-2",
        tool_context=ToolContext(
            caller_kind=CallerKind.AGENT,
            orchestration_complex_mode=True,
        ),
    )

    without_text = (
        "\n".join(without_delegate)
        if isinstance(without_delegate, tuple)
        else without_delegate
    )
    with_text = (
        "\n".join(with_delegate) if isinstance(with_delegate, tuple) else with_delegate
    )
    assert "Subagent Delegation Policy" not in without_text
    assert "Subagent Delegation Policy" in with_text
    assert DELEGATION_OWNERSHIP_PROMPT in with_text
    cacheable_system, dynamic_context = (
        with_delegate if isinstance(with_delegate, tuple) else (with_delegate, "")
    )
    assert not cacheable_system.startswith("## Subagent Delegation Policy")
    assert cacheable_system.endswith(DELEGATION_OWNERSHIP_PROMPT)
    assert "Subagent Delegation Policy" in cacheable_system
    assert DELEGATION_OWNERSHIP_PROMPT in cacheable_system
    assert "Subagent Delegation Policy" not in dynamic_context
    without_dynamic = without_delegate[1] if isinstance(without_delegate, tuple) else ""
    assert "Delegation check" not in without_dynamic


def test_assemble_prompt_ignores_removed_delegation_policy_switch(tmp_path) -> None:
    runner = TurnRunner(
        provider_selector=MagicMock(),
        config=SimpleNamespace(
            agent_name=None,
            tools=SimpleNamespace(profile=None),
            safety=SimpleNamespace(injection_scan_mode="off", wrap_untrusted_workspace=False),
            subagents=SimpleNamespace(prompt_compact=False, delegation_policy="off"),
            memory=SimpleNamespace(inject_limit=4000),
            heartbeat_prompt=None,
        ),
    )
    runner._resolve_bootstrap_workspace_dir = lambda _agent_id: tmp_path
    runner._resolve_memory_source_dir = lambda _agent_id: tmp_path

    assembled = runner._assemble_prompt(
        "main",
        [SimpleNamespace(name="delegate_task")],
        session_key="agent:main:run-off",
    )

    text = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    assert DELEGATION_OWNERSHIP_PROMPT not in text
    assert "Subagent Delegation Policy" not in text


def test_assemble_prompt_does_not_repeat_parent_delegation_policy_for_subagents(tmp_path) -> None:
    runner = TurnRunner(
        provider_selector=MagicMock(),
        config=SimpleNamespace(
            agent_name=None,
            tools=SimpleNamespace(profile=None),
            safety=SimpleNamespace(injection_scan_mode="off", wrap_untrusted_workspace=False),
            subagents=SimpleNamespace(prompt_compact=False, delegation_policy="aggressive"),
            memory=SimpleNamespace(inject_limit=4000),
            heartbeat_prompt=None,
        ),
    )
    runner._resolve_bootstrap_workspace_dir = lambda _agent_id: tmp_path
    runner._resolve_memory_source_dir = lambda _agent_id: tmp_path

    assembled = runner._assemble_prompt(
        "main",
        [SimpleNamespace(name="delegate_task")],
        session_key="agent:main:subagent:child-1",
    )

    text = "\n".join(assembled) if isinstance(assembled, tuple) else assembled
    assert "Subagent Delegation Policy" not in text
    assert "Delegation check:" not in text
