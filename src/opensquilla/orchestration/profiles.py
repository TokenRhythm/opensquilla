"""Fixed subagent profiles and post-inheritance capability filtering."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentProfile:
    name: str
    capability_summary: str = ""
    allow: frozenset[str] | None = None
    deny: frozenset[str] = field(default_factory=frozenset)
    required: frozenset[str] = field(default_factory=frozenset)
    may_delegate: bool = False


class ProfileCapabilityError(ValueError):
    def __init__(
        self,
        message: str = "Agent profile capabilities are unavailable",
        *,
        unknown: frozenset[str] = frozenset(),
        missing: frozenset[str] = frozenset(),
    ) -> None:
        self.unknown = unknown
        self.missing = missing
        details: list[str] = []
        if unknown:
            details.append(f"unknown={','.join(sorted(unknown))}")
        if missing:
            details.append(f"missing={','.join(sorted(missing))}")
        suffix = f" ({'; '.join(details)})" if details else ""
        super().__init__(message + suffix)


_LOCAL_READ_TOOLS = frozenset(
    {
        "document_inspect",
        "document_locate",
        "document_read",
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep_search",
        "list_dir",
        "read_file",
        "read_source",
        "retrieve_tool_result",
        "source_symbols",
        "tool_search",
    }
)

_MUTATING_TOOLS = frozenset(
    {
        "apply_patch",
        "background_process",
        "create_csv",
        "create_pdf_report",
        "create_pptx",
        "create_source",
        "create_xlsx",
        "document_apply",
        "document_finish",
        "document_patch",
        "edit_file",
        "edit_source",
        "execute_code",
        "git_commit",
        "process",
        "publish_artifact",
        "write_file",
    }
)

_DELEGATION_TOOLS = frozenset({"delegate_task", "interrupt_agent"})
_TASK_BOARD_TOOLS = frozenset({"task_board"})

_WORKER_TOOLS = (
    _LOCAL_READ_TOOLS
    | _MUTATING_TOOLS
    | _DELEGATION_TOOLS
    | _TASK_BOARD_TOOLS
    | {
        "exec_command",
        "request_user_input",
    }
)

_EXPLORER_TOOLS = _LOCAL_READ_TOOLS | _TASK_BOARD_TOOLS

_RESEARCHER_TOOLS = (
    _LOCAL_READ_TOOLS
    | _TASK_BOARD_TOOLS
    | {
        "document_browser_act",
        "document_browser_inspect",
        "document_browser_reload",
        "document_browser_screenshot",
        "http_request",
        "pdf",
        "web_discover",
        "web_fetch",
        "web_search",
    }
)

_REVIEWER_TOOLS = _LOCAL_READ_TOOLS | _TASK_BOARD_TOOLS


PRESET_PROFILES: dict[str, AgentProfile] = {
    "inherit": AgentProfile(
        name="inherit",
        capability_summary="keeps all inherited worker capabilities",
        allow=None,
        may_delegate=True,
    ),
    "worker": AgentProfile(
        name="worker",
        capability_summary="can read and edit files, run commands, verify work, and delegate",
        allow=frozenset(_WORKER_TOOLS),
        required=frozenset(
            {"read_file", "exec_command", "apply_patch", "delegate_task", "interrupt_agent"}
        ),
        may_delegate=True,
    ),
    "explorer": AgentProfile(
        name="explorer",
        capability_summary=(
            "can only read local files and search code; cannot run commands, tests, or edit files"
        ),
        allow=frozenset(_EXPLORER_TOOLS),
        deny=_MUTATING_TOOLS | _DELEGATION_TOOLS,
        required=frozenset({"read_file", "grep_search"}),
        may_delegate=False,
    ),
    "researcher": AgentProfile(
        name="researcher",
        capability_summary=(
            "can only read local files and use web research; cannot run local commands, tests, "
            "or edit files"
        ),
        allow=frozenset(_RESEARCHER_TOOLS),
        deny=_MUTATING_TOOLS | _DELEGATION_TOOLS,
        required=frozenset({"web_search", "web_fetch"}),
        may_delegate=False,
    ),
    "reviewer": AgentProfile(
        name="reviewer",
        capability_summary=(
            "can only read files and diffs; cannot run commands, tests, or edit files"
        ),
        allow=frozenset(_REVIEWER_TOOLS),
        deny=_MUTATING_TOOLS | _DELEGATION_TOOLS,
        required=frozenset({"read_file", "git_diff"}),
        may_delegate=False,
    ),
}


def get_profile(name: str) -> AgentProfile:
    try:
        return PRESET_PROFILES[name]
    except KeyError as exc:
        raise ProfileCapabilityError(f"Unknown agent profile: {name}") from exc


def resolve_profile_tools(
    *,
    inherited: frozenset[str],
    registered: frozenset[str],
    preset: AgentProfile,
) -> frozenset[str]:
    """Apply a fixed profile after inheritance without widening authority."""

    unknown = (
        frozenset()
        if preset.allow is None
        else frozenset(tool for tool in preset.allow if tool not in registered)
    )
    effective = inherited if preset.allow is None else inherited & preset.allow
    effective = frozenset(tool for tool in effective if tool not in preset.deny)
    missing = frozenset(tool for tool in preset.required if tool not in effective)
    if unknown or missing:
        raise ProfileCapabilityError(unknown=unknown, missing=missing)
    return effective


__all__ = [
    "PRESET_PROFILES",
    "AgentProfile",
    "ProfileCapabilityError",
    "get_profile",
    "resolve_profile_tools",
]
