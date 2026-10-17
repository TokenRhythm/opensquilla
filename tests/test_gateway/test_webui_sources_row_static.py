import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES_ROW = ROOT / "opensquilla-webui/src/components/chat/SourcesRow.vue"
CHAT_TYPES = ROOT / "opensquilla-webui/src/types/chat.ts"
PART_TYPES = ROOT / "opensquilla-webui/src/types/parts.ts"
RENDERED_MESSAGES = ROOT / "opensquilla-webui/src/composables/chat/useChatRenderedMessages.ts"
TEXT_PART = ROOT / "opensquilla-webui/src/components/chat/parts/TextPart.vue"
CITATIONS = ROOT / "opensquilla-webui/src/utils/chat/citations.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_sources_row_prefers_tool_call_sources_before_result_json() -> None:
    source = _read(SOURCES_ROW)

    call_sources_index = source.index("const directSources")
    record_sources_index = source.index("const recordSources")
    results_index = source.index("const results =")

    assert "extractSources(call.sources, out, seen)" in source
    assert "extractSources(record.sources, out, seen)" in source
    assert call_sources_index < record_sources_index < results_index


def test_chat_tool_call_type_and_history_normalizer_preserve_sources() -> None:
    types_source = _read(CHAT_TYPES)
    rendered_source = _read(RENDERED_MESSAGES)

    assert "sources?: unknown" in types_source
    raw_tool_call_payload_source = types_source[
        types_source.index("export interface RawToolCallPayload") :
    ]
    assert "sources?: unknown" in raw_tool_call_payload_source
    assert "sources: item.sources" in rendered_source
    assert "if (tc.sources !== undefined) item.sources = tc.sources" in rendered_source


def test_source_part_type_preserves_search_trust_metadata() -> None:
    source = _read(PART_TYPES)
    source_part = source[source.index("export interface SourcePart") :]

    assert "canonicalUrl?: string" in source_part
    assert "provider?: string" in source_part
    assert "fetched?: boolean" in source_part
    assert "fetchStatus?: string" in source_part


def test_sources_row_renders_search_trust_states() -> None:
    source = _read(SOURCES_ROW)

    assert "sourceTrustLabel(source)" in source
    assert "Verified" in source
    assert "Search result" in source
    assert "Fetch failed" in source
    assert "sources-row__status--verified" in source
    assert "sources-row__status--failed" in source


def test_text_part_reports_missing_citations_from_decorator() -> None:
    text_part = _read(TEXT_PART)
    citations = _read(CITATIONS)

    assert "t('chat.missingCitationSources', { citations: missingCitationLabel })" in text_part
    english = json.loads(_read(ROOT / "opensquilla-webui/src/locales/en.json"))
    assert english["chat"]["missingCitationSources"] == (
        "Some citations do not map to available sources: {citations}"
    )
    assert "onMissingCitations" in text_part
    assert "missingCitationIds" in text_part
    assert "onMissingCitations?: (sourceIds: number[]) => void" in citations
    assert "missing.add(n)" in citations
