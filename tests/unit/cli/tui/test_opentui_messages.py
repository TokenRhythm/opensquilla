from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui.messages import (
    HOST_TO_PYTHON_TYPES,
    PYTHON_TO_HOST_TYPES,
    ApprovalDismiss,
    AttachmentClear,
    AttachmentRemove,
    AttachmentState,
    AttachmentUpdate,
    CompletionArgumentChoice,
    CompletionCandidate,
    CompletionContext,
    HistoryMessage,
    HistoryReplace,
    HostApprovalResponse,
    HostInputCancel,
    HostInputEof,
    HostInputSubmit,
    HostProtocolUnknown,
    HostReady,
    HostResize,
    HostToPythonMessageError,
    RouterPluginState,
    host_message_from_json,
    python_message_to_json,
)

_PACKAGE_SRC = Path(__file__).resolve().parents[4] / "src/opensquilla/cli/tui/opentui/package/src"


def _production_host_sources() -> list[Path]:
    return [
        path for path in sorted(_PACKAGE_SRC.glob("*.mjs")) if not path.name.endswith(".test.mjs")
    ]


def test_python_message_to_json_serializes_router_update() -> None:
    payload = python_message_to_json(
        "router.update",
        RouterPluginState(
            model="gpt-5.5",
            route="T3 | 91%",
            saving="42% | -$0.021",
            context="128k | 37%",
            style="normal",
        ),
    )

    assert payload.endswith("\n")
    assert '"type":"router.update"' in payload
    assert '"model":"gpt-5.5"' in payload
    assert '"route":"T3 | 91%"' in payload


def test_python_message_to_json_serializes_completion_context() -> None:
    payload = python_message_to_json(
        "completion.context",
        CompletionContext(
            catalog=(
                CompletionCandidate(
                    label="/compact",
                    description="Compact chat context.",
                    insert_text="/compact",
                    category="control",
                    usage="/compact",
                    aliases=("/cmp",),
                    busy_policy="abort_and_run",
                    presentation="notice",
                ),
                CompletionCandidate(
                    label="/strategy",
                    description="Choose the model strategy.",
                    insert_text="/strategy ",
                    category="control",
                    usage="/strategy [direct|router|ensemble|status]",
                    argument_choices=(
                        CompletionArgumentChoice(
                            value="router",
                            description="Use Squilla Router.",
                        ),
                    ),
                ),
                CompletionCandidate(
                    label="/code-review",
                    description="Run a comprehensive code review",
                    insert_text="use the code-review skill: ",
                    category="skill",
                ),
            ),
            files=("src/main.py",),
            filters_sensitive_paths=True,
        ),
    )

    assert payload.endswith("\n")
    assert '"type":"completion.context"' in payload
    assert '"label":"/compact"' in payload
    assert '"category":"control"' in payload
    assert '"aliases":["/cmp"]' in payload
    assert '"busy_policy":"abort_and_run"' in payload
    assert '"argument_choices":[{"value":"router"' in payload
    assert '"insert_text":"use the code-review skill: "' in payload
    assert '"files":["src/main.py"]' in payload
    assert '"filters_sensitive_paths":true' in payload


def test_python_message_to_json_serializes_atomic_history_replace() -> None:
    payload = python_message_to_json(
        "history.replace",
        HistoryReplace(
            session_key="agent:main:session-1",
            history_scope="latest_window",
            has_more=True,
            loaded_count=1,
            canonical_available=True,
            messages=(
                HistoryMessage(
                    id="message-1",
                    role="user",
                    text="你好",
                    attachments=({"name": "brief.pdf"},),
                ),
            ),
        ),
    )

    assert payload.endswith("\n")
    assert '"type":"history.replace"' in payload
    assert '"history_scope":"latest_window"' in payload
    assert '"id":"message-1"' in payload
    assert '"attachments":[{"name":"brief.pdf"}]' in payload


def test_python_message_to_json_serializes_attachment_state_messages() -> None:
    added = python_message_to_json(
        "attachment.add",
        AttachmentState(
            id="attachment-1",
            kind="file",
            label="brief.pdf",
            status="reading",
        ),
    )
    updated = python_message_to_json(
        "attachment.update",
        AttachmentUpdate(
            id="attachment-1",
            status="failed",
            message="check the file and retry /file",
        ),
    )

    assert '"type":"attachment.add"' in added
    assert '"label":"brief.pdf"' in added
    assert '"status":"reading"' in added
    assert '"type":"attachment.update"' in updated
    assert '"status":"failed"' in updated
    assert '"type":"attachment.remove"' in python_message_to_json(
        "attachment.remove", AttachmentRemove(id="attachment-1")
    )
    assert '"status":"ready"' in python_message_to_json(
        "attachment.clear", AttachmentClear(status="ready")
    )


def test_host_message_from_json_parses_ready_and_submit() -> None:
    assert host_message_from_json('{"type":"ready"}') == HostReady()
    assert host_message_from_json(
        '{"type":"input.submit","text":"中文 prompt"}'
    ) == HostInputSubmit(text="中文 prompt")
    assert host_message_from_json(
        '{"type":"input.submit","text":"adjust","intent":"steer"}'
    ) == HostInputSubmit(text="adjust", intent="steer")
    assert host_message_from_json(
        '{"type":"input.submit","text":"/router on","intent":"control"}'
    ) == HostInputSubmit(text="/router on", intent="control")


def test_host_message_from_json_parses_versioned_ready_metadata() -> None:
    parsed = host_message_from_json(
        '{"type":"ready","protocol":1,"productVersion":"0.5.0",'
        '"hostVersion":"0.5.0","platform":"darwin","arch":"arm64",'
        '"buildId":"release","screenMode":"alternate-screen",'
        '"capabilities":["jsonl","authenticated"]}'
    )
    assert parsed == HostReady(
        protocol=1,
        product_version="0.5.0",
        host_version="0.5.0",
        platform="darwin",
        arch="arm64",
        build_id="release",
        screen_mode="alternate-screen",
        capabilities=("jsonl", "authenticated"),
    )


def test_host_message_from_json_parses_control_messages() -> None:
    assert host_message_from_json('{"type":"input.cancel"}') == HostInputCancel()
    assert host_message_from_json('{"type":"input.eof"}') == HostInputEof()
    assert host_message_from_json('{"type":"resize","width":120,"height":36}') == (
        HostResize(width=120, height=36)
    )


def test_host_message_rejects_malformed_control_payloads() -> None:
    with pytest.raises(HostToPythonMessageError, match="input.submit.text"):
        host_message_from_json('{"type":"input.submit"}')

    with pytest.raises(HostToPythonMessageError, match="resize.width"):
        host_message_from_json('{"type":"resize","height":36}')

    with pytest.raises(HostToPythonMessageError, match="theme.selected.name"):
        host_message_from_json('{"type":"theme.selected"}')

    with pytest.raises(HostToPythonMessageError, match="Unknown OpenTUI host"):
        host_message_from_json('{"type":"surprise"}')

    with pytest.raises(HostToPythonMessageError, match="input.submit.intent"):
        host_message_from_json('{"type":"input.submit","text":"hello","intent":"interrupt"}')


def test_host_message_from_json_parses_protocol_unknown() -> None:
    parsed = host_message_from_json('{"type":"protocol.unknown","messageType":"tool.call"}')
    assert parsed == HostProtocolUnknown(message_type="tool.call")

    with pytest.raises(HostToPythonMessageError, match="protocol.unknown.messageType"):
        host_message_from_json('{"type":"protocol.unknown"}')


def test_host_message_from_json_parses_approval_response() -> None:
    parsed = host_message_from_json(
        '{"type":"approval.response","id":"appr-1","approved":true,"choice":"allow_once"}'
    )
    assert parsed == HostApprovalResponse(id="appr-1", approved=True, choice="allow_once")

    denied = host_message_from_json(
        '{"type":"approval.response","id":"appr-2","approved":false,"choice":null}'
    )
    assert denied == HostApprovalResponse(id="appr-2", approved=False, choice=None)


def test_host_message_rejects_malformed_approval_response() -> None:
    with pytest.raises(HostToPythonMessageError, match="approval.response.id"):
        host_message_from_json('{"type":"approval.response","approved":true}')

    with pytest.raises(HostToPythonMessageError, match="approval.response.approved"):
        host_message_from_json('{"type":"approval.response","id":"appr-1"}')

    with pytest.raises(HostToPythonMessageError, match="approval.response.approved"):
        host_message_from_json('{"type":"approval.response","id":"appr-1","approved":"yes"}')


def test_python_message_to_json_serializes_approval_request() -> None:
    payload = python_message_to_json(
        "approval.request",
        {
            "id": "appr-1",
            "tool": "shell",
            "summary": "touch demo.txt",
            "choices": ["allow_once", "deny"],
        },
    )
    assert payload.endswith("\n")
    assert '"type":"approval.request"' in payload
    assert '"id":"appr-1"' in payload
    assert '"choices":["allow_once","deny"]' in payload


def test_python_message_to_json_serializes_approval_dismiss() -> None:
    payload = python_message_to_json("approval.dismiss", ApprovalDismiss(id="appr-1"))
    assert payload.endswith("\n")
    assert '"type":"approval.dismiss"' in payload
    assert '"id":"appr-1"' in payload


def test_python_message_to_json_serializes_structured_blocks() -> None:
    from opensquilla.cli.tui.opentui.messages import (
        ModelText,
        PromptEcho,
        TurnBegin,
        TurnEnd,
        TurnStatusState,
    )

    assert '"type":"turn.begin"' in python_message_to_json("turn.begin", TurnBegin(id="t1"))
    assert '"type":"prompt.echo"' in python_message_to_json(
        "prompt.echo", PromptEcho(text="帮我分析架构")
    )
    assert '"id":"t1"' in python_message_to_json("turn.end", TurnEnd(id="t1", cancelled=False))
    model = python_message_to_json("model.text", ModelText(text="先扫描结构"))
    assert '"type":"model.text"' in model and '"text":"先扫描结构"' in model
    status = python_message_to_json(
        "turn.status", TurnStatusState(phase="tool", label="read_file", active=True)
    )
    assert '"phase":"tool"' in status and '"active":true' in status


def test_block_messages_serialize_with_kind_and_fields() -> None:
    from opensquilla.cli.tui.opentui.messages import (
        BlockAppend,
        BlockBegin,
        BlockEnd,
        BlockUpdate,
        python_message_to_json,
    )

    begin = python_message_to_json(
        "block.begin",
        BlockBegin(id="b1", kind="tool", meta={"name": "ls", "args": "src"}),
    )
    assert '"type":"block.begin"' in begin
    assert '"kind":"tool"' in begin
    assert '"name":"ls"' in begin
    append = python_message_to_json("block.append", BlockAppend(id="b1", delta="line"))
    assert '"delta":"line"' in append
    update = python_message_to_json("block.update", BlockUpdate(id="b1", patch={"status": "ok"}))
    assert '"status":"ok"' in update
    end = python_message_to_json("block.end", BlockEnd(id="b1"))
    assert '"type":"block.end"' in end


def test_python_to_host_registry_matches_js_dispatcher_cases() -> None:
    """Every outbound type must have a dispatcher case in the host, and vice
    versa — a type without a handler is one release skew away from a live
    protocol error, and a handler without a sender is dead contract."""
    text = (_PACKAGE_SRC / "ipc.mjs").read_text(encoding="utf-8")
    dispatcher = text.split("createDispatcher", 1)[1]
    cases = set(re.findall(r'case "([a-z][a-z0-9._-]*)":', dispatcher))
    assert cases, "could not parse dispatcher cases from ipc.mjs"
    assert set(PYTHON_TO_HOST_TYPES) == cases


def test_host_emitted_types_are_all_parseable_by_python() -> None:
    """Every type literal the host can emit must have a Python parse branch
    (extra Python branches are harmless forward tolerance)."""
    emitted: set[str] = set()
    for path in _production_host_sources():
        emitted.update(
            re.findall(
                r'\{\s*type:\s*"([a-z][a-z0-9._-]*)"',
                path.read_text(encoding="utf-8"),
            )
        )
    assert emitted, "could not parse emitter literals from the host sources"
    # Authentication is consumed by the transport before product messages
    # reach host_message_from_json().
    unparseable = emitted - set(HOST_TO_PYTHON_TYPES) - {"auth"}
    assert not unparseable, f"host emits types Python cannot parse: {sorted(unparseable)}"


def test_host_to_python_registry_round_trips_canonical_frames() -> None:
    samples = {
        "ready": '{"type":"ready"}',
        "input.submit": '{"type":"input.submit","text":"hi"}',
        "input.cancel": '{"type":"input.cancel"}',
        "input.eof": '{"type":"input.eof"}',
        "resize": '{"type":"resize","width":80,"height":24}',
        "completion.request": (
            '{"type":"completion.request","kind":"file","query":"a","request_id":1}'
        ),
        "error": '{"type":"error","message":"boom"}',
        "protocol.unknown": '{"type":"protocol.unknown","messageType":"mystery.type"}',
        "approval.response": (
            '{"type":"approval.response","id":"appr-1","approved":true,"choice":"allow_once"}'
        ),
        "theme.selected": '{"type":"theme.selected","name":"nord"}',
    }
    assert set(samples) == set(HOST_TO_PYTHON_TYPES)
    for wire_type, raw in samples.items():
        assert isinstance(host_message_from_json(raw), HOST_TO_PYTHON_TYPES[wire_type])


def test_js_snake_case_payload_reads_match_python_dataclass_fields() -> None:
    """Payloads travel via dataclasses.asdict and JS reads fall back with `??`,
    so a renamed Python field silently blanks the host UI. Pin every snake_case
    payload read in the host against the registered dataclass fields."""
    reads: set[str] = set()
    for path in _production_host_sources():
        reads.update(
            re.findall(
                r"\b[a-zA-Z_$][a-zA-Z0-9_$]*\.([a-z][a-z0-9]*_[a-z0-9_]+)\b",
                path.read_text(encoding="utf-8"),
            )
        )
    assert reads, "could not find any snake_case payload reads in the host sources"
    allowed: set[str] = set()
    for payload_cls in PYTHON_TO_HOST_TYPES.values():
        if payload_cls is not None:
            allowed.update(field.name for field in dataclasses.fields(payload_cls))
    # Nested completion candidates plus the ad-hoc mapping payloads
    # (completion.response and theme.set carry dicts, not dataclasses).
    allowed.update(field.name for field in dataclasses.fields(CompletionCandidate))
    allowed.update(field.name for field in dataclasses.fields(HistoryMessage))
    # History usage/tool-call rows are canonical Gateway dictionaries nested
    # inside the typed HistoryMessage envelope.
    allowed.update(
        {
            "request_id",
            "kind",
            "items",
            "name",
            "cost_usd",
            "billed_cost",
            # Host-local semantic scroll anchors intentionally use the public
            # scroll.anchor.v1 wire spelling even though they are not fields on
            # a Python->host payload dataclass.
            "block_id",
            "elapsed_ms",
            "ensemble_trace",
            "execution_status",
            "fallback_reason",
            "fallback_used",
            "input_tokens",
            "is_error",
            "model_usage_breakdown",
            "output_tokens",
            "proposer_index",
            "reasoning_tokens",
            "row_within_block",
            "sample_index",
            "tool_name",
            "tool_use_id",
            "total_candidates",
        }
    )
    stale = reads - allowed
    assert not stale, f"host reads payload fields Python does not send: {sorted(stale)}"
