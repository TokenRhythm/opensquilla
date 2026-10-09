from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.orchestration.models import DelegatedTaskRecord, TaskOutcome
from opensquilla.orchestration.session_recall import SessionRecallEngine, _without_paths

pytestmark = pytest.mark.asyncio


class FakeEmbedder:
    model = "BAAI/bge-small-zh-v1.5"

    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0] if "redis timeout" in text.casefold() else [0.0, 1.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_query(text) for text in texts]


class FakeRepository:
    def __init__(self, tasks: list[DelegatedTaskRecord]) -> None:
        self.tasks = tasks
        self.calls: list[tuple[str, str]] = []

    async def list_session_recall_tasks(
        self,
        *,
        parent_runtime_session_key: str,
        profile: str,
        limit: int = 100,
    ) -> list[DelegatedTaskRecord]:
        del limit
        self.calls.append((parent_runtime_session_key, profile))
        return list(self.tasks)


async def test_build_index_uses_only_current_task_transcript_and_compacts_paths() -> None:
    engine = SessionRecallEngine(FakeEmbedder())
    task = DelegatedTaskRecord(
        task_id="task-1",
        run_id="run-1",
        task_key="fix-redis-timeout",
        owner_session_id="worker-1",
        description="Fix the Redis timeout handling",
        acceptance_criteria="Patch the implementation and report the focused test",
    )
    transcript = [
        SimpleNamespace(
            message_id="old-message",
            role="assistant",
            tool_calls=[
                {"type": "tool_use", "name": "read_file", "input": {"path": "old/a.py"}}
            ],
        ),
        SimpleNamespace(message_id="task-message", role="user", tool_calls=None),
        SimpleNamespace(
            message_id="tool-message",
            role="assistant",
            tool_calls=[
                {
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"path": "src/redis/client.py"},
                },
                {
                    "type": "tool_use",
                    "name": "apply_patch",
                    "input": {
                        "patch": "*** Update File: src/redis/timeout.py\n@@\n-old\n+new"
                    },
                },
                {
                    "type": "tool_use",
                    "name": "web_search",
                    "input": {"query": "redis timeout upstream behavior"},
                },
                {
                    "type": "tool_use",
                    "name": "web_fetch",
                    "input": {"url": "https://redis.io/docs/latest/develop/"},
                },
            ],
        ),
    ]

    index = await engine.build_task_index(
        task=task,
        result={"summary": "Redis timeout fixed in the shared client path."},
        transcript=transcript,
        start_message_id="task-message",
    )

    assert index["model"] == "BAAI/bge-small-zh-v1.5"
    assert index["path_roots"] == ["src/redis"]
    assert index["web_queries"] == ["redis timeout upstream behavior"]
    assert index["web_domains"] == ["redis.io"]
    assert index["embedding"] == [1.0, 0.0]
    assert index["result_summary"] == "Redis timeout fixed in the shared client path."
    assert "old/a.py" not in index["text"]


async def test_path_roots_do_not_collapse_unrelated_top_level_subtrees() -> None:
    engine = SessionRecallEngine(FakeEmbedder())
    task = DelegatedTaskRecord(
        task_id="task-1",
        run_id="run-1",
        task_key="hooks-change",
        owner_session_id="worker-1",
        description="Update the hooks implementation and browser test",
    )

    index = await engine.build_task_index(
        task=task,
        result={"summary": "Updated both requested files."},
        transcript=[
            SimpleNamespace(message_id="start", role="user", tool_calls=None),
            SimpleNamespace(
                message_id="tools",
                role="assistant",
                tool_calls=[
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "input": {"path": "hooks/src/index.js"},
                    },
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "input": {"path": "hooks/test/browser/useId.test.js"},
                    },
                ],
            ),
        ],
        start_message_id="start",
    )

    assert index["path_roots"] == [
        "hooks/src/index.js",
        "hooks/test/browser/useId.test.js",
    ]


async def test_hybrid_recall_filters_by_fixed_profile_and_reuses_above_threshold() -> None:
    prior = DelegatedTaskRecord(
        task_id="task-old",
        run_id="run-old",
        task_key="redis-timeout-analysis",
        owner_session_id="explorer-1",
        description="Investigate the Redis timeout",
        acceptance_criteria="Locate the timeout source path",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "summary": "The timeout is implemented under src/redis.",
            "recall_index": {
                "text": "Investigate the Redis timeout under src/redis",
                "path_roots": ["src/redis"],
                "web_queries": [],
                "web_domains": [],
                "embedding": [1.0, 0.0],
            },
        },
    )
    repository = FakeRepository([prior])
    engine = SessionRecallEngine(FakeEmbedder())

    match = await engine.find_reusable_session(
        repository,
        parent_runtime_session_key="agent:main:root",
        profile="explorer",
        task="Investigate redis timeout in src/redis/client.py",
        acceptance_criteria="Return the timeout source path",
    )

    assert match is not None
    assert match.session_id == "explorer-1"
    assert match.score >= 0.80
    assert repository.calls == [("agent:main:root", "explorer")]


async def test_hybrid_recall_creates_new_session_below_threshold() -> None:
    prior = DelegatedTaskRecord(
        task_id="task-old",
        run_id="run-old",
        task_key="documentation-search",
        owner_session_id="researcher-1",
        description="Search release documentation",
        acceptance_criteria="Return the release date",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "recall_index": {
                "text": "Search release documentation",
                "path_roots": [],
                "web_queries": ["release documentation"],
                "web_domains": ["example.com"],
                "embedding": [0.0, 1.0],
            }
        },
    )
    engine = SessionRecallEngine(FakeEmbedder(), threshold=0.72)

    match = await engine.find_reusable_session(
        FakeRepository([prior]),
        parent_runtime_session_key="agent:main:root",
        profile="researcher",
        task="Investigate redis timeout in src/redis/client.py",
        acceptance_criteria="Return the timeout source path",
    )

    assert match is None


async def test_default_recall_rejects_generic_similarity_between_unrelated_tasks() -> None:
    class GenericEmbedder(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            del text
            return [1.0, 0.0]

    prior = DelegatedTaskRecord(
        task_id="stock-task",
        run_id="earlier-run",
        task_key="stock-report",
        owner_session_id="stock-child",
        description="Research Apple stock price and save a report",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "recall_index": {
                "text": (
                    "Research Apple stock price. Save the result to a file "
                    "and summarize the source."
                ),
                "path_roots": [],
                "web_queries": [],
                "web_domains": [],
                "embedding": [1.0, 0.0],
            }
        },
    )

    match = await SessionRecallEngine(GenericEmbedder()).find_reusable_session(
        FakeRepository([prior]),
        parent_runtime_session_key="agent:main:root",
        profile="worker",
        task=(
            "Create weather.py for San Francisco. Save the result to a file "
            "and summarize the output."
        ),
        acceptance_criteria="",
    )

    assert match is None


async def test_keyword_match_outweighs_embedding_only_match() -> None:
    class FixedEmbedder(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            del text
            return [1.0, 0.0]

    query = "Summarize the quarterly revenue report"
    exact_words = DelegatedTaskRecord(
        task_id="exact-words",
        run_id="old-run",
        task_key="revenue-report",
        owner_session_id="lexical-child",
        description=query,
        outcome=TaskOutcome.SUCCEEDED,
        result={"recall_index": {"text": query, "embedding": [0.0, 1.0]}},
    )
    embedding_only = DelegatedTaskRecord(
        task_id="embedding-only",
        run_id="old-run",
        task_key="unrelated",
        owner_session_id="semantic-child",
        description="Deploy a weather service",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "recall_index": {"text": "Deploy a weather service", "embedding": [1.0, 0.0]}
        },
    )

    match = await SessionRecallEngine(FixedEmbedder(), threshold=0).find_reusable_session(
        FakeRepository([embedding_only, exact_words]),
        parent_runtime_session_key="agent:main:root",
        profile="inherit",
        task=query,
        acceptance_criteria="",
    )

    assert match is not None
    assert match.session_id == "lexical-child"


async def test_single_task_recall_ignores_paths_in_all_signals() -> None:
    class RecordingEmbedder(FakeEmbedder):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def embed_query(self, text: str) -> list[float]:
            self.texts.append(text)
            return [1.0, 0.0]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    prior = DelegatedTaskRecord(
        task_id="weather-task",
        run_id="old-run",
        task_key="weather",
        owner_session_id="weather-child",
        description="Create a Python weather script at /aef-run/subject_workspace/weather.py",
        acceptance_criteria="The weather script fetches San Francisco weather",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "recall_index": {
                "text": (
                    "Create a Python weather script at /aef-run/subject_workspace/weather.py\n"
                    "The weather script fetches San Francisco weather\n"
                    "Completed /aef-run/subject_workspace/weather.py\n"
                    "paths: /aef-run/subject_workspace"
                ),
                "path_roots": ["/aef-run/subject_workspace"],
                "embedding": [1.0, 0.0],
            }
        },
    )
    embedder = RecordingEmbedder()
    match = await SessionRecallEngine(embedder, threshold=0.72).find_reusable_session(
        FakeRepository([prior]),
        parent_runtime_session_key="agent:main:root",
        profile="inherit",
        task=(
            "Read the document at /aef-run/subject_workspace/summary_source.txt and "
            "write a concise summary to /aef-run/subject_workspace/summary_output.txt"
        ),
        acceptance_criteria="The summary file has three paragraphs",
        ignore_paths=True,
    )

    assert match is None
    assert embedder.texts
    assert all("/aef-run/subject_workspace" not in text for text in embedder.texts)


async def test_single_task_reuses_same_request_even_when_file_paths_change() -> None:
    class FixedEmbedder(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            del text
            return [1.0, 0.0]

    prior = DelegatedTaskRecord(
        task_id="report-task",
        run_id="old-run",
        task_key="report",
        owner_session_id="report-child",
        description="Summarize the quarterly report at /workspace/old/report.txt",
        acceptance_criteria="Return a concise summary of the report",
        outcome=TaskOutcome.SUCCEEDED,
        result={
            "recall_index": {
                "text": "Summarize the quarterly report at /workspace/old/report.txt",
                "embedding": [1.0, 0.0],
            }
        },
    )
    match = await SessionRecallEngine(FixedEmbedder(), threshold=0.72).find_reusable_session(
        FakeRepository([prior]),
        parent_runtime_session_key="agent:main:root",
        profile="inherit",
        task="Summarize the quarterly report at /workspace/new/report.txt",
        acceptance_criteria="Return a concise summary of the report",
        ignore_paths=True,
    )
    assert match is not None
    assert match.session_id == "report-child"


async def test_path_removal_keeps_web_urls_and_removes_stored_path_roots() -> None:
    text = (
        "Fetch https://wttr.in/San%20Francisco?format=j1 and save "
        "to /aef-run/subject_workspace/weather.py\n"
        "paths: /aef-run/subject_workspace"
    )
    cleaned = _without_paths(text)
    assert "https://wttr.in/San%20Francisco?format=j1" in cleaned
    assert "/aef-run/subject_workspace" not in cleaned
    assert "paths:" not in cleaned


async def test_embedding_failure_disables_guessing_without_blocking_delegation() -> None:
    class BrokenEmbedder(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            del text
            raise RuntimeError("model unavailable")

    prior = DelegatedTaskRecord(
        task_id="task-old",
        run_id="run-old",
        task_key="same-task",
        owner_session_id="worker-1",
        description="Same task",
        outcome=TaskOutcome.SUCCEEDED,
    )
    engine = SessionRecallEngine(BrokenEmbedder())

    match = await engine.find_reusable_session(
        FakeRepository([prior]),
        parent_runtime_session_key="agent:main:root",
        profile="worker",
        task="Same task",
        acceptance_criteria="Return the result",
    )

    assert match is None
    assert engine.available is False
