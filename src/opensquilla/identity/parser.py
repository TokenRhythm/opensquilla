"""Parsers for SOUL.md, IDENTITY.md, and AGENTS.md persona files."""

import re

from .types import AgentCapability, AgentsDocument, IdentityFields, SoulDocument

# Known placeholder values that should be ignored during IDENTITY.md parsing
# See THIRD_PARTY_NOTICES.md for attribution
_PLACEHOLDERS = frozenset(
    [
        "pick something you like",
        "ai? robot? familiar? ghost in the machine? something weirder?",
        "how do you come across? sharp? warm? chaotic? calm?",
        "your signature - pick one that feels right",
        # Stored post-normalization: parentheses removed → "https" not "http(s)"
        "workspace-relative path, https url, or data uri",
    ]
)

_KNOWN_IDENTITY_FIELDS = frozenset(["name", "emoji", "creature", "vibe", "theme", "avatar"])


def _strip_markdown_inline(text: str) -> str:
    """Strip inline markdown formatting: bold, italic, code."""
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _normalize_placeholder_check(value: str) -> str:
    """Normalize value for placeholder comparison."""
    value = _strip_markdown_inline(value)
    value = value.strip(" -")
    # Normalize dashes: em dash / en dash → hyphen
    value = value.replace("\u2014", "-").replace("\u2013", "-")
    # Collapse whitespace
    value = re.sub(r"\s+", " ", value)
    # Remove parentheses
    value = re.sub(r"[()]", "", value)
    return value.lower().strip()


def parse_soul(content: str) -> SoulDocument:
    """Parse SOUL.md: optional YAML frontmatter between --- markers + body."""
    frontmatter: dict[str, str] = {}
    body = content

    # Detect frontmatter block: starts with --- on first line
    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)", content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2)
        # Parse simple key: value YAML lines (no nesting)
        for line in fm_text.splitlines():
            kv = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)", line)
            if kv:
                frontmatter[kv.group(1)] = kv.group(2).strip()

    return SoulDocument(body=body.strip(), frontmatter=frontmatter)


def parse_identity(content: str) -> IdentityFields:
    """Parse IDENTITY.md: colon-separated label: value lines."""
    fields = IdentityFields()

    for line in content.splitlines():
        # Strip leading list markers
        line = re.sub(r"^[-*+]\s+", "", line).strip()
        if not line:
            continue

        colon_idx = line.find(":")
        if colon_idx < 1:
            continue

        raw_label = line[:colon_idx]
        raw_value = line[colon_idx + 1 :]

        # Normalize label
        label = _strip_markdown_inline(raw_label).strip().lower()
        if label not in _KNOWN_IDENTITY_FIELDS:
            continue

        # Normalize value
        value = _strip_markdown_inline(raw_value).strip()
        normalized = _normalize_placeholder_check(value)
        if not normalized or normalized in _PLACEHOLDERS:
            continue

        match label:
            case "name":
                fields.name = value
            case "emoji":
                fields.emoji = value
            case "creature":
                fields.creature = value
            case "vibe":
                fields.vibe = value
            case "theme":
                fields.theme = value
            case "avatar":
                fields.avatar = value

    return fields


def parse_agents(content: str) -> AgentsDocument:
    """Parse AGENTS.md: extract agent capability declarations."""
    capabilities: list[AgentCapability] = []

    # Look for lines like "- **CapabilityName**: description" or "## Section" headers
    for line in content.splitlines():
        # Match bullet capability declarations: - **Name**: description
        cap_match = re.match(r"^[-*+]\s+\*{1,2}([^*]+)\*{1,2}:?\s*(.*)", line)
        if cap_match:
            name = cap_match.group(1).strip()
            description = cap_match.group(2).strip()
            capabilities.append(AgentCapability(name=name, description=description))

    return AgentsDocument(raw=content, capabilities=capabilities)
