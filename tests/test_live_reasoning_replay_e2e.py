"""Offline checks of the opt-in live replay harness and its evidence assertions."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from collections.abc import AsyncIterator

import brotli
import httpx
import pytest
from PIL import Image

from scripts import live_reasoning_replay_e2e as harness


def _calls() -> list[harness.WireCall]:
    calls = []
    for index in range(5):
        assistant = {
            "role": "assistant",
            "content": f"synthetic-{index}",
            "reasoning_content": f"native-{index}",
        }
        if index in {0, 1, 3}:
            assistant["tool_calls"] = [{"id": f"tool-{index}"}]
        calls.append(
            harness.WireCall(
                request={
                    "tools": [{"type": "function"}],
                    "messages": [copy.deepcopy(call.response) for call in calls],
                },
                response=assistant,
                completed=True,
            )
        )
    return calls


def test_harness_requires_explicit_live_opt_in(capsys):
    assert harness.main([]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "live_opt_in_required"


@pytest.mark.parametrize(("content", "expected"), [
    ("COMPACTION_LABEL=amberfox", "amberfox"),
    ("COMPACTION_LABEL=mapleforest", "mapleforest"),
    ('COMPACTION_LABEL = "cedar-harbor"', "cedar-harbor"),
    ("No label was generated.", None),
])
def test_compaction_fact_does_not_depend_on_exact_label_format(content, expected):
    assert harness._compaction_generated_label(content) == expected


def test_final_non_tool_assistant_is_checked_after_restart():
    calls = _calls()
    checks = harness._assert_wire_replay(calls, 3, "deepseek")
    assert checks["completed_old_round_reasoning_replayed"] is True
    calls[3].request["messages"][2].pop("reasoning_content")
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(calls, 3, "deepseek")


@pytest.mark.parametrize("returned", [False, True])
def test_final_absent_reasoning_is_distinct_from_explicit_empty(returned):
    calls = _calls()
    if returned:
        calls[2].response["reasoning_content"] = ""
    else:
        calls[2].response.pop("reasoning_content")
    for call in calls[3:]:
        call.request["messages"][2] = copy.deepcopy(calls[2].response)
    checks = harness._assert_wire_replay(calls, 3, "deepseek")
    assert checks["completed_old_round_reasoning_returned"] is returned
    assert checks["completed_old_round_reasoning_replayed"] is returned
    if returned:
        calls[3].request["messages"][2].pop("reasoning_content")
        with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
            harness._assert_wire_replay(calls, 3, "deepseek")


def test_wire_parser_retains_explicit_empty_reasoning():
    call = harness.WireCall(request={})
    call.consume(
        b'data: {"choices":[{"delta":{"reasoning_content":""},"finish_reason":"stop"}]}\n\n'
    )
    assert call.response["reasoning_content"] == ""


def test_wire_diagnostics_expose_shapes_and_allowlisted_error_codes_only():
    secret = "synthetic-secret-that-must-not-leak"
    call = harness.WireCall(
        request={
            "messages": [
                {
                    "role": "assistant",
                    "reasoning_content": secret,
                    "content": secret,
                    "reasoning_details": [{"type": "reasoning.encrypted", "data": secret}],
                }
            ]
        }
    )
    call.consume(json.dumps({"error": {"code": secret, "message": secret}}).encode())
    call.response_fields = {"reasoning_content", secret}
    observer = harness.WireObserver("https://example.invalid")
    observer.calls.append(call)
    public = harness._wire_diagnostics(observer)
    assert public["calls"][0]["body_error_code"] == "provider_error"
    assert public["calls"][0]["response_unknown_field_count"] == 1
    assert secret not in json.dumps(public)


@pytest.mark.asyncio
async def test_observer_captures_already_read_error_response_without_public_body():
    async def respond(request):
        return httpx.Response(403, json={"error": {"code": 403, "message": "private-error-body"}})

    observer = harness.WireObserver("https://example.invalid", httpx.MockTransport(respond))
    with observer.observe():
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://example.invalid/chat/completions", json={"messages": []}
            )
    assert response.status_code == 403
    assert observer.calls[0].body_format == "json"
    assert observer.calls[0].body_error_code == "403"
    assert "private-error-body" not in json.dumps(harness._wire_diagnostics(observer))


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "length", None])
async def test_observer_incrementally_decodes_brotli_before_compression_trailer(finish_reason):
    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.session.compaction import call_compaction_provider
    from opensquilla.session.compaction_deployment import (
        CompactionExecutionPlan,
        CompactionExecutionTarget,
    )

    frames = [
        {"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}
        for _ in range(400)
    ]
    frames.append({
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 100},
    })
    raw_chunks = [f"data: {json.dumps(frame)}\n\n".encode() for frame in frames]
    raw_chunks[-1] += b"data: [DONE]\n\n"
    assert sum(map(len, raw_chunks)) > 32_768
    compressor = brotli.Compressor(quality=4)
    compressed_chunks = [compressor.process(chunk) + compressor.flush() for chunk in raw_chunks]
    # A real adapter stops at [DONE], before the HTTP compression trailer.
    # Do not call compressor.finish(): this exercises that partial raw stream.

    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in compressed_chunks:
                if chunk:
                    yield chunk

    async def respond(request):
        return httpx.Response(200, request=request, stream=CompressedStream(), headers={
            "content-type": "text/event-stream", "content-encoding": "br",
        })

    endpoint = "https://example.invalid/v1"
    observer = harness.WireObserver(endpoint, httpx.MockTransport(respond), max_calls=1)
    provider = OpenAIProvider(api_key="synthetic", model="synthetic", base_url=endpoint)
    plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
        provider=provider, provider_id="openai", model="synthetic", context_window_tokens=32_000,
    ),))
    with observer.observe():
        summary = await call_compaction_provider("Synthetic source.", "", plan)

    call, = observer.calls
    assert summary == ("x" * 400 if finish_reason == "stop" else None)
    assert call.response["content"] == "x" * 400
    assert call.encoded_response_bytes == sum(map(len, compressed_chunks))
    assert call.decoded_response_bytes == sum(map(len, raw_chunks))
    assert call.malformed_frames == 0
    assert call.completed is (finish_reason is not None)
    assert call.finish_reason == finish_reason
    assert call.usage == {"prompt_tokens": 20, "completion_tokens": 100}


@pytest.mark.asyncio
async def test_compaction_call_limit_blocks_transport_before_fourth_request():
    sent = 0

    async def respond(request):
        nonlocal sent
        sent += 1
        return httpx.Response(200, json={})

    observer = harness.WireObserver(
        "https://example.invalid", httpx.MockTransport(respond), max_calls=3
    )
    with observer.observe():
        async with httpx.AsyncClient() as client:
            for _ in range(3):
                await client.post("https://example.invalid/chat/completions", json={"messages": []})
            with pytest.raises(harness.ReplayCheckError, match="physical_model_call_limit"):
                await client.post("https://example.invalid/chat/completions", json={"messages": []})
    assert sent == len(observer.calls) == 3


def test_usage_report_keeps_missing_cache_and_reasoning_distinct_from_zero():
    calls = [
        harness.WireCall(request={}, usage={}),
        harness.WireCall(request={}, usage={
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }),
        harness.WireCall(request={}, usage={
            "prompt_cache_hit_tokens": 1024,
            "completion_tokens_details": {"reasoning_tokens": 2000},
        }),
    ]
    missing = harness._usage_report(calls[:1])
    assert missing["cached_input_tokens"] is None
    assert missing["reasoning_tokens"] is None
    report = harness._usage_report(calls)
    assert report["cached_input_tokens_by_call"] == [None, 0, 1024]
    assert report["reasoning_tokens_by_call"] == [None, 0, 2000]
    assert report["cached_input_tokens"] == 1024
    assert report["reasoning_tokens"] == 2000


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "changed_reasoning", "no_tools"])
def test_replay_observer_detects_loss_and_boundary_corruption(mutation):
    calls = _calls()
    if mutation == "missing":
        calls[3].request["messages"].pop(0)
    elif mutation == "duplicate":
        calls[3].request["messages"].append(copy.deepcopy(calls[0].response))
    elif mutation == "changed_reasoning":
        calls[3].request["messages"][0]["reasoning_content"] = "corrupted"
    else:
        calls[3].request["tools"] = []
    with pytest.raises(harness.ReplayCheckError):
        harness._assert_wire_replay(calls, 3, "deepseek")


def test_reasoning_details_preserve_opaque_fields_and_order():
    call = harness.WireCall(request={})
    blocks = [
        {"index": 0, "type": "reasoning.text", "text": "synthetic thought", "id": "r0"},
        {"index": 0, "type": "reasoning.text", "text": " continued", "id": "r0"},
        {
            "index": 1,
            "type": "reasoning.encrypted",
            "data": "opaque-synthetic",
            "signature": "synthetic-signature",
            "format": "vendor-v1",
            "id": "r1",
        },
    ]
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_details": [block]}}]} for block in blocks
    ]
    frames.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    call.consume("".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode())
    assert call.completed
    assert call.raw_reasoning_details == blocks
    expected = [dict(blocks[0], text="synthetic thought continued"), blocks[-1]]
    assert call.response["reasoning_details"] == expected
    calls = _calls()
    calls[0].response["reasoning_details"] = blocks
    for later in calls[1:]:
        later.request["messages"][0]["reasoning_details"] = copy.deepcopy(blocks)
    harness._assert_wire_replay(calls, 3, "openrouter")
    calls[3].request["messages"][0]["reasoning_details"][-1].pop("signature")
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(calls, 3, "openrouter")


def test_logical_details_never_merge_across_encrypted_or_conflicting_identity():
    chunks = [
        {"type": "reasoning.summary", "index": 0, "summary": "first"},
        {"type": "reasoning.summary", "index": 0, "summary": " continued"},
        {"type": "reasoning.encrypted", "index": 0, "data": "opaque-a"},
        {"type": "reasoning.encrypted", "index": 0, "data": "opaque-b"},
        {"type": "reasoning.summary", "index": 0, "summary": "second", "id": "a"},
        {"type": "reasoning.summary", "index": 0, "summary": "third", "id": "b"},
    ]
    assert harness._logical_details(chunks) == [
        {"type": "reasoning.summary", "index": 0, "summary": "first continued"},
        *chunks[2:],
    ]


def test_live_cli_suppresses_provider_output_and_restores_environment(monkeypatch, capsys):
    import os
    import sqlite3
    from pathlib import Path

    from opensquilla.application import approval_queue

    # Exercise cleanup of a queue created by this invocation, regardless of
    # whether an earlier test has already initialized the process singleton.
    monkeypatch.setattr(approval_queue, "_queue", None)

    secret = "synthetic-cli-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_SECRET", "never-pass-this")
    roots = []
    queues = []

    async def run(root, **kwargs):
        roots.append(root)
        assert "UNRELATED_SECRET" not in os.environ
        assert kwargs["api_key"] == secret
        if os.name == "nt":
            assert Path.home().is_relative_to(root)
        queues.append(approval_queue.get_approval_queue())
        print(secret)
        print("opaque-provider-signature")
        return {"ok": True, "provider": "deepseek"}

    monkeypatch.setattr(harness, "run_case", run)
    assert harness.main(["--live"]) == 0
    public = capsys.readouterr().out
    assert secret not in public and "opaque-provider-signature" not in public
    assert json.loads(public)["ok"] is True
    assert os.environ["UNRELATED_SECRET"] == "never-pass-this"
    assert roots and not roots[0].exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        queues[0]._conn.execute("SELECT 1")


def test_live_cli_does_not_close_or_delete_an_existing_external_approval_queue(
    tmp_path, monkeypatch, capsys,
):
    from opensquilla.application import approval_queue

    database = tmp_path / "caller-owned.sqlite"
    queue = approval_queue.ApprovalQueue(db_path=str(database))
    approval_id = queue.request("exec", {"command": "synthetic pending command"})
    monkeypatch.setattr(approval_queue, "_queue", queue)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-cli-credential")
    roots = []

    async def run(root, **kwargs):
        roots.append(root)
        assert not database.resolve().is_relative_to(root.resolve())
        return {"ok": True, "provider": "deepseek"}

    monkeypatch.setattr(harness, "run_case", run)
    try:
        assert harness.main(["--live"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert roots and not roots[0].exists()
        assert approval_queue.get_approval_queue() is queue
        assert database.is_file()
        assert queue.get(approval_id).resolved is False
        assert queue._conn.execute("SELECT COUNT(*) FROM approval_queue").fetchone()[0] == 1
    finally:
        queue.close()


@pytest.mark.parametrize("serve_fails", [False, True])
def test_live_cli_ui_dist_is_served_despite_prior_import_and_restored_on_exit(
    tmp_path, monkeypatch, capsys, serve_fails,
):
    from starlette.applications import Starlette

    from opensquilla.gateway import control_ui
    from opensquilla.gateway.config import GatewayConfig
    from scripts import live_compaction_gateway

    old_dist = tmp_path / "previous-bundle"
    requested_dist = tmp_path / "requested-bundle"
    for directory, name in ((old_dist, "previous"), (requested_dist, "requested")):
        (directory / "assets").mkdir(parents=True)
        (directory / "index.html").write_text(
            f'<script type="module" src="/assets/{name}.js"></script>', encoding="utf-8",
        )
        (directory / "assets" / f"{name}.js").write_text(f"{name}_bundle", encoding="utf-8")
    # Simulate the already-imported production controller caching the old build.
    monkeypatch.setattr(control_ui, "_DIST_DIR", old_dist)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-cli-credential")
    served = []

    async def serve(root, **kwargs):
        config = GatewayConfig(control_ui={"enabled": True, "base_path": "/control"})
        app = Starlette(routes=control_ui.create_control_ui_routes(config))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            index = await client.get("/control/")
            asset = await client.get("/control/static/dist/assets/requested.js")
        assert index.status_code == asset.status_code == 200
        assert "requested.js" in index.text and "previous.js" not in index.text
        assert asset.text == "requested_bundle"
        served.append(True)
        if serve_fails:
            raise RuntimeError("controlled gateway exit")
        return {"ok": True, "lifecycle_status": "stopped", "artifact_scan_status": "passed"}

    monkeypatch.setattr(live_compaction_gateway, "serve_compaction_gateway", serve)
    assert harness.main([
        "--live", "--serve-gateway", "--gateway-root", str(tmp_path / "owned-state"),
        "--ui-dist", str(requested_dist),
    ]) == int(serve_fails)
    capsys.readouterr()
    assert served == [True]
    assert control_ui._DIST_DIR == old_dist
    assert "previous.js" in control_ui._read_vite_assets("/control")[0]


def test_shared_relay_is_installed_after_clean_environment_and_uses_only_placeholder(
    tmp_path, monkeypatch, capsys,
):
    import os

    from scripts import live_tokenrhythm_transport

    placeholder = "live-budget-placeholder-synthetic-replay-acceptance"
    ready = tmp_path / "relay.json"
    ready.write_text(json.dumps({"enabled": True, "mode": "functional",
                                "base_url": "http://127.0.0.1:18791/v1",
                                "client_key": placeholder}))
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-unavailable-ambient-key")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-unrelated")
    installed = []

    def install():
        assert os.environ["TOKENRHYTHM_API_KEY"] == placeholder
        assert os.environ["OPENSQUILLA_LIVE_TRANSPORT"] == "1"
        assert "UNRELATED_SECRET" not in os.environ
        installed.append(True)
        return lambda: installed.append(False)

    async def run(root, **kwargs):
        assert installed == [True]
        assert kwargs["api_key"] == placeholder
        assert kwargs["observer"].max_calls == 60
        return {"ok": True, "provider": "tokenrhythm"}

    monkeypatch.setattr(live_tokenrhythm_transport, "install_from_env", install)
    monkeypatch.setattr(harness, "run_case", run)
    assert harness.main([
        "--live", "--provider", "tokenrhythm", "--model", "deepseek-v4-flash-0731",
        "--relay-ready", str(ready),
    ]) == 0
    assert installed == [True, False]
    assert os.environ["TOKENRHYTHM_API_KEY"] == "synthetic-unavailable-ambient-key"
    assert placeholder not in capsys.readouterr().out


@pytest.mark.parametrize("variant", [
    "basic", "chunked", "tools", "replay_off", "model_switch", "repeated", "truncated",
    "long_reasoning",
])
def test_compaction_cli_passes_explicit_variant_and_model(monkeypatch, capsys, variant):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-cli-credential")
    received = []

    async def run(root, **kwargs):
        received.append(kwargs)
        return {"ok": True, "provider": "openrouter", "scenario": "compaction"}

    monkeypatch.setattr(harness, "run_case", run)
    assert harness.main([
        "--live", "--provider", "openrouter", "--scenario", "compaction",
        "--compaction-variant", variant, "--compaction-next-model", "deepseek/deepseek-v4-pro",
    ]) == 0
    assert len(received) == 1
    assert received[0]["compaction_variant"] == variant
    assert received[0]["compaction_next_model"] == "deepseek/deepseek-v4-pro"
    assert received[0]["api_key"] == "synthetic-cli-credential"
    assert "synthetic-cli-credential" not in capsys.readouterr().out


def test_models_without_returned_native_state_are_not_claimed_as_replay_coverage():
    calls = _calls()
    for call in calls:
        call.response.pop("reasoning_content")
        for message in call.request["messages"]:
            message.pop("reasoning_content")
    checks = harness._assert_wire_replay(calls, 3, "deepseek")
    assert checks["native_wire_coverage"] == "not_returned"
    assert checks["native_state"]["returned"] == 0
    assert checks["native_state_comparisons"] == 0


def test_non_v4_tokenrhythm_native_omission_is_reported_and_can_be_required():
    calls = _calls()
    for call in calls:
        for message in call.request["messages"]:
            message.pop("reasoning_content")
    checks = harness._assert_wire_replay(calls, 3, "tokenrhythm", model="glm-5.2")
    assert checks["native_wire_coverage"] == "partial"
    assert checks["native_state"]["returned"] == 5
    assert checks["native_state"]["wire_present"] == 0
    assert checks["native_state"]["omitted"] == 10
    assert not any(item["documented_projection"] for item in checks["native_state"]["omissions"])
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(
            calls, 3, "tokenrhythm", model="glm-5.2", require_native_replay=True
        )
    # A present, corrupted value must fail even when omission is permitted.
    calls[1].request["messages"][0]["reasoning_content"] = "corrupted"
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(
            calls, 3, "tokenrhythm", model="glm-5.2", require_native_replay=False
        )


def test_v4_tool_state_is_required_even_when_other_native_replay_is_optional():
    calls = _calls()
    for call in calls[3:]:
        call.request["messages"][2]["reasoning_content"] = ""
    checks = harness._assert_wire_replay(
        calls, 3, "tokenrhythm", model="deepseek-v4-pro-0813", require_native_replay=False
    )
    assert checks["native_state"]["omitted"] == 2
    assert all(item["documented_projection"] for item in checks["native_state"]["omissions"])
    calls[1].request["messages"][0].pop("reasoning_content")
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(
            calls, 3, "tokenrhythm", model="deepseek-v4-pro-0813", require_native_replay=False
        )


def _calls_with_tool_results():
    calls = _calls()
    expected = []
    for index, call in enumerate(calls):
        call.request["messages"].extend(copy.deepcopy(expected))
        for tool in call.response.get("tool_calls", []):
            value = {0: 7, 1: 18, 3: 23}[index]
            tool["function"] = {"name": "replay_step", "arguments": json.dumps({"value": value})}
            expected.append(
                {
                    "role": "tool",
                    "tool_call_id": tool["id"],
                    "content": json.dumps({"next_value": value + 11}),
                }
            )
    return calls


@pytest.mark.parametrize("mutation", ["missing", "id", "content", "order"])
def test_tool_result_checks_detect_missing_changed_and_reordered_results(mutation):
    calls = _calls_with_tool_results()
    assert harness._assert_tool_results(calls) == 8
    messages = calls[-1].request["messages"]
    if mutation == "missing":
        messages.pop()
    elif mutation == "id":
        messages[-1]["tool_call_id"] = "different"
    elif mutation == "content":
        messages[-1]["content"] = '{"next_value": 999}'
    else:
        messages[-1], messages[-2] = messages[-2], messages[-1]
    with pytest.raises(harness.ReplayCheckError, match="tool_result_"):
        harness._assert_tool_results(calls)


def test_thinking_diagnostics_only_export_safe_request_controls():
    observer = harness.WireObserver("https://example.invalid")
    observer.calls.append(
        harness.WireCall(
            request={
                "thinking": {"type": "enabled", "budget_tokens": 1000, "private": "secret"},
                "reasoning": {"effort": "high", "exclude": False, "private": "secret"},
                "enable_thinking": True,
                "reasoning_effort": "private-secret",
            }
        )
    )
    report = harness._wire_diagnostics(observer)
    assert report["calls"][0]["request_thinking_controls"] == {
        "thinking": {"type": "enabled", "budget_tokens": 1000},
        "reasoning": {"effort": "high", "exclude": False},
        "enable_thinking": True,
    }
    assert "secret" not in json.dumps(report)


def test_provider_error_reports_fixed_mentions_without_raw_message():
    call = harness.WireCall(request={})
    call.consume(
        json.dumps(
            {"error": {"message": "Missing required reasoning_content: private-secret"}}
        ).encode()
    )
    observer = harness.WireObserver("https://example.invalid")
    observer.calls.append(call)
    report = harness._wire_diagnostics(observer)
    assert report["calls"][0]["error_field_mentions"] == ["reasoning_content"]
    assert report["calls"][0]["error_kind_mentions"] == ["missing", "required"]
    assert "private-secret" not in json.dumps(report)


@pytest.mark.parametrize("raw", [None, "", "raw-native"])
def test_raw_reasoning_observation_and_persistence_distinguish_absent_and_empty(raw):
    delta = {"reasoning": "display-alias", "content": "synthetic-answer"}
    if raw is not None:
        delta["reasoning_content"] = raw
    call = harness.WireCall(request={})
    call.consume(
        f'data: {json.dumps({"choices": [{"delta": delta, "finish_reason": "stop"}]})}\n\n'.encode()
    )
    assert call.native_reasoning_content == raw
    state = {"source": "synthetic-origin", "protocol": "test", "native_reasoning_content": raw}
    saved = [{"messages": [{**call.response, "provider_replay": state}]}]
    assert harness._assert_persisted_replay(saved, [call]) == 1
    state["native_reasoning_content"] = "" if raw is None else None
    with pytest.raises(
        harness.ReplayCheckError, match="persisted_native_reasoning_content_mismatch"
    ):
        harness._assert_persisted_replay(saved, [call])


def test_raw_reasoning_is_independent_of_detail_text_and_display_alias():
    call = harness.WireCall(request={})
    frames = [
        {
            "choices": [{"delta": {
                "reasoning_content": part,
                "reasoning": "unrelated-alias",
                "reasoning_details": [{"type": "reasoning.text", "text": "different-detail"}],
            }}]
        }
        for part in ("raw-first", " raw-second")
    ]
    frames.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    call.consume("".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode())
    assert call.native_reasoning_content == "raw-first raw-second"
    state = {
        "source": "synthetic-origin",
        "protocol": "test",
        "native_reasoning_content": call.native_reasoning_content,
        "reasoning_details": call.response["reasoning_details"],
    }
    saved = [
        {"messages": [{**call.response, "reasoning_content": "display", "provider_replay": state}]}
    ]
    assert harness._assert_persisted_replay(saved, [call]) == 1
    state["native_reasoning_content"] = "different-detaildifferent-detail"
    with pytest.raises(
        harness.ReplayCheckError, match="persisted_native_reasoning_content_mismatch"
    ):
        harness._assert_persisted_replay(saved, [call])


def test_wire_report_distinguishes_omitted_thinking_controls_from_explicit_off():
    observer = harness.WireObserver("https://example.invalid")
    observer.calls = [
        harness.WireCall(request={}),
        harness.WireCall(request={"thinking": {"type": "disabled"}}),
        harness.WireCall(request={"enable_thinking": False}),
    ]
    calls = harness._wire_diagnostics(observer)["calls"]
    assert calls[0]["request_thinking_controls_omitted"] is True
    assert calls[0]["request_thinking_control_fields"] == []
    assert calls[1]["request_thinking_controls_omitted"] is False
    assert calls[1]["request_thinking_control_fields"] == ["thinking"]
    assert calls[2]["request_thinking_controls_omitted"] is False
    assert calls[2]["request_thinking_controls"] == {"enable_thinking": False}


def test_wire_pressure_estimates_image_context_without_tokenizing_base64(monkeypatch):
    from opensquilla import token_estimation

    original_estimate = token_estimation.estimate_tokens_with_source
    measured_texts = []

    def estimate_text(text):
        # A raw-base64 regression fails before sending megabytes to the tokenizer.
        assert len(text) < 1000
        assert "base64," not in text
        measured_texts.append(text)
        return original_estimate(text)

    monkeypatch.setattr(token_estimation, "estimate_tokens_with_source", estimate_text)
    estimates = []
    wire_sizes = []
    for compression in (0, 9):
        image = io.BytesIO()
        Image.new("RGB", (1024, 768), "blue").save(
            image, format="PNG", compress_level=compression,
        )
        encoded = base64.b64encode(image.getvalue()).decode()
        request = {
            "model": "synthetic-vision",
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe the synthetic image."},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + encoded,
                }},
            ]}],
        }
        unchanged = copy.deepcopy(request)
        call = harness.WireCall(request=request, provider="synthetic")
        evidence = harness.wire_pressure_evidence(call)
        wire = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert request == unchanged
        assert evidence["request_chars"] == len(wire)
        assert evidence["request_payload_sha256"] == hashlib.sha256(wire.encode()).hexdigest()
        assert evidence["request_estimate_source"].endswith("_plus_media_reserve")
        assert 1024 < evidence["request_estimated_tokens"] < 1500
        assert evidence["physical_prompt_tokens"] is None
        assert evidence["physical_completion_tokens"] is None
        # Polling evidence must reflect new usage rather than cache an unfinished call.
        call.usage.update(prompt_tokens=5969, completion_tokens=492)
        finished = harness.wire_pressure_evidence(call)
        assert finished["physical_prompt_tokens"] == 5969
        assert finished["physical_completion_tokens"] == 492
        assert finished["request_estimated_tokens"] == evidence["request_estimated_tokens"]
        estimates.append(evidence["request_estimated_tokens"])
        wire_sizes.append(evidence["request_chars"])
    assert estimates[0] == estimates[1]
    assert wire_sizes[0] > 2_000_000
    assert wire_sizes[1] < 10_000
    assert len(measured_texts) == 4


def test_wire_checks_prefer_original_empty_field_over_nonempty_display_alias():
    calls = _calls()
    calls[0].native_reasoning_content = ""
    calls[0].response["reasoning_content"] = "display-only-alias"
    for call in calls[1:]:
        call.request["messages"][0]["reasoning_content"] = ""
    checks = harness._assert_wire_replay(
        calls, 3, "tokenrhythm", model="glm-5.2", require_native_replay=True
    )
    assert checks["native_wire_coverage"] == "complete"
    calls[-1].request["messages"][0]["reasoning_content"] = "display-only-alias"
    with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
        harness._assert_wire_replay(
            calls, 3, "tokenrhythm", model="glm-5.2", require_native_replay=True
        )


def test_top_level_gateway_error_has_safe_diagnostics_without_message_or_trace():
    call = harness.WireCall(request={}, status_code=403)
    call.consume(
        json.dumps(
            {
                "code": "LITELLM_ERROR",
                "message": "当前权限不允许访问该模型 private-response-secret",
                "traceId": "private-trace-identifier",
            }
        ).encode()
    )
    observer = harness.WireObserver("https://example.invalid")
    observer.calls.append(call)
    report = harness._wire_diagnostics(observer)
    assert report["calls"][0]["body_error_code"] == "litellm_error"
    assert report["calls"][0]["error_kind_mentions"] == ["access", "model", "permission"]
    public = json.dumps(report)
    assert "private-response-secret" not in public
    assert "private-trace-identifier" not in public
    assert "traceId" not in public


def test_top_level_success_message_is_not_reported_as_provider_error():
    call = harness.WireCall(request={}, status_code=200)
    call.consume(json.dumps({"code": 0, "message": "required private-success-message"}).encode())
    assert call.body_error_code is None
    assert call.error_kind_mentions == set()


@pytest.mark.parametrize("units,omitted", [(50_000, False), (50_002, True)])
def test_v4_utf16_limit_projection_matches_documented_boundary(units, omitted):
    calls = _calls()
    native = "\U0001f642" * (units // 2)
    calls[0].response["reasoning_content"] = native
    calls[0].native_reasoning_content = native
    for call in calls[1:]:
        call.request["messages"][0]["reasoning_content"] = "" if omitted else native
    checks = harness._assert_wire_replay(
        calls, 3, "tokenrhythm", model="deepseek-v4-pro-0813", require_native_replay=True
    )
    assert checks["native_wire_coverage"] == ("partial" if omitted else "complete")
    assert checks["native_state"]["omitted"] == (4 if omitted else 0)
    assert all(item["documented_projection"] for item in checks["native_state"]["omissions"])
    if not omitted:
        calls[1].request["messages"][0]["reasoning_content"] = ""
        with pytest.raises(harness.ReplayCheckError, match="native_state_wire_mismatch"):
            harness._assert_wire_replay(
                calls, 3, "tokenrhythm", model="deepseek-v4-pro-0813", require_native_replay=True
            )


@pytest.mark.parametrize(
    "scenario,turn,content",
    [
        ("tools", 0, "**REPLAY_FIRST_OK**."),
        ("tools", 1, "Done: replay second ok!"),
        ("chat", 0, "CHAT_FIRST_OK"),
        ("chat", 1, "Remembered: `Amber - 17`. CHAT_SECOND_OK."),
    ],
)
def test_final_answer_validation_accepts_formatted_markers(scenario, turn, content):
    harness._assert_turn_answer(content, scenario=scenario, turn=turn)


@pytest.mark.parametrize("turn", [0, 1])
def test_tool_final_answer_validation_rejects_noncompliance(turn):
    with pytest.raises(harness.ReplayCheckError, match="final_reply_marker_missing"):
        harness._assert_turn_answer(
            "I finished with no requested marker", scenario="tools", turn=turn
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["error", "empty", "length", "timeout"])
async def test_observer_attributes_multiple_physical_providers_and_only_faults_summary(fault):
    transported = []

    async def respond(request):
        transported.append(str(request.url))
        frame = {"choices": [{
            "delta": {"role": "assistant", "content": "synthetic answer"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 91, "completion_tokens": 7}}
        return httpx.Response(200, content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

    observer = harness.WireObserver(
        "https://alpha.invalid/v1", httpx.MockTransport(respond),
        endpoints={"alpha": "https://alpha.invalid/v1", "beta": "https://beta.invalid/v1"},
        summary_fault=lambda: fault,
    )
    with observer.observe():
        async with httpx.AsyncClient() as client:
            await client.post("https://alpha.invalid/v1/chat/completions", json={
                "model": "model-a", "messages": [{"role": "user", "content": "continue"}],
            })
            summary_payload = {
                "model": "model-b", "messages": [
                    {"role": "system", "content": "You are a conversation compactor."},
                    {"role": "user", "content": "synthetic source"},
                ],
            }
            if fault == "timeout":
                with pytest.raises(httpx.ReadTimeout, match="Synthetic summary-only timeout"):
                    await client.post("https://beta.invalid/v1/chat/completions",
                                      json=summary_payload)
            else:
                response = await client.post("https://beta.invalid/v1/chat/completions",
                                             json=summary_payload)
                assert response.status_code == (503 if fault == "error" else 200)
            with pytest.raises(harness.ReplayCheckError, match="unexpected_offline_http_request"):
                await client.post("https://unexpected.invalid/v1/chat/completions", json={})
    assert transported == ["https://alpha.invalid/v1/chat/completions"]
    assert [call.provider for call in observer.calls] == ["alpha", "beta"]
    assert [call.injected_fault for call in observer.calls] == [None, fault]
    public = harness._wire_diagnostics(observer)
    assert public["calls"][0]["physical_prompt_tokens"] == 91
    assert public["calls"][1]["physical_prompt_tokens"] is None
    assert "synthetic source" not in json.dumps(public)
    if fault == "length":
        assert observer.calls[1].finish_reason == "length"


@pytest.mark.parametrize("profile", harness.COMPACTION_TASKS)
def test_critical_fact_check_rejects_each_missing_fact(profile):
    facts = harness.compaction_task_facts(profile)
    assert len(facts) == 12
    for omitted in facts:
        text = "\n".join(value for key, value in facts.items() if key != omitted)
        coverage = harness.compaction_fact_coverage(text, facts)
        assert coverage[omitted] is False
        assert sum(coverage.values()) == 11


@pytest.mark.parametrize("profile", harness.COMPACTION_TASKS)
def test_task_fact_acceptance_requires_correct_field_associations(profile):
    facts = harness.compaction_task_facts(profile)
    assert all(harness.compaction_answer_fact_checks(json.dumps(facts), facts).values())
    swapped = {**facts, "completed": facts["pending"], "pending": facts["completed"]}
    text = json.dumps(swapped, ensure_ascii=False)
    assert all(harness.compaction_fact_coverage(text, facts).values())
    checks = harness.compaction_answer_fact_checks(text, facts)
    assert checks["completed"] is checks["pending"] is False
    assert sum(checks.values()) == 10
    assert not any(harness.compaction_answer_fact_checks(" ".join(facts.values()), facts).values())
    duplicate = json.dumps(facts)[:-1] + ', "pending": "wrong"}'
    assert not any(harness.compaction_answer_fact_checks(duplicate, facts).values())


def test_native_pressure_rejects_artificially_low_trigger():
    with pytest.raises(harness.ReplayCheckError,
                       match="native_pressure_requires_production_threshold"):
        harness.CompactionCaseOptions(context_window_tokens=200000,
                                      native_pressure=True, preflight_ratio=0.1)


def test_summary_fact_diagnostics_ignore_only_code_backticks_and_whitespace():
    facts = {"next": "read tests/test_scan.py"}
    assert harness.compaction_fact_coverage("Next: read `tests/test_scan.py`.", facts)["next"]
    assert harness.compaction_fact_coverage("read\n  tests/test_scan.py", facts)["next"]
    assert not harness.compaction_fact_coverage("delete `tests/test_scan.py`.", facts)["next"]
    assert not harness.compaction_fact_coverage("read `tests/test_scanner.py`.", facts)["next"]


@pytest.mark.parametrize(("known", "configured", "matches"), [
    (True, 200000, True), (True, 32000, False), (False, 200000, False),
])
def test_native_window_requires_known_physical_metadata(monkeypatch, known, configured, matches):
    from opensquilla.provider.model_catalog import DeploymentModelLimits, shared_catalog

    seen = []

    def resolve(model, **kwargs):
        seen.append(kwargs)
        return DeploymentModelLimits(200000, 8192, True, known)

    monkeypatch.setattr(shared_catalog(), "resolve_deployment_limits", resolve)
    result = harness.deployment_window_evidence(
        "deepseek", "synthetic-model", "synthetic-key", "https://example.invalid/v1", configured,
    )
    assert result["configured_window_matches_physical"] is matches
    assert result["physical_window_known"] is known
    assert "logical_max_tokens_override" not in seen[0]
    assert "context_window_tokens" not in seen[0]


@pytest.mark.asyncio
async def test_real_observer_blocks_unobserved_generation_and_only_allows_catalog_reads():
    transported = []

    async def respond(request):
        transported.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    observer = harness.WireObserver("https://allowed.invalid/v1")
    with observer.observe():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await client.get("https://allowed.invalid/v1/models")
            for url in ("https://other.invalid/v1/chat/completions",
                        "https://allowed.invalid/v1/responses",
                        "https://allowed.invalid/v1/chat/completions-extra"):
                with pytest.raises(harness.ReplayCheckError,
                                   match="unobserved_generation_request_blocked"):
                    await client.post(url, json={})
            with pytest.raises(harness.ReplayCheckError, match="http_endpoint_not_allowlisted"):
                await client.get("https://other.invalid/v1/models")
    assert transported == ["https://allowed.invalid/v1/models"]
    report = harness._wire_diagnostics(observer)
    assert report["blocked_unobserved_generation_requests"] == 3
    assert report["transport_kind"] == "real"


def test_pressure_file_evidence_requires_every_original_line(tmp_path):
    from scripts.live_compaction_gateway import pressure_file_evidence

    directory = tmp_path / "compaction-pressure"
    directory.mkdir()
    lines = ["SYNTHETIC_PRESSURE_FILE_0000_BEGIN", "keep this synthetic middle line",
             "SYNTHETIC_PRESSURE_FILE_0000_END"]
    (directory / "0000.txt").write_text("\n".join(lines), encoding="utf-8")
    full = "\n".join(f"{index}: {line}" for index, line in enumerate(lines))
    request = {"messages": [{"role": "tool", "content": full}]}
    assert pressure_file_evidence(request, tmp_path) == [{
        "fixture_id": "0000", "complete": True, "fixture_source": "workspace",
    }]
    request["messages"][0]["content"] = full.replace(lines[1], "[omitted]")
    assert pressure_file_evidence(request, tmp_path) == [{
        "fixture_id": "0000", "complete": False, "fixture_source": "workspace",
    }]


@pytest.mark.asyncio
async def test_gateway_tool_guard_blocks_oracle_reads_and_all_other_tools(tmp_path):
    from opensquilla.tools.types import ToolSpec
    from scripts.live_compaction_gateway import AcceptanceToolRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    forwarded = []

    async def read(**kwargs):
        forwarded.append(kwargs)
        return "synthetic file content"

    registry = AcceptanceToolRegistry((workspace,), allow_read_files=True)
    registry.register(ToolSpec(name="read_file", description="read", parameters={}), read)
    registry.register(ToolSpec(name="exec_command", description="execute", parameters={}), read)
    with pytest.raises(RuntimeError, match="acceptance_tool_or_path_not_allowlisted"):
        await registry.get("read_file").handler(path=str(tmp_path / "report.json"))
    with pytest.raises(RuntimeError, match="acceptance_tool_or_path_not_allowlisted"):
        await registry.get("exec_command").handler(command="synthetic")
    assert registry.blocked_tool_attempts == 2
    assert forwarded == []
    assert await registry.get("read_file").handler(path="fixture.txt") == "synthetic file content"
    assert forwarded == [{"path": "fixture.txt"}]


def test_compaction_cli_passes_explicit_capacity_and_layout(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-credential")
    received = []

    async def run(root, **kwargs):
        received.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(harness, "run_case", run)
    assert harness.main([
        "--live", "--scenario", "compaction", "--layout", "prefix",
        "--context-window", "200000", "--max-output", "8192",
        "--native-pressure", "--task-profile", "research",
    ]) == 0
    assert received[0]["compaction_options"] == harness.CompactionCaseOptions(
        layout="prefix", context_window_tokens=200000, max_output_tokens=8192,
        native_pressure=True, task_profile="research",
    )
    assert "synthetic-credential" not in capsys.readouterr().out


def test_comparison_cli_requires_both_history_capacities(capsys):
    assert harness.main(["--live", "--comparison-history-tokens", "5000"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == (
        "comparison_requires_both_positive_capacities"
    )


def test_comparison_snapshot_cannot_overwrite_an_existing_directory(tmp_path, capsys):
    assert harness.main([
        "--live", "--scenario", "compaction", "--task-profile", "coding",
        "--comparison-snapshot-out", str(tmp_path),
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == (
        "invalid_or_existing_comparison_snapshot"
    )


def test_frozen_comparison_rejects_changed_controls_and_secret_source(tmp_path):
    import sqlite3

    from scripts.live_compaction_comparison import export_snapshot, read_snapshot

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    database = source_dir / "source.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic_state (value TEXT)")
        connection.execute("INSERT INTO synthetic_state VALUES ('public synthetic data')")
    exported = tmp_path / "frozen"
    export_snapshot(exported, database, settings={"model": "synthetic"}, prompt="synthetic query",
                    source_digests={"synthetic-id": "digest"}, label="zqxjvtpr", secrets=())
    assert read_snapshot(exported)["prompt"] == "synthetic query"
    manifest_file = exported / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["prompt"] = "changed query"
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(harness.ReplayCheckError, match="comparison_snapshot_fingerprint_mismatch"):
        read_snapshot(exported)
    with pytest.raises(harness.ReplayCheckError, match="comparison_source_secret_scan_failed"):
        export_snapshot(tmp_path / "refused", database, settings={}, prompt="synthetic",
                        source_digests={}, label="zqxjvtpr", secrets=("public synthetic data",))
    assert not (tmp_path / "refused").exists()


@pytest.mark.asyncio
async def test_rate_limit_diagnostics_keep_only_category_and_numeric_retry_delay():
    async def respond(request):
        return httpx.Response(429, headers={"retry-after": "2.5", "private": "secret-header"},
                              json={"error": {"message": "Rate limit: private diagnostic text"}})

    observer = harness.WireObserver("https://example.invalid", httpx.MockTransport(respond))
    with observer.observe():
        async with httpx.AsyncClient() as client:
            await client.post("https://example.invalid/chat/completions", json={})
    diagnostic = harness._wire_diagnostics(observer)
    assert diagnostic["calls"][0]["retry_after_seconds"] == 2.5
    assert diagnostic["calls"][0]["limit_category"] == "rate_limit"
    assert "private diagnostic text" not in json.dumps(diagnostic)
    assert "secret-header" not in json.dumps(diagnostic)


def test_gateway_execution_overlay_rejects_credentials_and_endpoint_changes():
    from scripts.live_compaction_gateway import public_execution_overlay

    allowed = {"llm_ensemble": {"enabled": True}, "squilla_router": {"enabled": True}}
    assert public_execution_overlay(allowed) == allowed
    cap = {"models": {"deepseek": {"deepseek-v4-pro": {"max_output_tokens": 8192}}}}
    assert public_execution_overlay(cap) == cap
    for overlay in (
        {"llm": {"api_key": "synthetic"}},
        {"llm_ensemble": {"proposers": [{"api_key": "synthetic"}]}},
        {"squilla_router": {"base_url": "https://unexpected.invalid"}},
        {"models": {"deepseek": {"deepseek-v4-pro": {"context_window": 16000}}}},
        {"models": {"deepseek": {"deepseek-v4-pro": {"max_output_tokens": True}}}},
    ):
        with pytest.raises(harness.ReplayCheckError):
            public_execution_overlay(overlay)


def test_gateway_execution_evidence_keeps_roles_quorum_budgets_without_text():
    from scripts.live_compaction_gateway import execution_metadata_evidence

    safe = {
        "baseline_model": "deepseek-v4-pro", "routed_tier": "c2",
        "ensemble_trace": {
            "successful_proposers": 1, "min_successful_proposers": 2,
            "fallback_used": True, "fallback_code": "quorum_unreachable",
            "final_request_role": "fixed_direct",
            "final_request": {"execution": {
                "provider": "deepseek", "model": "deepseek-v4-pro",
                "effective_context_window_tokens": 1000000, "effective_max_tokens": 8192,
            }},
        },
    }
    raw = json.loads(json.dumps(safe))
    raw["ensemble_trace"]["final_request"]["input"] = "private-synthetic-prompt"
    raw["ensemble_trace"]["final_request"]["execution"]["base_url"] = "private-endpoint"
    raw["ensemble_trace"]["candidates"] = [{"content": "private-candidate", "error": "secret"}]
    expected = json.loads(json.dumps(safe))
    expected["ensemble_trace"]["candidates"] = [{}]
    assert execution_metadata_evidence(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_window,overlay,expected_window", [
    (200000, None, 200000),
    (None, {"squilla_router": {"enabled": False}}, 0),
    (32000, {"squilla_router": {"enabled": False}}, 32000),
])
async def test_gateway_adapter_uses_existing_storage_without_seeding_or_exposing_content(
    tmp_path, monkeypatch, configured_window, overlay, expected_window,
):
    from types import SimpleNamespace

    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage
    from scripts import live_compaction_gateway as gateway

    state = tmp_path / "state"
    state.mkdir()
    storage = SessionStorage(str(state / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create("agent:main:synthetic-browser")
    await manager.append_message("agent:main:synthetic-browser", "user", "private-synthetic-body")
    await storage.close()
    captured = []

    async def close():
        return None

    async def start(**kwargs):
        captured.append(kwargs["config"])
        (tmp_path / "stop").touch()
        return SimpleNamespace(close=close)

    monkeypatch.setattr(gateway, "start_gateway_server", start)
    report_path = tmp_path.with_suffix(".report.json")
    result = await gateway.serve_compaction_gateway(
        tmp_path, provider="deepseek", model="synthetic-model",
        endpoint="https://example.invalid/v1", provider_env="DEEPSEEK_API_KEY",
        observer=harness.WireObserver("https://example.invalid/v1"),
        options=harness.CompactionCaseOptions(
            context_window_tokens=configured_window, max_output_tokens=8192,
            layout="prefix", task_profile="research",
        ),
        port=18799, report_path=report_path, secrets=("synthetic-key",), thinking="off",
        execution_overlay=overlay,
    )
    assert result["lifecycle_status"] == "stopped"
    assert result["artifact_scan_status"] == "passed"
    assert result["acceptance_status"] == "requires_browser_assertions"
    assert "ok" not in result
    assert captured[0].llm.context_window_tokens == expected_window
    assert captured[0].llm.max_tokens == 8192
    assert captured[0].compaction.enabled is True
    report = json.loads(report_path.read_text())
    assert report["session_seeded"] is False
    assert report["storage"]["counts"]["transcript_entries"] == 1
    assert report["storage"]["counts"]["compacted_transcript_entries"] == 0
    assert report["storage"]["duplicate_canonical_ids"] == 0
    assert "private-synthetic-body" not in report_path.read_text()
    assert "synthetic-key" not in report_path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("recall_result", [
    "complete", "missing_label", "missing_marker", "missing_summary_label",
    "missing_persisted_label",
])
async def test_chunked_compaction_reports_one_preflight_after_runner_cleanup(
    tmp_path, monkeypatch, recall_result,
):
    label = "qzvxjprt"
    final_answer = {
        "missing_label": "COMPACTION_RECALL_OK",
        "missing_marker": label,
    }.get(recall_result, f"{label} COMPACTION_RECALL_OK")
    summary = "Completed archived background. The synthetic memory exercise is complete."
    if recall_result != "missing_summary_label":
        summary += f" Retain COMPACTION_LABEL={label} for exact recall."
    responses = iter([f"COMPACTION_LABEL={label}", summary, summary, final_answer])

    if recall_result == "missing_persisted_label":
        prepare = harness.SessionManager.prepare_message

        async def drop_label(self, session_key, role, content, **kwargs):
            if role == "assistant" and label in content:
                content = content.replace(label, "removed-fixture-label")
                kwargs["assistant_replay"] = None
            return await prepare(self, session_key, role, content, **kwargs)

        monkeypatch.setattr(harness.SessionManager, "prepare_message", drop_label)

    async def respond(request):
        frame = {
            "choices": [{"index": 0, "delta": {"content": next(responses)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode(),
        )

    observer = harness.WireObserver(
        harness.registry_endpoint("openrouter"), httpx.MockTransport(respond), max_calls=4,
    )
    run = harness._run_compaction_case(
        tmp_path, provider="openrouter", model="deepseek/deepseek-v4-flash",
        api_key="synthetic-offline-key", observer=observer, thinking="off", variant="chunked",
    )
    if recall_result == "complete":
        report = await run
        assert report["ok"] is True
        assert report["preflight_ratio"] == 0.85
        assert report["compaction_attempted_by_turn"] == [False, True]
        assert report["summary_call_indexes"] == [1, 2]
        assert report["coverage"]["observed"]["chunked_summary"] is True
        assert report["coverage"]["observed"]["single_preflight"] is True
    else:
        # Persistence, summary coverage, and answer compliance are separate
        # failure boundaries; no absent fact may pass through an implication.
        code = {
            "missing_summary_label": "summary_generated_fact_missing",
            "missing_persisted_label": "parent_generated_label_not_persisted",
        }.get(recall_result, "compaction_memory_recall_mismatch")
        with pytest.raises(harness.ReplayCheckError, match=f"^{code}$"):
            await run

    assert not observer.engine_error_codes
    recall_checks = observer.replay_checks["memory_recall_checks"]
    if recall_result == "missing_persisted_label":
        assert len(observer.calls) == 1
        assert observer.replay_checks["compaction_attempted_by_turn"] == [False]
        assert recall_checks == []
        return

    assert len(observer.calls) == 4
    assert observer.replay_checks["compaction_attempted_by_turn"] == [False, True]
    assert [event["phase"] for event in observer.replay_checks["compaction_events"]
            if event.get("status") == "started"] == ["preflight"]
    if recall_result == "missing_summary_label":
        assert label in observer.calls[-1].response["content"]
        assert recall_checks == []
    else:
        assert recall_checks == [{
            "labels_in_summary": [True],
            "labels_in_answer": [recall_result != "missing_label"],
            "completion_marker_in_answer": recall_result != "missing_marker",
        }]
    assert label not in json.dumps(recall_checks)
