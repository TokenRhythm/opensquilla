"""Offline checks of the opt-in live replay harness and its evidence assertions."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

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

    secret = "synthetic-cli-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_SECRET", "never-pass-this")
    roots = []

    async def run(root, **kwargs):
        roots.append(root)
        assert "UNRELATED_SECRET" not in os.environ
        assert kwargs["api_key"] == secret
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
