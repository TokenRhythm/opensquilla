"""Jinja2-based system prompt assembly for agent identity."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .types import AgentProfile

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def assemble_system_prompt(
    profile: AgentProfile,
    *,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    memory: str | None = None,
    timezone: str = "UTC",
    runtime_info: dict[str, str] | None = None,
    docs_path: str | None = None,
    owner_line: str | None = None,
    heartbeat_prompt: str | None = None,
    model_aliases: list[str] | None = None,
    reasoning_tag_hint: str | None = None,
) -> str:
    """Render the cacheable base of the system prompt for an agent profile.

    The output of this function is the prompt-cache-stable prefix.
    Per-turn volatile content (recalled-memory snippets, daily notes,
    workspace files, runtime extra_context) must NOT be injected here — it
    is appended by the runtime as a separate, uncached suffix via
    ``TurnRunner._render_volatile_block`` and ``_render_recall_block``.

    Args:
        profile: The agent profile containing identity and workspace context.
        tools: List of available tool names.
        skills: List of available skill names.
        memory: Memory section content (omitted in minimal mode).
        timezone: Current timezone string.
        runtime_info: OS/shell/workspace metadata for Runtime section.
        docs_path: Path to local documentation directory.
        owner_line: Authorized senders line (e.g. phone/ID allowlist).
        heartbeat_prompt: Heartbeat poll recognition and ack protocol text.
        model_aliases: List of model alias strings for Model Aliases section.
        reasoning_tag_hint: Reasoning format constraint (e.g. <think>/<final>).

    Returns:
        Rendered system prompt string (cacheable base only).
    """
    env = _make_env()
    template = env.get_template("system_prompt.j2")

    soul_body: str | None = None
    if profile.identity.soul:
        soul_body = profile.identity.soul.body or None

    ctx: dict = {
        "profile": profile,
        "identity": profile.identity,
        "soul_body": soul_body,
        "agents_doc": profile.agents_doc,
        "tools": tools or [],
        "skills": skills or [],
        "memory": memory,
        "timezone": timezone,
        "prompt_mode": profile.prompt_mode,
        "runtime_info": runtime_info,
        "docs_path": docs_path,
        "owner_line": owner_line,
        "heartbeat_prompt": heartbeat_prompt,
        "model_aliases": model_aliases or [],
        "reasoning_tag_hint": reasoning_tag_hint,
    }

    return template.render(**ctx).strip()
