from __future__ import annotations

from pathlib import Path

from opensquilla.identity.prompt import assemble_system_prompt
from opensquilla.identity.types import AgentProfile


def test_default_user_template_names_profile_fields() -> None:
    template = Path("src/opensquilla/identity/templates/bootstrap/USER.md").read_text(
        encoding="utf-8"
    )

    assert "Name:" in template
    assert "What to call them:" in template
    assert "Pronouns:" in template
    assert "Timezone:" in template
    assert "Notes:" in template
    assert "## Context" in template
    assert "Do not put secrets" in template
    assert "one-off task notes" in template


def test_default_bootstrap_templates_define_distinct_file_roles() -> None:
    template_dir = Path("src/opensquilla/identity/templates/bootstrap")

    agents = (template_dir / "AGENTS.md").read_text(encoding="utf-8")
    soul = (template_dir / "SOUL.md").read_text(encoding="utf-8")
    identity = (template_dir / "IDENTITY.md").read_text(encoding="utf-8")
    tools = (template_dir / "TOOLS.md").read_text(encoding="utf-8")
    memory = (template_dir / "MEMORY.md").read_text(encoding="utf-8")

    assert "operating rules" in agents
    assert "Do not store user profile facts here" in agents
    assert "voice, tone, and interaction style" in soul
    assert "Do not store user profile facts, task history, or tool inventories here" in soul
    assert "agent's public-facing name" in identity
    assert "If the user asks to rename the assistant" in identity
    assert "local tool conventions" in tools
    assert "does not register tools, grant permissions, or change tool policy" in tools
    assert "durable non-profile facts" in memory
    assert "Agent name, tone, and persona belong in IDENTITY.md or SOUL.md" in memory


def test_system_prompt_routes_profile_to_user_md() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "write_file", "edit_file", "apply_patch"],
    )

    assert "USER.md" in prompt
    assert "name, preferred address, pronouns, timezone" in prompt
    assert "Do not use `memory_save` for `USER.md`" in prompt
    assert "MEMORY.md` for durable non-profile facts" in prompt
    assert "`MEMORY.md` + `memory/**/*.md`" in prompt
    assert "relevant `USER.md`, `MEMORY.md`, or `memory/**/*.md` file" in prompt
    assert "decisions, dates, people, preferences, or todos" not in prompt
    assert "prior work, decisions, dated history, todos" in prompt


def test_system_prompt_requires_bare_single_token_silent_replies() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=None,
    )

    assert "output that single bare token and nothing else" in prompt
    assert "Do not wrap it in Markdown" in prompt
    assert "Goal continuation has user-visible information" in prompt
    assert "Never use `NO_REPLY` for messages from a human user" in prompt


def test_system_prompt_disambiguates_session_memory_results() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get"],
    )

    assert "By default, `memory_search` searches curated memory source files" in prompt
    assert "source=sessions" in prompt
    assert "source=all" in prompt
    assert "raw turn captures or raw fallback files" in prompt
    assert "For `source: memory` results, use `memory_get`" in prompt
    assert "For `source: sessions` results, use the returned snippet" in prompt
    assert "`sessions/...` paths are virtual index sources" in prompt
    assert "Prefer curated `MEMORY.md`/`memory/**/*.md` facts" in prompt
    assert "not automatically as current truth" in prompt
    assert "include the returned citation or path#line" in prompt
    assert "Do not invent citations" in prompt


def test_system_prompt_routes_exact_transcript_search_to_session_search() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "session_search"],
    )

    assert "`session_search`" in prompt
    assert "exact prior chat wording" in prompt
    assert "transcript context" in prompt
    assert "code snippets from persisted sessions" in prompt
    assert "Ordinary recall should start with default curated `memory_search`" in prompt
    assert "debug" not in prompt.lower()


def test_system_prompt_routes_agent_identity_away_from_memory_md() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "write_file", "edit_file", "apply_patch"],
    )

    assert "Agent identity: `IDENTITY.md`" in prompt
    assert "Agent persona: `SOUL.md`" in prompt
    assert "Do not put assistant rename/persona requests into `MEMORY.md`" in prompt


def test_system_prompt_only_documents_canonical_tool_names() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["image_generate", "sessions_spawn", "sessions_send", "subagents"],
    )

    assert "`image_generate`" in prompt
    assert "generate_image" not in prompt
    assert "spawn_subagent" not in prompt
    assert "send_message" not in prompt


def test_system_prompt_requires_tool_preambles_and_conversation_language() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command", "apply_patch"],
    )

    assert "Before invoking a tool, send a brief user-visible note" in prompt
    assert "same language as the user's current conversation" in prompt
    assert "Use the conversation's language for all user-visible replies" in prompt
    assert "If the user writes in Chinese, keep replying in Chinese" in prompt


def test_system_prompt_describes_managed_execution_run_mode() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command"],
        runtime_info={
            "os": "Windows",
            "shell": "powershell",
            "workspace_dir": r"C:\OpenSquilla\workspace",
            "run_mode": "trusted",
            "run_mode_label": "Managed Execution",
        },
    )

    assert "Run mode: Managed Execution" in prompt
    assert "explicit host-affecting actions can run on the host when policy allows" in prompt
    install_guidance = (
        "Do not refuse a user-requested installation merely because the default path "
        "starts sandboxed"
    )
    assert install_guidance in prompt


def test_headless_repo_coding_scaffold_edit_prompt_matches_visible_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_repo_coding_scaffold"),
        tools=[
            "exec_command",
            "read_file",
            "edit_file",
            "write_file",
            "grep_search",
            "glob_search",
            "list_dir",
            "git_status",
            "git_diff",
            "retrieve_tool_result",
        ],
    )

    assert "## Repository Coding Scaffold" in prompt
    assert "`grep_search`" in prompt
    assert "`glob_search`" in prompt
    assert "`list_dir`" in prompt
    assert "`read_file`" in prompt
    assert "`edit_file`" in prompt
    assert "`write_file`" in prompt
    assert "`apply_patch`" not in prompt
    assert "`exec_command`" in prompt
    assert "`git_status`" in prompt
    assert "`git_diff`" in prompt
    assert "## Product Identity" not in prompt
    assert "read_source" not in prompt
    assert "edit_source" not in prompt
    assert "source_symbols" not in prompt


def test_headless_repo_coding_scaffold_patch_prompt_matches_visible_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_repo_coding_scaffold"),
        tools=[
            "exec_command",
            "read_file",
            "edit_file",
            "write_file",
            "apply_patch",
            "grep_search",
            "glob_search",
            "list_dir",
            "git_status",
            "git_diff",
            "retrieve_tool_result",
        ],
    )

    assert "## Repository Coding Scaffold" in prompt
    assert "`grep_search`" in prompt
    assert "`glob_search`" in prompt
    assert "`list_dir`" in prompt
    assert "`read_file`" in prompt
    assert "`edit_file`" in prompt
    assert "`write_file`" in prompt
    assert "`apply_patch`" in prompt
    assert "`exec_command`" in prompt
    assert "`git_status`" in prompt
    assert "`git_diff`" in prompt
    assert "## Product Identity" not in prompt
    assert "read_source" not in prompt
    assert "edit_source" not in prompt
    assert "source_symbols" not in prompt


def test_headless_repo_coding_scaffold_omits_hidden_git_tool_guidance() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_repo_coding_scaffold"),
        tools=["exec_command", "read_file", "edit_file", "write_file"],
    )

    assert "## Repository Coding Scaffold" in prompt
    assert "Use `git_status` to inspect the final repository state" not in prompt
    assert "Use `git_diff` to inspect the final source diff" not in prompt


def test_patch_evidence_protocol_renders_when_enabled_in_scaffold_mode() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(
            agent_id="main",
            prompt_mode="headless_repo_coding_scaffold",
            patch_evidence_protocol=True,
        ),
        tools=[
            "exec_command",
            "read_file",
            "edit_file",
            "write_file",
            "grep_search",
            "glob_search",
            "list_dir",
            "git_status",
            "git_diff",
        ],
    )

    assert "## Patch Evidence Protocol" in prompt
    assert "## Repository Coding Scaffold" in prompt
    assert "not sufficient final evidence by itself" in prompt
    assert "Do not modify existing test expectations" in prompt
    assert "change hypothesis or inspect a different implementation layer" in prompt
    assert "neighboring existing test" in prompt
    assert "strongest command/output evidence" in prompt


def test_patch_evidence_protocol_absent_by_default() -> None:
    scaffold_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_repo_coding_scaffold"),
        tools=["exec_command", "read_file", "edit_file", "git_diff"],
    )
    full_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command", "read_file", "edit_file", "git_diff"],
    )

    assert "## Patch Evidence Protocol" not in scaffold_prompt
    assert "## Patch Evidence Protocol" not in full_prompt


def test_patch_evidence_protocol_requires_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(
            agent_id="main",
            prompt_mode="headless_repo_coding_scaffold",
            patch_evidence_protocol=True,
        ),
        tools=None,
    )

    assert "## Patch Evidence Protocol" not in prompt


def test_finalize_evidence_gate_section_renders_when_enabled() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(
            agent_id="main",
            prompt_mode="headless_repo_coding_scaffold",
            finalize_evidence_gate=True,
        ),
        tools=["exec_command", "read_file", "edit_file", "git_diff"],
    )

    assert "## Reproduction Evidence" in prompt
    assert "binding evidence that the issue is not fixed yet" in prompt
    assert "exits non-zero while the bug is present" in prompt
    assert "re-run your reproduction and the most relevant existing test" in prompt
    # The section must not contain minimality directives or wording that
    # devalues reproduction evidence.
    section = prompt.split("## Reproduction Evidence", 1)[1].split("## ", 1)[0]
    assert "minimal" not in section.lower()
    assert "not sufficient" not in section.lower()


def test_finalize_evidence_gate_section_absent_by_default() -> None:
    scaffold_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_repo_coding_scaffold"),
        tools=["exec_command", "read_file", "edit_file", "git_diff"],
    )
    full_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command", "read_file", "edit_file", "git_diff"],
    )

    assert "## Reproduction Evidence" not in scaffold_prompt
    assert "## Reproduction Evidence" not in full_prompt


def test_finalize_evidence_gate_section_requires_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(
            agent_id="main",
            prompt_mode="headless_repo_coding_scaffold",
            finalize_evidence_gate=True,
        ),
        tools=None,
    )

    assert "## Reproduction Evidence" not in prompt


def test_system_prompt_disambiguates_session_send_from_channel_message() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["sessions_send", "message"],
    )

    assert "agent-to-agent or session-to-session" in prompt
    assert "`sessions_send`" in prompt
    assert "`message` only for channel adapter delivery" in prompt
    assert "send_message" not in prompt


def test_prompt_guides_web_tool_selection_for_source_backed_answers() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["web_search", "web_discover", "web_fetch"],
    )

    assert "## Web Research Tools" in prompt
    assert "Prefer `web_search`" in prompt
    assert "`web_search`" in prompt
    assert "`web_discover` for lightweight link discovery" in prompt
    assert "`web_fetch` for a specific URL" in prompt
    assert "citation-ready excerpts" in prompt
    assert "research_search" not in prompt


def test_prompt_handles_partial_web_tool_availability() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["web_fetch"],
    )

    assert "## Web Research Tools" in prompt
    assert "Prefer `research_search`" not in prompt
    assert "research_search" not in prompt
    assert "`web_fetch` for a specific URL" in prompt
    assert "known page needs deeper inspection" in prompt


def test_system_prompt_guides_generated_file_delivery() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["execute_code", "publish_artifact", "image_generate"],
    )

    assert "## Generated File Delivery" in prompt
    assert "Do not paste full file source" in prompt
    assert "call `publish_artifact` for the final file" in prompt
    assert "local entry path" in prompt
    assert "Do not invent artifact download URLs" in prompt
    assert "do not call `publish_artifact` again" in prompt
    assert "After `publish_artifact` succeeds" in prompt
    assert "final response" in prompt


def test_system_prompt_limits_file_delivery_when_no_file_authoring_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["publish_artifact", "image_generate", "read_file", "glob_search"],
    )

    assert "## Generated File Delivery Limits" in prompt
    assert "already exists in the workspace" in prompt
    assert "file creation is not enabled for this session" in prompt
    assert "surface where file authoring is enabled" in prompt
    assert "Do not paste full file source" in prompt
    assert "create the file in the active workspace" not in prompt


def test_system_prompt_describes_structured_artifact_fallback_limits() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["publish_artifact", "create_pptx", "image_generate"],
    )

    assert "## Structured Generated File Delivery" in prompt
    assert "only when the request fits the tool schema" in prompt
    assert "`create_pptx` creates a basic text-only deck" in prompt
    assert "create, send, deliver, or attach" in prompt
    assert "call `create_pptx`" in prompt
    assert "Do not substitute a PDF, CSV, XLSX, Python script, OOXML" in prompt
    assert "full visual deck authoring is not enabled" in prompt
    assert "file creation is not enabled for this session" not in prompt


def test_legacy_image_alias_does_not_enable_image_generation_prompt() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["generate_image"],
    )

    assert "MUST call the `image_generate` tool" not in prompt
    assert "Image generation is not available in this session" in prompt


def test_template_no_longer_renders_duplicate_skills_section() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search"],
        skills=["memory"],
    )

    assert "## Skills (mandatory)" not in prompt
    assert "Available skills:" not in prompt


def test_headless_source_edit_prompt_is_source_edit_focused() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_source_edit"),
        tools=[
            "read_source",
            "edit_source",
            "create_source",
            "write_scratch",
            "source_symbols",
            "grep_search",
            "glob_search",
            "read_file",
            "list_dir",
            "exec_command",
            "git_diff",
            "git_status",
            "retrieve_tool_result",
        ],
        runtime_info={
            "workspace_dir": "/testbed",
            "os": "Linux",
            "shell": "/bin/bash",
        },
    )

    assert "## Source Edit Contract" in prompt
    assert "Use `grep_search`, `glob_search`, and `source_symbols`" in prompt
    assert "Use `read_source`" in prompt
    assert "Use `edit_source`" in prompt
    assert "Use `create_source`" in prompt
    assert "Use `write_scratch`" in prompt
    assert "Use `exec_command` mainly for tests" in prompt
    assert "Inspect the final source diff with `git_diff`" in prompt
    assert "Working directory: /testbed" in prompt
    assert "## Product Identity" not in prompt
    assert "## Tool Call Style" not in prompt
    assert "## OpenSquilla CLI Quick Reference" not in prompt
    assert "## Runtime" not in prompt


def test_headless_source_edit_prompt_omits_hidden_git_diff_guidance() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="headless_source_edit"),
        tools=["read_source", "edit_source", "exec_command"],
    )

    assert "## Source Edit Contract" in prompt
    assert "Inspect the final source diff with `git_diff`" not in prompt


def test_legacy_prompt_style_restores_compact_directives() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full", legacy_prompt_style=True),
        tools=["exec_command", "apply_patch"],
    )

    assert (
        "## Tool Call Style\n\n"
        "- Narrate what you are about to do before invoking a tool.\n"
        "- Only call tools when the task genuinely requires it."
    ) in prompt
    assert (
        "## Reply Guidelines\n\n"
        "- Use the conversation's language for replies\n"
        "- When uncertain, ask for clarification rather than guessing\n"
        "- Prefer concise replies unless detail is requested"
    ) in prompt
    assert "Before invoking a tool, send a brief user-visible note" not in prompt
    assert "same language as the user's current conversation" not in prompt
    assert "If the user writes in Chinese" not in prompt
    assert "Match reply length to the request" not in prompt


def test_legacy_prompt_style_absent_by_default() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command", "apply_patch"],
    )

    assert "Narrate what you are about to do before invoking a tool." not in prompt
    assert "- Use the conversation's language for replies\n" not in prompt
    assert "Prefer concise replies unless detail is requested" not in prompt


def test_legacy_prompt_style_restores_runtime_section_spacing() -> None:
    runtime_info = {"os": "Linux", "shell": "/bin/bash", "workspace_dir": "/tmp/ws"}

    legacy_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full", legacy_prompt_style=True),
        tools=["exec_command"],
        runtime_info=runtime_info,
    )
    default_prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["exec_command"],
        runtime_info=runtime_info,
    )

    # Legacy style keeps a blank separator line between the Runtime section
    # and the next header; the current style renders them adjacent.
    assert "- Shell: /bin/bash\n\n## Reply Guidelines" in legacy_prompt
    assert "- Shell: /bin/bash\n## Reply Guidelines" in default_prompt
