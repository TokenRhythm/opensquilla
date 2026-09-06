"""Progressive tool discovery backed by a small in-memory BM25 index."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import snowballstemmer  # type: ignore[import-untyped]
from anyascii import anyascii

BUILTIN_TOOL_NAMESPACE = "builtin"
MCP_NAMESPACE_PREFIX = "mcp__"

# This intentionally stays local and deterministic.  Tool search is not natural
# language generation: removing a compact set of high-frequency English words
# improves matching without making the index dependent on a downloaded corpus.
_ENGLISH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "with",
        "you",
        "your",
    }
)
_WORD_RE = re.compile(r"\d+(?:\.\d+)*|[a-z]+(?:'[a-z]+)?")
_STEMMER = snowballstemmer.stemmer("english")


def normalize_search_text(value: str) -> str:
    """Transliterate Unicode to lowercase ASCII text for lexical matching."""

    return anyascii(value).lower()


def tokenize_for_bm25(value: str) -> tuple[str, ...]:
    """Normalize, split, stop-word filter, and stem a query or document."""

    words = [word for word in _WORD_RE.findall(normalize_search_text(value))]
    words = [word for word in words if word not in _ENGLISH_STOP_WORDS]
    return tuple(_STEMMER.stemWords(words))


def tool_namespace(tool_name: str) -> str:
    """Return the explicit namespace for a registered tool name."""

    if tool_name.startswith(MCP_NAMESPACE_PREFIX):
        # Accept legacy dotted names in persisted/test integrations, while new
        # provider-facing MCP names use mcp__server__tool.
        if "." in tool_name:
            return tool_name.split(".", 1)[0]
        namespace, separator, component = tool_name.rpartition("__")
        if separator and component and namespace.startswith(MCP_NAMESPACE_PREFIX):
            return namespace
    return BUILTIN_TOOL_NAMESPACE


def searchable_tool_text(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any] | None = None,
) -> str:
    """Build index text, retaining both raw and underscore-split tool names."""

    expanded_name = name.replace("_", " ").replace(".", " ").replace("-", " ")
    schema_text = json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True)
    return f"{name} {expanded_name} {description} {schema_text}"


@dataclass(frozen=True)
class ToolSearchDocument:
    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    tokens: tuple[str, ...]
    exact_name_forms: frozenset[str]


@dataclass(frozen=True)
class ToolSearchHit:
    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    score: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "description": self.description,
            "input_schema": self.input_schema,
            "score": round(self.score, 6),
        }


class ToolSearchIndex:
    """Immutable per-turn BM25 view of tools authorized for that turn."""

    def __init__(self, documents: Iterable[ToolSearchDocument]) -> None:
        self._documents = tuple(documents)

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted({doc.namespace for doc in self._documents}))

    @classmethod
    def from_definitions(cls, definitions: Iterable[Any]) -> ToolSearchIndex:
        documents: list[ToolSearchDocument] = []
        for definition in definitions:
            schema = getattr(definition, "input_schema", None)
            model_dump = getattr(schema, "model_dump", None)
            if callable(model_dump):
                schema_payload = model_dump(mode="json", exclude_none=True)
            elif isinstance(schema, Mapping):
                schema_payload = dict(schema)
            else:
                schema_payload = {}
            name = str(getattr(definition, "name", ""))
            description = str(getattr(definition, "description", ""))
            documents.append(
                ToolSearchDocument(
                    name=name,
                    namespace=tool_namespace(name),
                    description=description,
                    input_schema=schema_payload,
                    tokens=tokenize_for_bm25(
                        searchable_tool_text(
                            name=name,
                            description=description,
                            parameters=schema_payload,
                        )
                    ),
                    exact_name_forms=frozenset(
                        {
                            normalize_search_text(name).strip(),
                            normalize_search_text(
                                name.replace("_", " ").replace(".", " ").replace("-", " ")
                            ).strip(),
                        }
                    ),
                )
            )
        return cls(documents)

    def search(
        self,
        query: str,
        *,
        namespace: str = BUILTIN_TOOL_NAMESPACE,
        limit: int = 5,
    ) -> list[ToolSearchHit]:
        query_tokens = tokenize_for_bm25(query)
        documents = [doc for doc in self._documents if doc.namespace == namespace]
        if not documents:
            return []
        normalized_query = normalize_search_text(query).strip()
        if not query_tokens:
            exact_documents = [doc for doc in documents if normalized_query in doc.exact_name_forms]
            return [
                ToolSearchHit(
                    name=doc.name,
                    namespace=doc.namespace,
                    description=doc.description,
                    input_schema=doc.input_schema,
                    score=1_000.0,
                )
                for doc in exact_documents[: max(1, min(int(limit), 20))]
            ]

        term_frequencies = [Counter(doc.tokens) for doc in documents]
        document_frequency = Counter(
            token for frequencies in term_frequencies for token in frequencies
        )
        average_length = sum(len(doc.tokens) for doc in documents) / len(documents)
        average_length = average_length or 1.0
        document_count = len(documents)
        k1 = 1.5
        b = 0.75
        scores: list[tuple[float, ToolSearchDocument]] = []
        for doc, frequencies in zip(documents, term_frequencies, strict=True):
            score = 0.0
            length_ratio = len(doc.tokens) / average_length
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                matching_documents = document_frequency[token]
                inverse_document_frequency = math.log(
                    1.0 + (document_count - matching_documents + 0.5) / (matching_documents + 0.5)
                )
                score += inverse_document_frequency * (
                    frequency * (k1 + 1.0) / (frequency + k1 * (1.0 - b + b * length_ratio))
                )
            # Exact callable-name lookup is a critical recovery path. BM25 can
            # otherwise rank a shorter, more common tool above the requested
            # identifier due to length normalization.
            if normalized_query in doc.exact_name_forms:
                score += 1_000.0
            if score > 0:
                scores.append((score, doc))
        scores.sort(key=lambda item: (-item[0], item[1].name))
        return [
            ToolSearchHit(
                name=doc.name,
                namespace=doc.namespace,
                description=doc.description,
                input_schema=doc.input_schema,
                score=score,
            )
            for score, doc in scores[: max(1, min(int(limit), 20))]
        ]
