"""Fail early when curated paper sources cannot meet the citation contract."""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from urllib.parse import urlparse

_INTEGER_FIELD = r"^\s*{name}\s*:\s*(?:>=|≥|at\s+least\s+)?(\d+)\b"
_MARKDOWN_FIELD_RE = re.compile(
    r"^(?P<indent>\s*)(?:#{1,6}\s+)?(?P<marker>\*\*|__)"
    r"(?P<name>[A-Z][A-Z0-9_ ]*?)"
    r"(?:(?P<colon_inside>:)(?P=marker)|(?P=marker)(?P<colon_outside>:))"
    r"(?P<value>[^\n]*)$",
)
_BIB_ENTRY_RE = re.compile(
    r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@\w+\s*\{|\Z)",
    re.DOTALL,
)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)


def _normalize_field_lines(text: str) -> str:
    """Unwrap Markdown emphasis only when it encloses an uppercase field label."""

    lines: list[str] = []
    for line in text.splitlines():
        match = _MARKDOWN_FIELD_RE.fullmatch(line)
        if match:
            name = match.group("name").strip()
            lines.append(f"{match.group('indent')}{name}:{match.group('value')}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _field_count(text: str, name: str) -> int:
    return len(re.findall(rf"^\s*{re.escape(name)}\s*:", text, re.I | re.M))


def _integer_field(text: str, name: str) -> int | None:
    match = re.search(_INTEGER_FIELD.format(name=re.escape(name)), text, re.I | re.M)
    return int(match.group(1)) if match else None


def _text_field(text: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}\s*:\s*([^\n]+?)\s*$", text, re.I | re.M)
    return match.group(1).strip() if match else None


def _section(text: str, *names: str) -> str:
    labels = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"^\s*(?:{labels})\s*:\s*(.*?)(?=^\s*[A-Z][A-Z0-9_ ]*\s*:|\Z)",
        text,
        re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def _keys_from_section(section: str) -> set[str]:
    keys = {
        match.group(1)
        for match in re.finditer(r"^\s*-\s*([A-Za-z0-9_.:-]+)\b", section, re.M)
    }
    if not keys and section.startswith("[") and "]" in section:
        keys.update(re.findall(r"[A-Za-z][A-Za-z0-9_.:-]*", section.partition("]")[0]))
    return keys


def _bibliography_entries(bibliography: str) -> dict[str, str]:
    return {
        match.group(1).strip(): match.group(2)
        for match in _BIB_ENTRY_RE.finditer(bibliography)
    }


def _unwrap_bib_value(raw_value: str) -> str:
    """Remove a field delimiter without accepting unbalanced wrapper braces."""

    value = raw_value.strip()
    if value.endswith(","):
        value = value[:-1].rstrip()
    while len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        depth = 0
        closes_at_end = False
        for index, character in enumerate(value):
            if character == "{" and (index == 0 or value[index - 1] != "\\"):
                depth += 1
            elif character == "}" and (index == 0 or value[index - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(value) - 1
                    break
                if depth < 0:
                    break
        if not closes_at_end:
            break
        value = value[1:-1].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


def _bib_fields(body: str) -> dict[str, list[str]]:
    """Parse top-level BibTeX fields, including compact one-line entries.

    A line-oriented regular expression misses valid fields when an entire
    entry is emitted on one line.  Looking for ``, url=`` anywhere is not safe
    either because locator-like text can occur inside a braced title.  This
    small scanner recognizes only top-level assignments and preserves their
    delimiters for the strict value validator below.
    """

    fields: dict[str, list[str]] = {}
    index = 0
    length = len(body)
    while index < length:
        while index < length and (body[index].isspace() or body[index] == ","):
            index += 1
        if index >= length or body[index] in "})":
            break

        name_match = re.match(r"[A-Za-z][A-Za-z0-9_-]*", body[index:])
        if name_match is None:
            break
        name = name_match.group(0).lower()
        index += name_match.end()
        while index < length and body[index].isspace():
            index += 1
        if index >= length or body[index] != "=":
            break
        index += 1
        while index < length and body[index].isspace():
            index += 1

        value_start = index
        if index < length and body[index] == "{":
            depth = 0
            while index < length:
                character = body[index]
                escaped = index > value_start and body[index - 1] == "\\"
                if character == "{" and not escaped:
                    depth += 1
                elif character == "}" and not escaped:
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                    if depth < 0:
                        break
                index += 1
            if depth != 0:
                break
        elif index < length and body[index] == '"':
            index += 1
            while index < length:
                if body[index] == '"' and body[index - 1] != "\\":
                    index += 1
                    break
                index += 1
            else:
                break
        else:
            while index < length and body[index] not in ",}":
                index += 1

        raw_value = body[value_start:index].strip()
        fields.setdefault(name, []).append(raw_value)
        while index < length and body[index].isspace():
            index += 1
        if index < length and body[index] == ",":
            index += 1
        elif index < length and body[index] not in "})":
            break
    return fields


def _valid_https_url(value: str) -> bool:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlparse(value)
        # Accessing ``port`` also rejects malformed bracketed hosts and ports.
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.hostname)


def _entry_has_valid_locator(body: str) -> bool:
    """Accept only a non-empty, strictly shaped offline locator."""

    for name, raw_values in _bib_fields(body).items():
        if name not in {"url", "howpublished", "doi", "eprint"}:
            continue
        for raw_value in raw_values:
            value = _unwrap_bib_value(raw_value)
            if name in {"url", "howpublished"}:
                url_wrapper = re.fullmatch(r"\\url\s*\{([^{}]*)\}", value, re.DOTALL)
                if url_wrapper is not None:
                    value = url_wrapper.group(1).strip()
                if _valid_https_url(value):
                    return True
            elif name == "doi" and _DOI_RE.fullmatch(value):
                return True
            elif name == "eprint" and _ARXIV_RE.fullmatch(value):
                return True
    return False


def audit(payload: dict[str, Any]) -> tuple[list[str], int, int, list[str]]:
    paper_contract = _normalize_field_lines(str(payload.get("paper_contract") or ""))
    paper_preferences = _normalize_field_lines(str(payload.get("paper_preferences") or ""))
    source_pack = _normalize_field_lines(str(payload.get("source_pack") or ""))
    bibliography = str(payload.get("bibliography") or "")

    target = _integer_field(paper_contract, "CITATION_TARGET")
    if target is None:
        target = _integer_field(paper_preferences, "CITATION_TARGET")

    status = (_text_field(source_pack, "SOURCE_STATUS") or "").lower()
    declared_target = _integer_field(source_pack, "CITATION_TARGET")
    declared_count = _integer_field(source_pack, "USABLE_REFERENCE_COUNT")
    usable_keys = _keys_from_section(_section(source_pack, "USABLE_KEYS"))
    primary_keys = _keys_from_section(
        _section(source_pack, "PRIMARY_REFERENCES", "PRIMARY_SOURCES"),
    )
    entries = _bibliography_entries(bibliography)

    candidate_keys = usable_keys & primary_keys
    verified_keys = sorted(
        key for key in candidate_keys if key in entries and _entry_has_valid_locator(entries[key])
    )
    found = len(verified_keys)
    required = target or declared_target or 0

    blockers: list[str] = []
    for name in ("SOURCE_STATUS", "CITATION_TARGET", "USABLE_REFERENCE_COUNT"):
        if _field_count(source_pack, name) > 1:
            blockers.append(f"source pack contains duplicate {name} fields")
    if _field_count(source_pack, "USABLE_KEYS") > 1:
        blockers.append("source pack contains duplicate USABLE_KEYS sections")
    if sum(
        _field_count(source_pack, name)
        for name in ("PRIMARY_REFERENCES", "PRIMARY_SOURCES")
    ) > 1:
        blockers.append("source pack contains duplicate primary-reference sections")
    if target is None:
        blockers.append("CITATION_TARGET must be a machine-readable integer before drafting")
    if status not in {"sufficient", "pass"}:
        rendered = status or "missing"
        blockers.append(f"SOURCE_STATUS is {rendered}, not sufficient")
    if declared_target is None:
        blockers.append("source pack omitted integer CITATION_TARGET")
    elif target is not None and declared_target != target:
        blockers.append(
            f"source-pack target {declared_target} does not match paper target {target}",
        )
    if declared_count is None:
        blockers.append("source pack omitted USABLE_REFERENCE_COUNT")
    elif declared_count != found:
        blockers.append(
            f"declared usable count {declared_count} does not match {found} verified primary keys",
        )
    if not usable_keys:
        blockers.append("USABLE_KEYS is empty or missing")
    if not primary_keys:
        blockers.append("PRIMARY_REFERENCES is empty or missing")

    missing_primary = sorted(usable_keys - primary_keys)
    if missing_primary:
        blockers.append(
            "usable keys absent from PRIMARY_REFERENCES: " + ", ".join(missing_primary),
        )
    missing_bib = sorted(candidate_keys - set(entries))
    if missing_bib:
        blockers.append("usable keys absent from bibliography: " + ", ".join(missing_bib))
    missing_locator = sorted(
        key
        for key in candidate_keys
        if key in entries and not _entry_has_valid_locator(entries[key])
    )
    if missing_locator:
        blockers.append(
            "usable bibliography entries lack URL/DOI/arXiv locator: "
            + ", ".join(missing_locator),
        )
    if required <= 0:
        blockers.append("citation target must be greater than zero")
    elif found < required:
        blockers.append(f"source coverage insufficient: found {found}/{required} usable references")

    return blockers, found, required, verified_keys


def _render(verdict: str, found: int, required: int, keys: list[str], blockers: list[str]) -> str:
    lines = [
        f"SOURCE_READINESS: {verdict}",
        f"CITATION_TARGET: {required}",
        f"FOUND_REFERENCES: {found}/{required}",
        "USABLE_KEYS: " + (", ".join(keys) if keys else "none"),
        "BLOCKERS:",
    ]
    lines.extend(f"- {blocker}" for blocker in blockers or ["none"])
    return "\n".join(lines)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        rendered = _render("block", 0, 0, [], [f"invalid JSON input: {exc}"])
        print(rendered)
        print(rendered, file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        rendered = _render("block", 0, 0, [], ["input must be a JSON object"])
        print(rendered)
        print(rendered, file=sys.stderr)
        return 2

    blockers, found, required, keys = audit(payload)
    rendered = _render("block" if blockers else "pass", found, required, keys, blockers)
    print(rendered)
    if blockers:
        # skill_exec surfaces stderr for non-zero wrapped skills. Mirror the
        # structured blocker there while keeping stdout useful for direct runs.
        print(rendered, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
