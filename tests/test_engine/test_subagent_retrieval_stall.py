from __future__ import annotations

import json

from opensquilla.engine import ToolResult
from opensquilla.engine.subagent_retrieval_stall import SubagentRetrievalStallGuard


def _result(tool: str, payload: dict[str, object], *, is_error: bool = False) -> ToolResult:
    return ToolResult(
        tool_use_id=f"{tool}-id",
        tool_name=tool,
        content=json.dumps(payload),
        is_error=is_error,
    )


def _search(*urls: str) -> ToolResult:
    return _result(
        "web_search",
        {
            "ok": True,
            "results": [{"url": url, "title": url} for url in urls],
        },
    )


def _fetch(url: str) -> ToolResult:
    return _result(
        "web_fetch",
        {"url": url, "final_url": url, "status": 200, "text": "evidence"},
    )


def test_duplicate_search_rounds_do_not_inject_model_guidance() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=4)

    assert guard.observe([_search("https://example.com/a")]).terminal is None
    assert guard.observe([_search("https://example.com/a")]).terminal is None
    observation = guard.observe([_search("https://example.com/a")])

    assert observation.terminal is None
    assert not hasattr(observation, "notice")


def test_new_source_resets_stalled_rounds() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=4)

    guard.observe([_search("https://example.com/a")])
    guard.observe([_search("https://example.com/a")])
    assert guard.consecutive_stalled_attempts == 1

    observation = guard.observe([_search("https://example.com/b")])

    assert observation.terminal is None
    assert guard.consecutive_stalled_attempts == 0


def test_first_fetch_of_a_source_is_progress_but_repeated_fetch_is_not() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=3)
    url = "https://example.com/story?utm_source=test"

    assert guard.observe([_search(url)]).terminal is None
    assert guard.observe([_fetch(url)]).terminal is None
    observation = guard.observe([_fetch("https://example.com/story")])

    assert observation.terminal is None


def test_failed_retrieval_is_left_to_generic_tool_failure_fallback() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=2)

    observation = guard.observe(
        [_result("web_search", {"ok": False, "results": []}, is_error=True)]
    )

    assert observation.terminal is None
    assert guard.consecutive_stalled_attempts == 0


def test_web_fetch_payload_error_is_not_counted_as_progress() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=2)

    failure = _result(
        "web_fetch",
        {
            "url": "https://example.com/story",
            "final_url": "https://example.com/story",
            "status": 503,
            "text": "",
            "error": "rate-limited or blocked upstream",
        },
    )
    guard.observe([failure])
    observation = guard.observe([failure])

    assert observation.terminal is None
    assert guard.consecutive_stalled_attempts == 0


def test_continued_stall_returns_standard_failure_with_found_sources() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=4)
    source = "https://example.com/a"
    guard.observe([_search(source)])

    for _ in range(3):
        assert guard.observe([_search(source)]).terminal is None
    terminal = guard.observe([_search(source)]).terminal

    assert terminal is not None
    payload = json.loads(terminal)
    assert payload == {
        "error": "Four consecutive retrieval attempts added no new sources."
    }


def test_parallel_duplicate_attempts_count_individually() -> None:
    guard = SubagentRetrievalStallGuard(hard_attempts=4)
    source = "https://example.com/a"
    guard.observe([_search(source)])

    observation = guard.observe([_search(source) for _ in range(4)])

    assert observation.terminal is not None
    assert json.loads(observation.terminal) == {
        "error": "Four consecutive retrieval attempts added no new sources."
    }
