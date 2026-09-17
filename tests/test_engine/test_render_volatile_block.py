from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from opensquilla.engine.runtime import TurnRunner


def test_retired_bootstrap_md_has_no_special_rendering() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"BOOTSTRAP.md": "do the setup ritual"},
        extra_context=None,
    )

    assert rendered == ""


def test_bootstrap_md_absent_emits_no_bootstrap_heading() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={"USER.md": "user profile"},
        extra_context=None,
    )

    assert "One-Shot Workspace Bootstrap" not in rendered
    assert "### Workspace Context 1" in rendered
    assert "<untrusted source='workspace:USER.md'>user profile</untrusted>" in rendered


def test_retired_file_does_not_consume_a_context_slot() -> None:
    rendered = TurnRunner._render_volatile_block(
        daily_notes=None,
        workspace_files={
            "AGENTS.md": "agents body",
            "BOOTSTRAP.md": "bootstrap body",
            "USER.md": "user body",
        },
        extra_context=None,
    )

    assert "bootstrap body" not in rendered
    assert (
        "### Workspace Context 1\n\n<untrusted source='workspace:AGENTS.md'>agents body</untrusted>"
    ) in rendered
    assert (
        "### Workspace Context 2\n\n<untrusted source='workspace:USER.md'>user body</untrusted>"
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


def test_subagent_prompt_compact_keeps_only_agents(tmp_path) -> None:
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
    assert "TOOLS.md body" not in dynamic_suffix
    assert "USER.md body" not in dynamic_suffix
    assert "SOUL.md body" not in dynamic_suffix
