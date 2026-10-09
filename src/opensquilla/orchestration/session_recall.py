"""Small task-aware recall index for persistent child sessions."""

from __future__ import annotations

import json
import math
import posixpath
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import structlog

from opensquilla.orchestration.models import DelegatedTaskRecord

log = structlog.get_logger(__name__)

DEFAULT_SESSION_RECALL_THRESHOLD = 0.80
_MAX_PATH_ROOTS = 4
_MAX_WEB_ITEMS = 3
_MAX_SUMMARY_CHARS = 1_000
_MAX_RECALL_TEXT_CHARS = 4_000
_PATH_KEYS = frozenset({"path", "file", "file_path", "directory", "dir", "root", "cwd", "workdir"})
_MUTATING_TOOLS = frozenset(
    {
        "apply_patch",
        "create_source",
        "edit_file",
        "edit_source",
        "write_file",
    }
)
_WEB_QUERY_TOOLS = frozenset({"web_discover", "web_search"})
_WEB_URL_TOOLS = frozenset({"http_request", "web_fetch"})
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Delete|Update) File:\s*(.+?)\s*$", re.MULTILINE)
_TEXT_PATH = re.compile(
    r"(?<![\w.-])(?:[A-Za-z]:)?/?(?:[\w.-]+/)+[\w.-]+(?:\.[A-Za-z0-9]+)?"
)
_TOKEN = re.compile(r"[a-z0-9_./-]+|[\u4e00-\u9fff]")
_URL = re.compile(r"https?://\S+")


class _Embedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class _RecallRepository(Protocol):
    async def list_session_recall_tasks(
        self,
        *,
        parent_runtime_session_key: str,
        profile: str,
        limit: int = 100,
    ) -> list[DelegatedTaskRecord]: ...


@dataclass(frozen=True, slots=True)
class SessionRecallMatch:
    session_id: str
    score: float
    task_id: str


def _entry_value(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _tool_name_and_arguments(segment: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(segment, Mapping):
        return "", {}
    function = segment.get("function")
    nested = function if isinstance(function, Mapping) else {}
    name = str(segment.get("name") or nested.get("name") or "").strip()
    arguments = segment.get("input")
    if arguments is None:
        arguments = segment.get("arguments")
    if arguments is None:
        arguments = nested.get("arguments")
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            return name, {}
        arguments = decoded
    return name, dict(arguments) if isinstance(arguments, Mapping) else {}


def _normalize_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if not path or "\n" in path or "\x00" in path:
        return None
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", "/"}:
        return None
    return normalized


def _compact_path_roots(paths: list[tuple[str, bool]]) -> list[str]:
    if not paths:
        return []
    counts = Counter(path for path, _mutating in paths)
    mutating = {path for path, is_mutating in paths if is_mutating}
    grouped: dict[tuple[str, ...], set[str]] = {}
    for path in counts:
        parts = tuple(part for part in path.split("/") if part)
        minimum_depth = 3 if path.startswith("/") or (parts and parts[0].endswith(":")) else 2
        key = parts[:minimum_depth]
        grouped.setdefault(key, set()).add(path)

    collapsed: dict[str, tuple[int, bool]] = {}
    consumed: set[str] = set()
    for key, members in grouped.items():
        if len(members) < 2:
            continue
        parent = posixpath.commonpath(sorted(members))
        parent_depth = len(tuple(part for part in parent.split("/") if part))
        if parent_depth < len(key):
            continue
        collapsed[parent] = (
            sum(counts[path] for path in members),
            any(path in mutating for path in members),
        )
        consumed.update(members)
    for path, count in counts.items():
        if path not in consumed:
            collapsed[path] = (count, path in mutating)
    ordered = sorted(
        collapsed.items(),
        key=lambda item: (-int(item[1][1]), -item[1][0], item[0]),
    )
    return [path for path, _metadata in ordered[:_MAX_PATH_ROOTS]]


def _ordered_unique(values: Sequence[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _slice_current_task(transcript: Sequence[Any], start_message_id: str | None) -> list[Any]:
    if not start_message_id:
        return list(transcript)
    for index, entry in enumerate(transcript):
        if str(_entry_value(entry, "message_id", "")) == start_message_id:
            return list(transcript[index + 1 :])
    return []


def _extract_tool_context(
    transcript: Sequence[Any],
    *,
    start_message_id: str | None,
) -> tuple[list[str], list[str], list[str]]:
    paths: list[tuple[str, bool]] = []
    queries: list[str] = []
    domains: list[str] = []
    for entry in _slice_current_task(transcript, start_message_id):
        tool_calls = _entry_value(entry, "tool_calls", [])
        if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
            continue
        for segment in tool_calls:
            name, arguments = _tool_name_and_arguments(segment)
            if not name:
                continue
            is_mutating = name in _MUTATING_TOOLS
            for key in _PATH_KEYS:
                path = _normalize_path(arguments.get(key))
                if path is not None:
                    paths.append((path, is_mutating))
            if name == "apply_patch":
                patch = arguments.get("patch") or arguments.get("patch_text")
                if isinstance(patch, str):
                    for raw_path in _PATCH_PATH.findall(patch):
                        path = _normalize_path(raw_path)
                        if path is not None:
                            paths.append((path, True))
            if name in _WEB_QUERY_TOOLS:
                query = arguments.get("query")
                if isinstance(query, str):
                    queries.append(query)
            if name in _WEB_URL_TOOLS:
                url = arguments.get("url")
                if isinstance(url, str):
                    domain = urlparse(url).hostname
                    if domain:
                        domains.append(domain.casefold())
    return (
        _compact_path_roots(paths),
        _ordered_unique(queries, limit=_MAX_WEB_ITEMS),
        _ordered_unique(domains, limit=_MAX_WEB_ITEMS),
    )


def _recall_text(
    *,
    task: str,
    acceptance_criteria: str | None,
    summary: str,
    path_roots: Sequence[str],
    web_queries: Sequence[str],
    web_domains: Sequence[str],
) -> str:
    parts = [task.strip(), str(acceptance_criteria or "").strip(), summary.strip()]
    if path_roots:
        parts.append("paths: " + " ".join(path_roots))
    if web_queries:
        parts.append("web queries: " + " | ".join(web_queries))
    if web_domains:
        parts.append("web domains: " + " ".join(web_domains))
    return "\n".join(part for part in parts if part)[:_MAX_RECALL_TEXT_CHARS]


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.casefold()))


def _lexical_score(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return 2.0 * len(left_tokens & right_tokens) / (len(left_tokens) + len(right_tokens))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _paths_in_text(text: str) -> list[str]:
    paths = [_normalize_path(match.group(0)) for match in _TEXT_PATH.finditer(text)]
    return [path for path in paths if path is not None]


def _without_paths(text: str) -> str:
    """Remove file paths, but keep URLs, for path-independent task recall."""

    cleaned: list[str] = []
    for line in text.splitlines():
        if line.startswith("paths: "):
            continue
        parts: list[str] = []
        position = 0
        for match in _URL.finditer(line):
            parts.append(_TEXT_PATH.sub(" ", line[position : match.start()]))
            parts.append(match.group(0))
            position = match.end()
        parts.append(_TEXT_PATH.sub(" ", line[position:]))
        cleaned.append("".join(parts))
    return "\n".join(cleaned)


def _path_related(left: str, right: str) -> bool:
    return left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(
        left.rstrip("/") + "/"
    )


def _structured_score(query_text: str, index: Mapping[str, Any]) -> float:
    query_paths = _paths_in_text(query_text)
    candidate_paths = [
        value for value in index.get("path_roots", []) if isinstance(value, str)
    ]
    if query_paths and candidate_paths and any(
        _path_related(query_path, candidate_path)
        for query_path in query_paths
        for candidate_path in candidate_paths
    ):
        return 1.0
    query_domains = {
        domain.casefold()
        for match in re.finditer(r"https?://[^\s]+", query_text)
        if (domain := urlparse(match.group(0)).hostname)
    }
    candidate_domains = {
        value.casefold()
        for value in index.get("web_domains", [])
        if isinstance(value, str)
    }
    if query_domains & candidate_domains:
        return 1.0
    query_tokens = _tokens(query_text)
    web_queries = [
        value for value in index.get("web_queries", []) if isinstance(value, str)
    ]
    if query_tokens and web_queries:
        return max(
            (len(query_tokens & _tokens(value)) / len(query_tokens) for value in web_queries),
            default=0.0,
        )
    return 0.0


def _stored_index(task: DelegatedTaskRecord) -> dict[str, Any]:
    result = task.result if isinstance(task.result, Mapping) else {}
    raw = result.get("recall_index")
    if isinstance(raw, Mapping):
        return dict(raw)
    summary = str(result.get("summary") or result.get("deliverable") or "")[:_MAX_SUMMARY_CHARS]
    text = _recall_text(
        task=task.description,
        acceptance_criteria=task.acceptance_criteria,
        summary=summary,
        path_roots=(),
        web_queries=(),
        web_domains=(),
    )
    return {
        "text": text,
        "path_roots": [],
        "web_queries": [],
        "web_domains": [],
        "embedding": [],
    }


class SessionRecallEngine:
    """Index task-local context and select a sufficiently similar same-profile session."""

    def __init__(
        self,
        embedder: _Embedder,
        *,
        threshold: float = DEFAULT_SESSION_RECALL_THRESHOLD,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("session recall threshold must be between 0 and 1")
        self.embedder = embedder
        self.threshold = threshold
        self.available = True

    def _disable(self, exc: Exception) -> None:
        if self.available:
            log.warning("subagent_session_recall_disabled", error=str(exc))
        self.available = False

    async def build_task_index(
        self,
        *,
        task: DelegatedTaskRecord,
        result: Mapping[str, Any],
        transcript: Sequence[Any],
        start_message_id: str | None,
    ) -> dict[str, Any]:
        path_roots, web_queries, web_domains = _extract_tool_context(
            transcript,
            start_message_id=start_message_id,
        )
        summary = str(result.get("summary") or result.get("deliverable") or "")[
            :_MAX_SUMMARY_CHARS
        ]
        text = _recall_text(
            task=task.description,
            acceptance_criteria=task.acceptance_criteria,
            summary=summary,
            path_roots=path_roots,
            web_queries=web_queries,
            web_domains=web_domains,
        )
        embedding: list[float] = []
        if self.available:
            try:
                embedding = await self.embedder.embed_query(text)
            except Exception as exc:  # noqa: BLE001 - local recall must not block delegation
                self._disable(exc)
        return {
            "model": self.embedder.model,
            "text": text,
            "result_summary": summary,
            "path_roots": path_roots,
            "web_queries": web_queries,
            "web_domains": web_domains,
            "embedding": embedding,
        }

    async def find_reusable_session(
        self,
        repository: _RecallRepository,
        *,
        parent_runtime_session_key: str,
        profile: str,
        task: str,
        acceptance_criteria: str,
        ignore_paths: bool = False,
    ) -> SessionRecallMatch | None:
        if not self.available:
            return None
        candidates = await repository.list_session_recall_tasks(
            parent_runtime_session_key=parent_runtime_session_key,
            profile=profile,
        )
        if not candidates:
            return None
        query_text = _recall_text(
            task=task,
            acceptance_criteria=acceptance_criteria,
            summary="",
            path_roots=_paths_in_text(task + "\n" + acceptance_criteria),
            web_queries=(),
            web_domains=(),
        )
        if ignore_paths:
            query_text = _without_paths(query_text)
        try:
            query_embedding = await self.embedder.embed_query(query_text)
            indexes = [_stored_index(candidate) for candidate in candidates]
            missing_positions = [
                position
                for position, stored in enumerate(indexes)
                if ignore_paths
                or not isinstance(stored.get("embedding"), list)
                or len(stored["embedding"]) != len(query_embedding)
            ]
            if missing_positions:
                generated = await self.embedder.embed_batch(
                    [
                        _without_paths(str(indexes[position].get("text") or ""))
                        if ignore_paths
                        else str(indexes[position].get("text") or "")
                        for position in missing_positions
                    ]
                )
                for position, embedding in zip(missing_positions, generated, strict=True):
                    indexes[position]["embedding"] = embedding
        except Exception as exc:  # noqa: BLE001 - a failed index must create, not guess
            self._disable(exc)
            return None

        best_by_session: dict[str, SessionRecallMatch] = {}
        for candidate, index in zip(candidates, indexes, strict=True):
            raw_embedding = index.get("embedding")
            vector = (
                [float(value) for value in raw_embedding]
                if isinstance(raw_embedding, list)
                and all(isinstance(value, (int, float)) for value in raw_embedding)
                else []
            )
            candidate_task_text = _recall_text(
                task=candidate.description,
                acceptance_criteria=candidate.acceptance_criteria,
                summary="",
                path_roots=(),
                web_queries=(),
                web_domains=(),
            )
            if ignore_paths:
                candidate_task_text = _without_paths(candidate_task_text)
            lexical = _lexical_score(query_text, candidate_task_text)
            semantic = _cosine(query_embedding, vector)
            score = (
                0.60 * lexical + 0.40 * semantic
                if ignore_paths
                else 0.50 * lexical
                + 0.35 * semantic
                + 0.15 * _structured_score(query_text, index)
            )
            prior = best_by_session.get(candidate.owner_session_id)
            if prior is None or score > prior.score:
                best_by_session[candidate.owner_session_id] = SessionRecallMatch(
                    session_id=candidate.owner_session_id,
                    score=score,
                    task_id=candidate.task_id,
                )
        if not best_by_session:
            return None
        best = max(best_by_session.values(), key=lambda match: match.score)
        return best if best.score >= self.threshold else None


__all__ = [
    "DEFAULT_SESSION_RECALL_THRESHOLD",
    "SessionRecallEngine",
    "SessionRecallMatch",
]
