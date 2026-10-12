from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import snowballstemmer

from opensquilla.provider.types import ToolDefinition, ToolInputSchema
from opensquilla.tools import search
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext


def _uncached_tokenize(value: str) -> tuple[str, ...]:
    words = search._WORD_RE.findall(search.normalize_search_text(value))
    words = [word for word in words if word not in search._ENGLISH_STOP_WORDS]
    return tuple(snowballstemmer.stemmer("english").stemWords(words))


@pytest.mark.parametrize("value", [
    "create_calendar_event connected connections connecting connected",
    "Café Gießen 中文 🍕 123.45 it's children's the for users",
    " ".join(["repeat"] * 20),
    "a" * 129 + " " + "connecting" * 100,
])
def test_cached_tokenization_matches_complete_uncached_tokens(value: str) -> None:
    expected = _uncached_tokenize(value)
    search._cached_stem_word.cache_clear()
    assert search.tokenize_for_bm25(value) == expected
    assert search.tokenize_for_bm25(value) == expected


@pytest.mark.parametrize("guest", [False, True])
def test_real_authorized_catalog_has_identical_cold_and_warm_search_scores(guest: bool) -> None:
    registry = get_default_registry()
    context = ToolContext(is_owner=not guest, guest_safe=guest, caller_kind=CallerKind.WEB)
    definitions = registry.to_tool_definitions(context)
    queries = ("read_file", "edit files", "search directory", "connected servers")

    with patch.object(search, "tokenize_for_bm25", _uncached_tokenize):
        original = search.ToolSearchIndex.from_definitions(definitions)
        expected = [[hit.to_payload() for hit in original.search(query)] for query in queries]

    search._cached_stem_word.cache_clear()
    for _ in range(2):
        index = search.ToolSearchIndex.from_definitions(definitions)
        actual = [[hit.to_payload() for hit in index.search(query)] for query in queries]
        assert actual == expected
        assert {hit["name"] for hits in actual for hit in hits} <= {
            definition.name for definition in definitions
        }


def test_warm_cache_does_not_retain_revoked_tools_or_outdated_schema() -> None:
    definition = ToolDefinition(
        name="router_control", description="Adjust routing",
        input_schema=ToolInputSchema(properties={"target": {"enum": ["tier:c1"]}}),
    )
    revoked = ToolDefinition(
        name="owner_only", description="Owner command", input_schema=ToolInputSchema(),
    )
    first = search.ToolSearchIndex.from_definitions([definition, revoked])
    definition.input_schema.properties["target"]["enum"] = ["tier:c2"]
    second = search.ToolSearchIndex.from_definitions([definition])

    assert second.search("owner_only") == []
    assert first.search("owner_only")[0].name == "owner_only"
    assert first.search("router_control")[0].input_schema["properties"]["target"]["enum"] == [
        "tier:c1",
    ]
    assert second.search("router_control")[0].input_schema["properties"]["target"]["enum"] == [
        "tier:c2",
    ]


def test_vocabulary_cache_is_bounded_and_does_not_retain_long_words() -> None:
    search._cached_stem_word.cache_clear()
    search.tokenize_for_bm25(" ".join(str(number) for number in range(4200)))
    before = search._cached_stem_word.cache_info()
    assert before.currsize == before.maxsize == 4096
    long_word = "connecting" * 100
    assert search.tokenize_for_bm25(long_word) == _uncached_tokenize(long_word)
    assert search._cached_stem_word.cache_info() == before


def test_concurrent_cold_cache_misses_match_independent_stemmers() -> None:
    values = [f"connected connections running studies {number}" for number in range(40)]
    expected = [_uncached_tokenize(value) for value in values]
    search._cached_stem_word.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as executor:
        actual = list(executor.map(search.tokenize_for_bm25, values))
    assert actual == expected
