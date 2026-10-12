"""Build provider-visible context views from durable session state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from opensquilla.provider.types import ContentBlockCompaction, Message
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.context_state_selection import (
    latest_context_state,
    latest_context_states_by_covered_through_id,
    ordered_context_states,
)
from opensquilla.session.models import SessionContextState, SessionSummary

_ANTHROPIC_COMPACTION_STATE_KIND = "anthropic_compaction_block"
_COMPACTION_SUMMARY_CONTEXT_HEADER = "[Compacted Session Summaries]"
_COMPACTION_SUMMARY_CONTEXT_MAX_CHARS = 16_000
_STRUCTURED_COMPACTION_SUMMARY_HEADER = "[Structured Compaction Summary]"
_STRUCTURED_SUMMARY_SECTION_ORDER = (
    "Goal",
    "Current Status",
    "Next Action",
    "Completed Steps",
    "Open Steps",
    "Files and Artifacts",
    "Tool Results To Remember",
    "Decisions and Rationale",
    "Known Failures",
    "Executed Commands and Tests",
    "Pending Tool and Approval IDs",
    "Important Identifiers",
    "Constraints and Preferences",
    "Do Not Repeat",
    "Unresolved Questions",
    "Critical Carry Forward",
)
_STRUCTURED_SUMMARY_SECTION_PRIORITY = (
    "Goal",
    "Next Action",
    "Pending Tool and Approval IDs",
    "Critical Carry Forward",
    "Constraints and Preferences",
    "Current Status",
    "Open Steps",
    "Known Failures",
    "Unresolved Questions",
    "Important Identifiers",
    "Decisions and Rationale",
    "Tool Results To Remember",
    "Files and Artifacts",
    "Do Not Repeat",
    "Completed Steps",
    "Executed Commands and Tests",
)
_COMPACTION_SECTION_OMISSION_MARKER = (
    "[Omitted from request replay to fit the context budget.]"
)
_LEGACY_SUMMARY_MIDDLE_OMISSION_MARKER = (
    "[Legacy compaction summary text omitted in the middle to fit the context budget.]"
)


@dataclass(frozen=True)
class ProviderCompactionContext:
    messages: list[Message]
    covered_through_ids: set[int]


@dataclass(frozen=True)
class CompactionContextItem:
    text: str
    compaction_id: str | None
    source: str
    covered_through_id: int


@dataclass(frozen=True)
class _StructuredSummarySection:
    title: str
    text: str


def _split_structured_summary_sections(
    text: str,
) -> list[_StructuredSummarySection] | None:
    """Split renderer-owned structured text without cutting section values."""

    lines = text.splitlines()
    if not lines or lines[0] != _STRUCTURED_COMPACTION_SUMMARY_HEADER:
        return None

    title_order = {
        title: index for index, title in enumerate(_STRUCTURED_SUMMARY_SECTION_ORDER)
    }
    starts: list[tuple[int, str]] = []
    seen: set[str] = set()
    previous_order = -1
    for line_index, line in enumerate(lines[1:], start=1):
        if not line.endswith(":"):
            continue
        title = line[:-1]
        order = title_order.get(title)
        if order is None:
            continue
        # Renderer-owned section headers are separated from the previous
        # section by a blank line. Requiring that boundary avoids treating
        # ordinary one-line values as headers.
        if line_index > 1 and lines[line_index - 1].strip():
            continue
        if title in seen or order <= previous_order:
            # Ambiguous text is safer as one atomic summary than as guessed
            # sections that could be split in the middle of a field.
            return None
        seen.add(title)
        previous_order = order
        starts.append((line_index, title))

    if not starts:
        return [] if not any(line.strip() for line in lines[1:]) else None
    if any(line.strip() for line in lines[1 : starts[0][0]]):
        return None

    sections: list[_StructuredSummarySection] = []
    for index, (start, title) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end]).rstrip()
        if not block:
            return None
        sections.append(_StructuredSummarySection(title=title, text=block))
    return sections


def _render_structured_sections(
    sections: Sequence[_StructuredSummarySection],
) -> str:
    body = "\n\n".join(section.text for section in sections)
    if not body:
        return _STRUCTURED_COMPACTION_SUMMARY_HEADER
    return f"{_STRUCTURED_COMPACTION_SUMMARY_HEADER}\n\n{body}"


def _pack_structured_summary_sections(text: str, *, max_chars: int) -> str | None:
    """Pack complete structured sections, explicitly marking every omission."""

    sections = _split_structured_summary_sections(text)
    if sections is None:
        return None

    packed_by_title = {
        section.title: _StructuredSummarySection(
            title=section.title,
            text=f"{section.title}:\n{_COMPACTION_SECTION_OMISSION_MARKER}",
        )
        for section in sections
    }

    def _render_current() -> str:
        return _render_structured_sections(
            [packed_by_title[section.title] for section in sections]
        )

    packed = _render_current()
    if len(packed) > max_chars:
        return None

    source_by_title = {section.title: section for section in sections}
    for title in _STRUCTURED_SUMMARY_SECTION_PRIORITY:
        source = source_by_title.get(title)
        if source is None:
            continue
        omitted = packed_by_title[title]
        packed_by_title[title] = source
        candidate = _render_current()
        if len(candidate) <= max_chars:
            packed = candidate
        else:
            packed_by_title[title] = omitted
    return packed


def _bounded_legacy_fragment(text: str, *, max_chars: int, from_end: bool) -> str:
    """Keep a readable word/line-bounded edge of otherwise opaque legacy text."""

    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    candidate = text[-max_chars:] if from_end else text[:max_chars]
    if from_end:
        newline = candidate.find("\n")
        whitespace = candidate.find(" ")
        boundaries = [value for value in (newline, whitespace) if value >= 0]
        boundary = min(boundaries, default=-1)
        if 0 <= boundary < max_chars // 2:
            candidate = candidate[boundary + 1 :]
        return candidate.lstrip()

    newline = candidate.rfind("\n")
    whitespace = candidate.rfind(" ")
    boundary = max(newline, whitespace)
    if boundary >= max_chars // 2:
        candidate = candidate[:boundary]
    return candidate.rstrip()


def _pack_legacy_summary_text(text: str, *, max_chars: int) -> str | None:
    """Preserve both continuity edges of an oversized pre-structured summary."""

    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= max_chars:
        return cleaned

    separator = f"\n{_LEGACY_SUMMARY_MIDDLE_OMISSION_MARKER}\n"
    content_budget = max_chars - len(separator)
    if content_budget < 2:
        return None

    # Goals and original framing tend to live at the start, while the latest
    # status and next action tend to live at the end. Reserve both explicitly.
    head_budget = max(1, content_budget // 3)
    tail_budget = max(1, content_budget - head_budget)
    head = _bounded_legacy_fragment(
        cleaned,
        max_chars=head_budget,
        from_end=False,
    )
    tail = _bounded_legacy_fragment(
        cleaned,
        max_chars=tail_budget,
        from_end=True,
    )
    if not head and not tail:
        return None
    packed = f"{head}{separator}{tail}"
    return packed if len(packed) <= max_chars else None


def _summary_omission_marker(count: int) -> str:
    noun = "summary" if count == 1 else "summaries"
    return (
        f"[Omitted {count} earlier compaction {noun} "
        "from request replay to fit the context budget.]"
    )


def _render_summary_context_blocks(
    blocks: Sequence[tuple[int, str]],
    *,
    omitted_earlier: int,
) -> str:
    body: list[str] = []
    if omitted_earlier:
        body.append(_summary_omission_marker(omitted_earlier))
    body.extend(block for _, block in sorted(blocks))
    return f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n" + "\n\n".join(body)


def format_compaction_summary_context(summary_texts: Sequence[str]) -> str | None:
    """Render portable checkpoints exactly as the request-context path does."""

    deduped: list[str] = []
    seen: set[str] = set()
    for raw in summary_texts:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    if not deduped:
        return None

    blocks = [
        f"[Summary {index}]\n{text}"
        for index, text in enumerate(deduped, start=1)
    ]
    rendered = (
        f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n"
        + "\n\n".join(blocks)
    )
    if len(rendered) <= _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS:
        return rendered

    # Budget from newest to oldest, but retain the original chronological
    # rendering order. Structured summaries keep complete sections; opaque
    # legacy summaries keep explicitly delimited head/tail continuity so old
    # installations do not lose the entire checkpoint.
    selected: list[tuple[int, str]] = []
    omitted_earlier = 0
    for index in range(len(deduped) - 1, -1, -1):
        summary_number = index + 1
        full_block = f"[Summary {summary_number}]\n{deduped[index]}"
        full_candidate = _render_summary_context_blocks(
            [*selected, (summary_number, full_block)],
            omitted_earlier=index,
        )
        if len(full_candidate) <= _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS:
            selected.append((summary_number, full_block))
            continue

        block_prefix = f"[Summary {summary_number}]\n"
        prefix_candidate = _render_summary_context_blocks(
            [*selected, (summary_number, block_prefix)],
            omitted_earlier=index,
        )
        section_budget = (
            _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS - len(prefix_candidate)
        )
        packed = _pack_structured_summary_sections(
            deduped[index],
            max_chars=section_budget,
        )
        if (
            packed is None
            and not deduped[index].lstrip().startswith(
                _STRUCTURED_COMPACTION_SUMMARY_HEADER
            )
        ):
            packed = _pack_legacy_summary_text(
                deduped[index],
                max_chars=section_budget,
            )
        if packed is not None:
            packed_block = f"{block_prefix}{packed}"
            packed_candidate = _render_summary_context_blocks(
                [*selected, (summary_number, packed_block)],
                omitted_earlier=index,
            )
            if len(packed_candidate) <= _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS:
                selected.append((summary_number, packed_block))
                continue

        omitted_earlier = index + 1
        break
    else:
        omitted_earlier = 0

    return _render_summary_context_blocks(
        selected,
        omitted_earlier=omitted_earlier,
    )


def compaction_context_fingerprint(
    *,
    context_states: Sequence[SessionContextState],
    summaries: Sequence[SessionSummary],
) -> str:
    """Hash the exact active portable context projection for CAS validation."""

    records = build_compaction_context_records(
        context_states=context_states,
        summaries=summaries,
    )
    payload = [
        {
            "text": record.text,
            "compaction_id": record.compaction_id,
            "source": record.source,
            "covered_through_id": record.covered_through_id,
        }
        for record in records
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _valid_structured_summary_state(
    state: SessionContextState,
    *,
    now_ms: int,
) -> bool:
    if not state.valid:
        return False
    if state.expires_at is not None and state.expires_at <= now_ms:
        return False
    return (
        state.provider == "portable"
        and state.state_kind == "structured_summary_v1"
        and state.portable
        and isinstance(state.payload, dict)
    )


def _valid_anthropic_compaction_state(
    state: SessionContextState,
    *,
    now_ms: int,
) -> bool:
    if not state.valid:
        return False
    if state.expires_at is not None and state.expires_at <= now_ms:
        return False
    content = state.payload.get("content") if isinstance(state.payload, dict) else None
    return (
        state.provider == "anthropic"
        and state.state_kind == _ANTHROPIC_COMPACTION_STATE_KIND
        and not state.portable
        and isinstance(content, str)
        and bool(content.strip())
    )


def _replaces_prior_context(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    coverage = payload.get("source_coverage")
    return bool(
        isinstance(coverage, dict)
        and coverage.get("replaces_prior_context") is True
    )


def build_provider_compaction_context(
    *,
    context_states: Sequence[SessionContextState],
    provider_kind: str,
    now_ms: int | None = None,
) -> ProviderCompactionContext:
    """Return provider-native compaction messages for compatible providers."""

    now = _now_ms() if now_ms is None else now_ms
    provider = provider_kind.strip().lower()
    messages: list[Message] = []
    covered_through_ids: set[int] = set()
    if provider != "anthropic":
        return ProviderCompactionContext(messages=messages, covered_through_ids=covered_through_ids)

    valid_states = [
        state for state in context_states if _valid_anthropic_compaction_state(state, now_ms=now)
    ]
    if not valid_states:
        return ProviderCompactionContext(messages=messages, covered_through_ids=covered_through_ids)

    state = latest_context_state(valid_states, provider="anthropic")
    if state is None:
        return ProviderCompactionContext(messages=messages, covered_through_ids=covered_through_ids)
    payload = state.payload
    cache_control = payload.get("cache_control")
    if not isinstance(cache_control, dict):
        cache_control = None
    messages.append(
        Message(
            role="assistant",
            content=[
                ContentBlockCompaction(
                    content=str(payload["content"]),
                    cache_control=cache_control,
                )
            ],
        )
    )
    covered_through_ids.add(state.covered_through_id)

    return ProviderCompactionContext(messages=messages, covered_through_ids=covered_through_ids)


def build_compaction_context_items(
    *,
    context_states: Sequence[SessionContextState],
    summaries: Sequence[SessionSummary],
    legacy_summary_markers: Sequence[str] = (),
    skip_covered_through_ids: set[int] | None = None,
    now_ms: int | None = None,
) -> list[str]:
    """Return stable compaction context blocks with summary_text fallback."""

    return [
        item.text
        for item in build_compaction_context_records(
            context_states=context_states,
            summaries=summaries,
            legacy_summary_markers=legacy_summary_markers,
            skip_covered_through_ids=skip_covered_through_ids,
            now_ms=now_ms,
        )
    ]


def build_compaction_context_records(
    *,
    context_states: Sequence[SessionContextState],
    summaries: Sequence[SessionSummary],
    legacy_summary_markers: Sequence[str] = (),
    skip_covered_through_ids: set[int] | None = None,
    now_ms: int | None = None,
) -> list[CompactionContextItem]:
    """Return stable compaction context blocks with correlation metadata."""

    now = _now_ms() if now_ms is None else now_ms
    items: list[CompactionContextItem] = []
    state_covered_ids: set[int] = set(skip_covered_through_ids or set())

    structured_states = [
        state
        for state in ordered_context_states(context_states)
        if _valid_structured_summary_state(state, now_ms=now)
    ]
    selected_states = latest_context_states_by_covered_through_id(
        structured_states
    )
    validated_states: list[tuple[SessionContextState, StructuredCompactionSummary, str]] = []
    for state in selected_states:
        try:
            structured = StructuredCompactionSummary.model_validate(state.payload)
        except Exception:
            continue
        rendered = render_structured_summary(structured)
        if rendered.strip():
            validated_states.append((state, structured, rendered))

    replacement_index = next(
        (
            index
            for index in range(len(validated_states) - 1, -1, -1)
            if _replaces_prior_context(validated_states[index][0].payload)
        ),
        None,
    )
    replacement_active = replacement_index is not None
    replacement_floor = 0
    if replacement_index is not None:
        validated_states = validated_states[replacement_index:]
        replacement_floor = validated_states[0][0].covered_through_id

    for state, _structured, rendered in validated_states:
        if state.covered_through_id in state_covered_ids:
            continue
        compaction_id = None
        if isinstance(state.payload, dict):
            raw_compaction_id = state.payload.get("compaction_id")
            if isinstance(raw_compaction_id, str) and raw_compaction_id.strip():
                compaction_id = raw_compaction_id.strip()
        items.append(
            CompactionContextItem(
                text=rendered,
                compaction_id=compaction_id,
                source="context_state",
                covered_through_id=state.covered_through_id,
            )
        )
        state_covered_ids.add(state.covered_through_id)

    for summary in summaries:
        if summary.covered_through_id in state_covered_ids:
            continue
        if (
            replacement_active
            and summary.covered_through_id <= replacement_floor
        ):
            continue
        text = summary.summary_text.strip()
        if text:
            items.append(
                CompactionContextItem(
                    text=text,
                    compaction_id=summary.compaction_id,
                    source="summary",
                    covered_through_id=summary.covered_through_id,
                )
            )

    for marker in legacy_summary_markers:
        text = marker.strip()
        if text:
            items.append(
                CompactionContextItem(
                    text=text,
                    compaction_id=None,
                    source="legacy_marker",
                    covered_through_id=0,
                )
            )

    return items
