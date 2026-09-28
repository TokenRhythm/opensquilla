"""Offline guardrails for the live user-corpus runner; no network or credentials."""

import hashlib
import json
import threading

import httpx
import pytest

from scripts.live_tokenrhythm_budget import BudgetRejectedError, FunctionalRequestLog
from scripts.live_workspace_file_cases import (
    CaseRelay,
    final_file_candidates,
    pending_input,
    read_cases,
    review_delivery,
    tool_errors,
    workspace_access,
)
from scripts.live_workspace_file_transport import install_from_env


def test_case_loader_preserves_full_task_and_excludes_observations(tmp_path):
    source = tmp_path / "cases.md"
    prompt = "Must create an actual app.\n- Preserve this detail.\n\nDeliverables: all files."
    source.write_text(
        "## Case 5：Image\nObservation is not a task.\n```text\n"
        + prompt
        + "\n```\n## Case B5：Research\n```text\n必须使用 `deep-research` skill\n```",
        encoding="utf-8",
    )
    cases = read_cases(source)
    assert [case.id for case in cases] == ["5", "B5"]
    assert cases[0].prompt == prompt
    assert cases[1].required_skill == "deep-research"
    assert cases[1].timeout_seconds > cases[0].timeout_seconds


def test_case_loader_rejects_ambiguous_task_blocks(tmp_path):
    source = tmp_path / "cases.md"
    source.write_text("## Case 5：Image\n```text\na\n```\n```text\nb\n```", encoding="utf-8")
    with pytest.raises(ValueError, match="one task block"):
        read_cases(source)


def test_per_case_relay_rejects_unbudgeted_requests_before_network(tmp_path):
    sent = []
    log = FunctionalRequestLog(tmp_path / "requests.db", enabled=True)
    log.select_phase(variant="new", case_id="5")

    def upstream(request):
        sent.append(request)
        return httpx.Response(200, content=b'{"choices":[]}')

    relay = CaseRelay(
        model="test-model",
        max_calls=1,
        api_key="fake-secret",
        request_log=log,
        transport=httpx.MockTransport(upstream),
    )
    try:
        valid = json.dumps({"model": "test-model", "max_tokens": 100, "messages": []}).encode()
        invalid = json.dumps({"model": "other-model", "max_tokens": 100}).encode()
        with pytest.raises(BudgetRejectedError):
            with relay.forward(invalid):
                pass
        assert not sent
        with relay.forward(valid) as response:
            assert response.status_code == 200
            list(response.chunks)
        with pytest.raises(BudgetRejectedError):
            with relay.forward(valid):
                pass
        assert len(sent) == 1
    finally:
        relay.close()


def test_workspace_access_requires_real_matching_bytes_and_session_header():
    data = b"<svg />"
    calls = []

    def respond(request):
        assert request.headers["x-opensquilla-session-key"] == "current-session"
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "requestedPath": "image.svg",
                            "path": "image.svg",
                            "kind": "download",
                            "contentUrl": (
                                "/api/v1/workspace-files/content?path=image.svg&binding=opaque"
                            ),
                        }
                    ]
                },
            )
        return httpx.Response(200, content=data)

    with httpx.Client(
        base_url="http://127.0.0.1:1111", transport=httpx.MockTransport(respond)
    ) as client:
        result = workspace_access(
            client,
            "current-session",
            [{"path": "image.svg", "sha256": hashlib.sha256(data).hexdigest()}],
        )
    assert result["files"][0]["matches_workspace"] is True
    assert len(calls) == 2


def test_workspace_access_does_not_follow_external_content_url():
    def respond(request):
        assert request.method == "POST"
        return httpx.Response(
            200, json={"files": [{"path": "x", "contentUrl": "https://example.com/secret"}]}
        )

    with httpx.Client(
        base_url="http://127.0.0.1:1111", transport=httpx.MockTransport(respond)
    ) as client:
        result = workspace_access(client, "session", [{"path": "x"}])
    assert result["files"][0]["error"] == "unexpected_content_url"


def test_inner_tool_error_is_preserved_despite_outer_success():
    result = tool_errors(
        {"status": "succeeded", "messages": [{"content": '{"isError":true,"message":"failed"}'}]}
    )
    assert result == [{"isError": True, "message": "failed"}]


def test_intermediate_code_is_not_an_infographic_delivery():
    result = review_delivery(
        "5",
        [{"path": "render.py"}],
        {"files": [{"path": "render.py", "matches_workspace": True}]},
        [],
    )
    assert result["file_types_status"] == "missing_required_output"
    assert result["delivery_status"] == "missing_required_delivery"


def test_final_candidates_exclude_code_trees_and_http_urls():
    assert final_file_candidates(
        "```\nfolder/\n├ file.js\n```\nSee `file.js` and [a](folder/a.png). "
        "[web](https://example.org/x.svg)"
    ) == ["file.js", "folder/a.png"]


def test_provider_relay_preserves_public_search_custom_transport(monkeypatch):
    placeholder = "live-budget-placeholder-" + "1" * 32
    monkeypatch.setenv("OPENSQUILLA_LIVE_TRANSPORT", "1")
    monkeypatch.setenv("OPENSQUILLA_LIVE_RELAY_URL", "http://127.0.0.1:1111/v1")
    monkeypatch.setenv("OPENSQUILLA_LIVE_RELAY_CLIENT_KEY", placeholder)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    restore = install_from_env()
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            client.post("https://html.duckduckgo.com/html", data={"q": "public research"})
            client.post(
                "https://tokenrhythm.studio/v1/chat/completions",
                headers={"Authorization": "Bearer " + placeholder},
                json={},
            )
            with pytest.raises(httpx.TransportError, match="placeholder_outside_relay"):
                client.get(
                    "https://example.org", headers={"Authorization": "Bearer " + placeholder}
                )
    finally:
        restore()
    assert calls[0].url.host == "html.duckduckgo.com"
    assert calls[1].url.host == "127.0.0.1"


def test_pending_input_requires_paused_result_for_current_turn(tmp_path):
    folder = tmp_path / "turn-calls"
    folder.mkdir()
    event = {
        "turn_id": "turn",
        "kind": "tool_response",
        "payload": {
            "name": "request_user_input",
            "result": json.dumps({"status": "input_required", "paused": True, "questions": []}),
        },
    }
    (folder / "trace.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert pending_input(tmp_path, "other-turn") is None
    assert pending_input(tmp_path, "turn")["tool"] == "request_user_input"


def test_next_request_waits_for_previous_response_cleanup(tmp_path):
    log = FunctionalRequestLog(tmp_path / "requests.db", enabled=True)
    log.select_phase(variant="new", case_id="race")
    first_stream_open = threading.Event()
    finish_first_stream = threading.Event()
    second_started = threading.Event()
    failures = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: [DONE]\n\n"
            first_stream_open.set()
            assert finish_first_stream.wait(3)

    calls = []

    def respond(request):
        calls.append(request)
        return (
            httpx.Response(200, stream=Stream())
            if len(calls) == 1
            else httpx.Response(200, content=b"{}")
        )

    relay = CaseRelay(
        model="test-model",
        max_calls=2,
        api_key="fake",
        request_log=log,
        transport=httpx.MockTransport(respond),
    )
    body = json.dumps({"model": "test-model", "max_tokens": 1}).encode()

    def request(second=False):
        if second:
            second_started.set()
        try:
            with relay.forward(body) as response:
                list(response.chunks)
        except Exception as exc:
            failures.append(exc)

    first = threading.Thread(target=request)
    second = threading.Thread(target=request, args=(True,))
    try:
        first.start()
        assert first_stream_open.wait(3)
        second.start()
        assert second_started.wait(3)
        finish_first_stream.set()
        first.join(3)
        second.join(3)
        assert not failures
        assert len(calls) == 2
    finally:
        finish_first_stream.set()
        first.join(3)
        if second.ident:
            second.join(3)
        relay.close()


def test_manifest_download_does_not_deliver_extension_dependencies():
    files = [
        {
            "path": "ext/manifest.json",
            "sha256": "manifest-hash",
            "parsed": {"manifest_version": 3, "required_files": ["content.js", "style.css"]},
        },
        {"path": "ext/content.js", "sha256": "js-hash"},
        {"path": "ext/style.css", "sha256": "css-hash"},
    ]
    result = review_delivery(
        "3",
        files,
        {"files": []},
        [{"name": "manifest.json", "sha256": "manifest-hash", "matches_artifact": True}],
    )
    assert result["delivery_status"] == "incomplete_extension_delivery"
    assert result["unavailable_extension_files"] == ["ext/content.js", "ext/style.css"]
