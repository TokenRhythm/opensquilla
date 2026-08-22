from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


def _load_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "live_artifact_prompt_annotations_e2e.py"
    )
    spec = importlib.util.spec_from_file_location(
        "live_artifact_prompt_annotations_e2e",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


e2e = _load_module()


class _DeterministicArtifactProvider:
    """Local OpenAI-compatible fixture for one real Direct mutation turn."""

    def __init__(self) -> None:
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.requests: list[dict[str, Any]] = []

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length))
                owner.requests.append(payload)
                chunks = owner._response_chunks(payload)
                body = b"".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @staticmethod
    def _tool_chunk(model: str, *, call_id: str, name: str, arguments: object) -> list[dict]:
        return _DeterministicArtifactProvider._tool_chunks(
            model,
            [(call_id, name, arguments)],
        )

    @staticmethod
    def _tool_chunks(
        model: str,
        calls: list[tuple[str, str, object]],
    ) -> list[dict]:
        if not calls:
            raise AssertionError("deterministic provider requires at least one tool call")
        return [
            {
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(
                                            arguments,
                                            separators=(",", ":"),
                                        ),
                                    },
                                }
                                for index, (call_id, name, arguments) in enumerate(calls)
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]

    @staticmethod
    def _text_chunk(model: str, text: str) -> list[dict]:
        return [
            {
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]

    @staticmethod
    def _json_tool_content(content: object) -> dict[str, Any]:
        text = str(content or "")
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise AssertionError("artifact tool result did not contain JSON")
        payload = json.loads(text[start : end + 1])
        assert isinstance(payload, dict)
        return payload

    def _response_chunks(self, payload: dict[str, Any]) -> list[dict]:
        tools = payload.get("tools") or []
        names = {
            item.get("function", {}).get("name")
            for item in tools
            if isinstance(item, dict)
        }
        serialized = json.dumps(payload, sort_keys=True)
        assert "start_offset" not in serialized
        assert "end_offset" not in serialized
        assert "## Workspace Files (injected)" not in serialized
        assert "<available_skills>" not in serialized
        assert "Working directory:" not in serialized
        assert "workspace:AGENTS.md" not in serialized
        messages = payload.get("messages") or []
        model = str(payload.get("model") or "synthetic")
        if not names:
            # Full B5 proposers are deliberately tool-less.  Their inert text
            # is consumed only by the verified Aggregator request below.
            return self._text_chunk(
                model,
                "Read the accepted annotations, use their opaque ranges, "
                "and apply one atomic edit.",
            )
        assert names == e2e._ALLOWED_TOOLS
        tool_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        if not tool_messages:
            return self._tool_chunk(
                model,
                call_id="call_document_inspect",
                name="document_inspect",
                arguments={},
            )

        tool_payloads = [
            self._json_tool_content(message.get("content"))
            for message in tool_messages
        ]
        annotation_payload = next(
            (item for item in tool_payloads if isinstance(item.get("annotations"), list)),
            None,
        )
        assert annotation_payload is not None
        latest_payload = tool_payloads[-1]
        if latest_payload.get("status") == "candidate_staged":
            return self._tool_chunk(
                model,
                call_id="call_document_browser_inspect",
                name="document_browser_inspect",
                arguments={"scope": "document", "maxNodes": 100},
            )
        source_patch_requested = any(
            e2e._SOURCE_PATCH_INSERTION in str(annotation.get("instruction") or "")
            for annotation in annotation_payload["annotations"]
        )
        if latest_payload.get("status") == "verification_passed":
            if source_patch_requested and not any(
                item.get("imageAttached") is True for item in tool_payloads
            ):
                return self._tool_chunk(
                    model,
                    call_id="call_document_browser_screenshot",
                    name="document_browser_screenshot",
                    arguments={},
                )
            return self._tool_chunk(
                model,
                call_id="call_document_finish",
                name="document_finish",
                arguments={
                    "decision": "commit",
                    "expectedCandidateSha256": latest_payload["candidateSha256"],
                    "verificationToken": latest_payload["verificationToken"],
                },
            )
        if latest_payload.get("status") in {"applied", "discarded"}:
            return self._text_chunk(model, "The document update is complete.")
        if source_patch_requested:
            candidate_payloads = [
                item for item in tool_payloads if item.get("status") == "candidate_staged"
            ]
            if candidate_payloads and any(item.get("status") == "ok" for item in tool_payloads):
                repaired = (
                    '<span id="reset-status" role="status" aria-live="polite">Ready</span>'
                )
                return self._tool_chunk(
                    model,
                    call_id="call_document_patch_repair",
                    name="document_patch",
                    arguments={
                        "expectedSha256": candidate_payloads[-1]["candidateSha256"],
                        "edits": [
                            {
                                "expectedText": e2e._SOURCE_PATCH_INSERTION,
                                "replacement": repaired,
                            }
                        ],
                    },
                )
            source_payload = next(
                (item for item in reversed(tool_payloads) if item.get("view") == "source"),
                None,
            )
            if source_payload is None:
                source_sha = next(
                    (
                        item.get("revision", {}).get("sha256")
                        for item in tool_payloads
                        if isinstance(item.get("revision"), dict)
                    ),
                    None,
                )
                if not isinstance(source_sha, str):
                    raise AssertionError("inspect response did not expose source SHA")
                return self._tool_chunk(
                    model,
                    call_id="call_document_read",
                    name="document_read",
                    arguments={"view": "source"},
                )
            reset_button = '<button id="btn-reset" class="btn-outline">Reset</button>'
            return self._tool_chunk(
                model,
                call_id="call_document_patch",
                name="document_patch",
                arguments={
                    "expectedSha256": source_payload["sha256"],
                    "edits": [
                        {
                            "expectedText": reset_button,
                            "replacement": reset_button + e2e._SOURCE_PATCH_INSERTION,
                        }
                    ],
                },
            )

        mutations: list[dict[str, str]] = []
        for annotation in annotation_payload["annotations"]:
            target_kind = annotation["selection"]["kind"]
            locations = annotation["initialLocations"]
            if target_kind == "button":
                location = next(
                    item for item in locations if item["operation"] == "set_style"
                )
                mutation = {
                    "grant_token": location["grantToken"],
                    "input": "background-color: #ef4444",
                }
            elif target_kind == "image":
                location = next(
                    item for item in locations if item["operation"] == "remove_node"
                )
                mutation = {
                    "grant_token": location["grantToken"],
                }
            else:
                location = next(
                    item for item in locations if item["operation"] == "replace_text"
                )
                mutation = {
                    "grant_token": location["grantToken"],
                    "input": e2e._TITLE_TEXT,
                }
            mutations.append(mutation)
        return self._tool_chunk(
            model,
            call_id="call_document_apply",
            name="document_apply",
            arguments={"mutations": mutations},
        )


def test_scenario_matrix_has_approved_42_63_64_budget() -> None:
    e2e._assert_scenario_plan()

    assert sum(row.expected_physical_calls for row in e2e.SCENARIOS) == 42
    assert e2e.WORST_CASE_PHYSICAL_CALLS == 63
    assert e2e.HARD_PHYSICAL_CALL_CAP == 64
    assert sum(row.zero_call_preflight for row in e2e.SCENARIOS) == 3
    mutation_cases = [row for row in e2e.SCENARIOS if not row.zero_call_preflight]
    assert mutation_cases
    assert all(
        row.expected_tools == e2e._ANNOTATION_TOOLS
        for row in mutation_cases
    )


def test_physical_call_budget_refuses_overrun() -> None:
    with pytest.raises(ValueError, match="between 63 and 64"):
        e2e.PhysicalCallBudget(hard_cap=62)

    budget = e2e.PhysicalCallBudget(hard_cap=64)
    budget.reserve("baseline", 42)
    budget.reserve("retry", 16)
    budget.reserve("ensemble_extra", 5)
    budget.claim("baseline", 42)
    budget.claim("retry", 16)
    budget.claim("ensemble_extra", 5)

    assert budget.observed == 63
    with pytest.raises(RuntimeError, match="exceeded"):
        budget.claim("ensemble_extra")


def test_direct_repair_loop_requires_one_decision_per_provider_request() -> None:
    exact = (
        ("document_inspect",),
        ("document_read",),
        ("document_patch",),
        ("document_browser_inspect",),
        ("document_browser_screenshot",),
        ("document_patch",),
        ("document_browser_inspect",),
        ("document_finish",),
        (),
    )
    assert e2e._direct_repair_loop_verified(exact) is True

    grouped_read_and_patch = (
        exact[0],
        ("document_read", "document_patch"),
        *exact[3:],
    )
    assert e2e._direct_repair_loop_verified(grouped_read_and_patch) is False


def test_worker_environment_contains_only_tokenrhythm_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-secret")
    monkeypatch.setenv("TOKENRHYTHM_BASE_URL", "https://attacker.invalid")
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid")

    env = e2e._worker_environment("synthetic-rotated-key")

    assert env["TOKENRHYTHM_API_KEY"] == "synthetic-rotated-key"
    assert "OPENAI_API_KEY" not in env
    assert "TOKENRHYTHM_BASE_URL" not in env
    assert "HTTP_PROXY" not in env
    assert "HOME" not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["OPENSQUILLA_LIVE_DISABLE_DOTENV"] == "1"


def test_isolated_home_environment_supports_path_home_in_child(tmp_path: Path) -> None:
    isolated_home = tmp_path / "user-state"
    isolated_home.mkdir()
    env = e2e._worker_environment("synthetic-rotated-key")

    e2e._apply_isolated_home_environment(env, isolated_home)

    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    assert Path(result.stdout.strip()) == isolated_home.resolve()
    assert env["HOME"] == str(isolated_home.resolve())
    assert env["USERPROFILE"] == str(isolated_home.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows isolated-profile ACL smoke")
def test_isolated_home_environment_supports_windows_acl_hardening(tmp_path: Path) -> None:
    isolated_home = tmp_path / "user-state"
    isolated_home.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    env = e2e._worker_environment("synthetic-rotated-key")
    e2e._apply_isolated_home_environment(env, isolated_home)
    env["PYTHONPATH"] = str(e2e.SRC_DIR)
    code = (
        "from pathlib import Path; import sys; "
        "from opensquilla.sandbox.upgrade_migration import "
        "_current_windows_user_sid, _protect_private_path; "
        "print('acl-child-imported', flush=True); "
        "sid = _current_windows_user_sid(); "
        "print(f'acl-child-sid={sid}', flush=True); "
        "_protect_private_path(Path(sys.argv[1]), directory=True, "
        "windows_user_sid=sid); "
        "print('acl-child-protected', flush=True)"
    )

    try:
        subprocess.run(
            [sys.executable, "-c", code, str(protected)],
            check=True,
            capture_output=True,
            env=env,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"isolated Windows ACL hardening timed out: stdout={exc.stdout!r} "
            f"stderr={exc.stderr!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"isolated Windows ACL hardening failed: stdout={exc.stdout!r} "
            f"stderr={exc.stderr!r}"
        ) from exc


def test_live_harness_checks_each_feature_default_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_store = tmp_path / "opensquilla-webui" / "src" / "stores" / "app.ts"
    app_store.parent.mkdir(parents=True)
    app_store.write_text(
        "artifactPromptAnnotations: false,\ndocumentWorkbenchResources: false,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)

    assert e2e._feature_defaults() == {
        "artifactPromptAnnotations": False,
        "documentWorkbenchResources": False,
    }
    app_store.write_text(
        "artifactPromptAnnotations: false,\ndocumentWorkbenchResources: true,\n",
        encoding="utf-8",
    )
    assert e2e._feature_defaults() == {
        "artifactPromptAnnotations": False,
        "documentWorkbenchResources": True,
    }
    app_store.write_text(
        "artifactPromptAnnotations: hasNativeBridge(),\n"
        "documentWorkbenchResources: true,\n",
        encoding="utf-8",
    )
    assert e2e._feature_defaults() == {
        "artifactPromptAnnotations": True,
        "documentWorkbenchResources": True,
    }


def test_incomplete_report_is_explicit_safe_and_zero_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        e2e,
        "_feature_defaults",
        lambda: {
            "artifactPromptAnnotations": True,
            "documentWorkbenchResources": True,
        },
    )
    report = e2e._incomplete_report(hard_cap=64)
    e2e._assert_report_safe(report, {"TOKENRHYTHM_API_KEY": "secret-never-present"})

    assert report["certification"] == "incomplete"
    assert report["featureDefaultEnabled"] is True
    assert report["featureDefaults"] == {
        "artifactPromptAnnotations": True,
        "documentWorkbenchResources": True,
    }
    assert report["physicalCallBudget"]["observed"] == 0
    assert all(row["status"] == "not_run" for row in report["cases"])
    assert report["reasonCodes"] == ["live_gateway_executor_failed"]
    assert all(row["providerCalled"] is False for row in report["cases"])


def _passing_evidence(scenario) -> object:
    if scenario.zero_call_preflight:
        return e2e.CaseEvidence(
            before_hash_verified=True,
            mode_verified=True,
            router_tier_verified=True,
            passed=True,
            status="passed",
            reason_code="none",
        )
    return e2e.CaseEvidence(
        observed_physical_calls=scenario.expected_physical_calls,
        provider_called=True,
        before_hash_verified=True,
        after_hash_verified=True,
        single_revision_verified=True,
        single_change_set_verified=True,
        accepted_annotations_verified=True,
        mode_verified=True,
        router_tier_verified=True,
        observed_tools=tuple(
            sorted({"document_inspect", e2e._expected_writer_name(scenario)})
        ),
        writer_calls=e2e._expected_writer_calls(scenario),
        writer_attempts=e2e._expected_writer_calls(scenario),
        proposer_tool_calls=0,
        aggregator_tools_verified=True,
        revert_verified=True,
        passed=True,
        status="passed",
        reason_code="none",
    )


def test_certification_reserves_each_case_and_completes_from_evidence_not_feature_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_reserve = e2e.PhysicalCallBudget.reserve

    def recording_reserve(self, kind, count):
        events.append(f"reserve:{kind}:{count}")
        return original_reserve(self, kind, count)

    monkeypatch.setattr(e2e.PhysicalCallBudget, "reserve", recording_reserve)
    monkeypatch.setattr(
        e2e,
        "_feature_defaults",
        lambda: {
            "artifactPromptAnnotations": True,
            "documentWorkbenchResources": True,
        },
    )

    class FakeDriver:
        async def start(self) -> None:
            events.append("start")

        async def run_case(self, scenario):
            events.append(f"run:{scenario.case}")
            return _passing_evidence(scenario)

        async def close(self) -> None:
            events.append("close")

    report = asyncio.run(e2e._run_certification(FakeDriver(), hard_cap=64))
    e2e._assert_report_safe(report, {})

    for scenario in e2e.SCENARIOS:
        run = f"run:{scenario.case}"
        assert run in events
        if scenario.expected_physical_calls:
            reservation = f"reserve:baseline:{scenario.expected_physical_calls}"
            assert events.index(reservation) < events.index(run)
    assert events[-1] == "close"
    assert report["certification"] == "complete"
    assert report["featureDefaultEnabled"] is True
    assert report["reasonCodes"] == []
    assert report["physicalCallBudget"]["observed"] == 42
    assert all(row["status"] == "passed" for row in report["cases"])


def test_certification_closes_driver_and_never_invents_report_after_executor_failure() -> None:
    events: list[str] = []

    class FailingDriver:
        async def start(self) -> None:
            events.append("start")

        async def run_case(self, scenario):
            events.append(f"run:{scenario.case}")
            raise RuntimeError("synthetic executor failure")

        async def close(self) -> None:
            events.append("close")

    with pytest.raises(RuntimeError, match="synthetic executor failure"):
        asyncio.run(e2e._run_certification(FailingDriver(), hard_cap=64))
    assert events == ["start", "run:discarded_annotation_zero_call", "close"]


def test_certification_rejects_case_that_exceeds_its_pre_reserved_calls() -> None:
    class OverrunningDriver:
        async def start(self) -> None:
            return None

        async def run_case(self, scenario):
            if scenario.zero_call_preflight:
                return _passing_evidence(scenario)
            return e2e.CaseEvidence(
                observed_physical_calls=scenario.expected_physical_calls + 1,
                provider_called=True,
            )

        async def close(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="reserved physical-call budget"):
        asyncio.run(e2e._run_certification(OverrunningDriver(), hard_cap=64))


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_owned_gateway_preflights_use_real_rpc_bridge_and_zero_provider_calls(
    tmp_path: Path,
) -> None:
    driver = e2e.GatewayCertificationDriver(
        temp_root=tmp_path,
        api_key="synthetic-key-must-never-be-sent",
        timeout_seconds=20.0,
        # Any accidental provider request fails immediately.  The three
        # preflights must be rejected by ingress/selection before that point.
        provider_endpoint="http://127.0.0.1:9/v1",
        preload_router=False,
    )
    try:
        await driver.start()
        for scenario in e2e.SCENARIOS[:3]:
            evidence = await driver.run_case(scenario)
            assert evidence.passed is True, scenario.case
            assert evidence.provider_called is False
            assert evidence.observed_physical_calls == 0
    finally:
        await driver.close()


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_owned_gateway_html_workbench_lifecycle_is_offline_and_immutable(
    tmp_path: Path,
) -> None:
    """Exercise the complete offline upload-to-publish lifecycle over real RPC."""

    provider = _DeterministicArtifactProvider()
    provider.start()
    driver = e2e.GatewayCertificationDriver(
        temp_root=tmp_path,
        api_key="synthetic-key-for-loopback-provider-only",
        timeout_seconds=20.0,
        provider_endpoint=provider.endpoint,
        allow_local_test_model_overrides=True,
        preload_router=False,
    )
    storage: SessionStorage | None = None
    try:
        await driver.start()
        created = await driver.client.call(
            "sessions.create",
            {"agentId": "main", "kind": "webchat"},
        )
        session_key = str(created["key"])
        session_id = str(created["sessionId"])
        original = (
            b"<!doctype html><html><body><main><h1>Draft</h1>"
            b'<img id="hero" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
            b"<p>Keep byte-for-byte</p></main></body></html>"
        )
        envelope, _writes = build_transcript_attachment_envelope(
            text="synthetic HTML upload",
            attachments=[
                {
                    "type": "text/html",
                    "data": base64.b64encode(original).decode("ascii"),
                    "name": "uploaded.html",
                    "_was_staged": True,
                }
            ],
            session_id=session_id,
            media_root=driver.media_root,
            persist_enabled=True,
        )
        attachment = json.loads(envelope)["attachments"][0]
        attachment_id = str(attachment["attachment_id"])
        source_sha256 = hashlib.sha256(original).hexdigest()
        assert attachment["sha256_ref"] == source_sha256

        # The transcript material is the upload boundary. It is inserted into
        # the owned Gateway's durable store without starting an agent turn.
        gateway_db_path = driver.state_dir / "state" / "sessions.db"
        storage = SessionStorage(str(gateway_db_path))
        await storage.connect()
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id=session_id,
                session_key=session_key,
                message_id="synthetic-upload-message",
                role="user",
                content=envelope,
            )
        )

        def artifact_counts() -> tuple[int, int, int, int]:
            with sqlite3.connect(gateway_db_path, timeout=5) as conn:
                row = conn.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM artifact_documents WHERE session_id = ?),
                      (SELECT COUNT(*) FROM artifact_revisions r
                         JOIN artifact_documents d ON d.document_id = r.document_id
                        WHERE d.session_id = ?),
                      (SELECT COUNT(*) FROM artifact_change_sets c
                         JOIN artifact_documents d ON d.document_id = c.document_id
                        WHERE d.session_id = ?),
                      (SELECT COUNT(*) FROM artifact_edit_sessions e
                         JOIN artifact_documents d ON d.document_id = e.document_id
                        WHERE d.session_id = ?)
                    """,
                    (session_id, session_id, session_id, session_id),
                ).fetchone()
            assert row is not None
            return int(row[0]), int(row[1]), int(row[2]), int(row[3])

        assert artifact_counts() == (0, 0, 0, 0)
        inventory = await driver.client.call(
            "workbench.resources.list",
            {"sessionKey": session_key},
        )
        listed_attachments = [
            item
            for item in inventory["resources"]
            if (item.get("resourceRef") or item.get("resource", {})).get("type")
            == "attachment"
            and (item.get("resourceRef") or item.get("resource", {})).get("id")
            == attachment_id
        ]
        assert listed_attachments, inventory
        listed_attachment = listed_attachments[0]
        assert listed_attachment["sha256"] == source_sha256
        assert listed_attachment["capabilities"]["preview"] is True
        assert listed_attachment["capabilities"]["edit"] is True

        previewed = await driver.client.call(
            "workbench.previews.create",
            {
                "sessionKey": session_key,
                "resourceRef": {"type": "attachment", "id": attachment_id},
                "mode": "isolated",
            },
        )
        assert previewed["preview"]["sandboxProfile"] == "opaque-offline"
        assert previewed["preview"]["network"] is False
        assert previewed["preview"]["adapter"]["sourceSha256"] == source_sha256
        assert artifact_counts() == (0, 0, 0, 0)

        import_params = {
            "sessionKey": session_key,
            "source": {"type": "attachment", "id": attachment_id},
            "mode": "copy",
            "expectedSha256": source_sha256,
            "idempotencyKey": "offline-upload-import",
        }
        imported = await driver.client.call("documents.import", import_params)
        document_id = str(imported["document"]["id"])
        initial_revision_id = str(imported["revision"]["id"])
        assert imported["revision"]["generation"] == 1
        assert imported["revision"]["sha256"] == source_sha256
        assert imported["receipt"]["replayed"] is False
        assert artifact_counts() == (1, 1, 0, 0)

        replayed = await driver.client.call("documents.import", import_params)
        assert replayed["document"]["id"] == document_id
        assert replayed["revision"]["id"] == initial_revision_id
        assert replayed["receipt"]["replayed"] is True
        assert artifact_counts() == (1, 1, 0, 0)

        started = await driver.client.call(
            "documents.editSessions.start",
            {
                "sessionKey": session_key,
                "documentId": document_id,
                "mode": "edit",
                "clientRequestId": "offline-first-source-open",
            },
        )
        edit_session = started["editSession"]
        assert edit_session["status"] == "active"
        assert edit_session["lastSavedRevisionId"] == initial_revision_id
        assert artifact_counts() == (1, 1, 0, 1)

        source = (
            await driver.client.call(
                "artifacts.source.read",
                {"sessionKey": session_key, "documentId": document_id},
            )
        )["source"]
        start = source["text"].index("Draft")
        first_patch = await driver.client.call(
            "artifacts.source.patch",
            {
                "sessionKey": session_key,
                "documentId": document_id,
                "expectedHeadRevisionId": source["revisionId"],
                "expectedStateRevision": source["stateRevision"],
                "expectedSourceSha256": source["sha256"],
                "offsetEncoding": source["offsetEncoding"],
                "clientRequestId": "offline-edit-session-save",
                "editSessionId": edit_session["id"],
                "expectedEditSessionStateRevision": edit_session["stateRevision"],
                "expectedLastSavedRevisionId": edit_session["lastSavedRevisionId"],
                "patches": [
                    {
                        "startOffset": start,
                        "endOffset": start + len("Draft"),
                        "replacement": "Published",
                    }
                ],
            },
        )
        saved_revision_id = str(first_patch["revision"]["id"])
        saved_edit_session = first_patch["editSession"]
        assert saved_edit_session["lastSavedRevisionId"] == saved_revision_id
        assert artifact_counts() == (1, 2, 1, 1)

        closed = await driver.client.call(
            "documents.editSessions.close",
            {
                "sessionKey": session_key,
                "editSessionId": saved_edit_session["id"],
                "expectedStateRevision": saved_edit_session["stateRevision"],
            },
        )
        assert closed["editSession"]["status"] == "closed"

        # Bind one source-proven visual annotation to the freshly saved head,
        # then let the real restricted turn surface the ten-tool autonomous
        # loop and atomically remove the selected void element through
        # document_apply followed by preview verification and finish.
        saved_source = (
            await driver.client.call(
                "artifacts.source.read",
                {"sessionKey": session_key, "documentId": document_id},
            )
        )["source"]
        image_path = json.dumps(
            [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "img", 1]],
            separators=(",", ":"),
        )
        dom_sha256, element_proof_sha256 = e2e._source_proofs(
            saved_source["text"],
            image_path,
        )
        annotation_id = f"ann_{os.urandom(16).hex()}"
        selection_id = f"sel_{os.urandom(16).hex()}"
        active_artifact_id = str(first_patch["revision"]["artifactId"])
        driver.bridge.register(
            e2e._BridgeSelection(
                active_preview_artifact_id=active_artifact_id,
                selection_id=selection_id,
                tag_name="img",
                element_path=image_path,
                dom_sha256=dom_sha256,
                element_proof_sha256=element_proof_sha256,
                scope_id=session_key,
            )
        )
        created_annotation = await driver.client.call(
            "artifacts.prompt_annotations.create",
            {
                "annotationId": annotation_id,
                "sessionKey": session_key,
                "documentId": document_id,
                "revisionId": saved_revision_id,
                "selection": {
                    "selectionId": selection_id,
                    "tagName": "img",
                    "elementPath": image_path,
                    "domSha256": dom_sha256,
                    "elementProofSha256": element_proof_sha256,
                },
                "body": "",
            },
        )
        await driver.client.call(
            "artifacts.prompt_annotations.update",
            {
                "sessionKey": session_key,
                "annotationId": annotation_id,
                "expectedStateRevision": created_annotation["annotation"]["stateRevision"],
                "body": "Remove only the selected image and preserve every other byte.",
            },
        )
        accepted = await driver.client.call(
            "chat.send",
            {
                "sessionKey": session_key,
                "message": (
                    "Apply the attached artifact annotation, verify the preview, then commit."
                ),
                "clientRequestId": "offline-loop-remove-node",
                "promptAnnotationIds": [annotation_id],
            },
        )
        assert accepted["acceptedPromptAnnotationIds"] == [annotation_id]
        task = await driver._wait_for_task(session_key, str(accepted["task_id"]))
        assert task["status"] == "succeeded", task
        agent_source = (
            await driver.client.call(
                "artifacts.source.read",
                {"sessionKey": session_key, "documentId": document_id},
            )
        )["source"]
        assert '<img id="hero"' not in agent_source["text"]
        assert "<h1>Published</h1>" in agent_source["text"]
        assert "<p>Keep byte-for-byte</p>" in agent_source["text"]
        assert artifact_counts() == (1, 3, 2, 1)
        trace = e2e._trace_evidence(driver._session_trace(session_key), mode="direct")
        assert trace.physical_calls == 5
        assert trace.provider_calls == trace.physical_calls
        assert trace.loop_continuation_calls == 2
        assert trace.surfaced_tools_exact is True
        assert trace.observed_tools == (
            "document_apply",
            "document_browser_inspect",
            "document_finish",
            "document_inspect",
        )
        assert trace.writer_calls == 1
        assert trace.writer_attempts == 1
        assert trace.writer_succeeded is True
        assert trace.restricted_prompt_verified is True

        published = await driver.client.call(
            "documents.publish",
            {
                "sessionKey": session_key,
                "documentId": document_id,
                "revisionId": agent_source["revisionId"],
                "idempotencyKey": "offline-document-publish",
            },
        )
        publication = published["publication"]
        deliverable_id = str(publication["deliverableId"])
        immutable_bytes = (
            original.replace(b"Draft", b"Published")
            .replace(
                b'<img id="hero" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">',
                b"",
            )
        )
        immutable_sha256 = hashlib.sha256(immutable_bytes).hexdigest()
        assert publication["sha256"] == immutable_sha256
        assert artifact_counts() == (1, 3, 2, 1)
        _ref, deliverable_path = ArtifactStore(driver.media_root).resolve_for_download(
            deliverable_id,
            session_id=session_id,
        )
        assert Path(deliverable_path).read_bytes() == immutable_bytes

        latest = (
            await driver.client.call(
                "artifacts.source.read",
                {"sessionKey": session_key, "documentId": document_id},
            )
        )["source"]
        later_start = latest["text"].index("Published")
        later = await driver.client.call(
            "artifacts.source.patch",
            {
                "sessionKey": session_key,
                "documentId": document_id,
                "expectedHeadRevisionId": latest["revisionId"],
                "expectedStateRevision": latest["stateRevision"],
                "expectedSourceSha256": latest["sha256"],
                "offsetEncoding": latest["offsetEncoding"],
                "patches": [
                    {
                        "startOffset": later_start,
                        "endOffset": later_start + len("Published"),
                        "replacement": "Later",
                    }
                ],
            },
        )
        assert later["revision"]["sha256"] != immutable_sha256
        assert artifact_counts() == (1, 4, 3, 1)
        _same_ref, same_path = ArtifactStore(driver.media_root).resolve_for_download(
            deliverable_id,
            session_id=session_id,
        )
        assert Path(same_path).read_bytes() == immutable_bytes
        assert hashlib.sha256(Path(same_path).read_bytes()).hexdigest() == immutable_sha256
        published_resource = await driver.client.call(
            "workbench.resources.get",
            {
                "sessionKey": session_key,
                "resourceRef": {"type": "deliverable", "id": deliverable_id},
            },
        )
        download_url = str(published_resource["resource"]["downloadUrl"])
        assert download_url == f"/api/v1/artifacts/{deliverable_id}"

        def download_published_snapshot() -> bytes:
            assert driver.port is not None
            url = (
                f"http://127.0.0.1:{driver.port}{download_url}"
                f"?sessionKey={quote(session_key, safe='')}"
            )
            with urlopen(url, timeout=5) as response:  # noqa: S310 - owned loopback Gateway
                assert response.status == 200
                return response.read()

        assert await asyncio.to_thread(download_published_snapshot) == immutable_bytes
        assert (
            driver.media_root / "transcripts" / session_id / source_sha256
        ).read_bytes() == original
        assert not any(
            record.get("kind") == "llm_request"
            and record.get("session_key") != session_key
            for record in e2e._read_turn_call_records(driver.turn_log_dir)
        )
        # inspect → apply(candidate) → browser_inspect → finish(commit) →
        # tools=[] finalizer
        assert len(provider.requests) == 5
    finally:
        if storage is not None:
            await storage.close()
        await driver.close()
        provider.close()


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_owned_gateway_mutations_use_real_rpc_and_local_provider(
    tmp_path: Path,
) -> None:
    provider = _DeterministicArtifactProvider()
    provider.start()
    driver = e2e.GatewayCertificationDriver(
        temp_root=tmp_path,
        api_key="synthetic-key-for-local-provider-only",
        # The first routed case cold-loads the recommended local router model.
        # Keep this below the live harness default while allowing that one-time
        # startup cost on slower CI hosts.
        timeout_seconds=45.0,
        provider_endpoint=provider.endpoint,
        allow_local_test_model_overrides=True,
    )
    try:
        await driver.start()
        scenarios = [row for row in e2e.SCENARIOS if not row.zero_call_preflight]
        evidences = []
        for scenario in scenarios:
            request_start = len(provider.requests)
            evidence = await driver.run_case(scenario)
            case_requests = provider.requests[request_start:]
            evidences.append(evidence)
            assert evidence.passed is True, scenario.case
            assert evidence.observed_physical_calls == scenario.expected_physical_calls
            assert evidence.writer_calls == e2e._expected_writer_calls(scenario)
            assert evidence.writer_attempts == e2e._expected_writer_calls(scenario)
            assert evidence.single_revision_verified is True
            assert evidence.single_change_set_verified is True
            assert evidence.revert_verified is True
            if scenario.case == "direct_single_annotation":
                assert len(case_requests) == 9
                surfaced = [
                    {
                        item.get("function", {}).get("name")
                        for item in request.get("tools") or []
                    }
                    for request in case_requests
                ]
                assert all(names == e2e._ALLOWED_TOOLS for names in surfaced[:-1])
                assert surfaced[-1] == set()
                assert {request.get("model") for request in case_requests} == {"glm-5.2"}
        # The approved 42-call budget counts every raw provider request,
        # including preview, finish, and tools=[] finalizer continuations.
        assert len(provider.requests) == sum(item.provider_calls for item in evidences)
        assert sum(item.observed_physical_calls for item in evidences) == 42
        assert len(provider.requests) <= e2e.HARD_PHYSICAL_CALL_CAP
        prompt_reports = [
            record["payload"]
            for record in e2e._read_turn_call_records(driver.turn_log_dir)
            if record.get("kind") == "prompt_report"
        ]
        assert len(prompt_reports) == len(scenarios)
        assert all(report.get("injected_workspace_files_count") == 0 for report in prompt_reports)
        assert all(report.get("skill_count") == 0 for report in prompt_reports)
        assert all(report.get("skills_prompt_chars") == 0 for report in prompt_reports)
        assert all(report.get("bootstrap_files") == [] for report in prompt_reports)
    finally:
        await driver.close()
        provider.close()


def test_report_guard_rejects_runtime_payload_fields_and_call_overrun() -> None:
    report = e2e._incomplete_report(hard_cap=64)
    report["prompt"] = "must not persist"
    with pytest.raises(RuntimeError, match="top-level schema"):
        e2e._assert_report_safe(report, {})

    report = e2e._incomplete_report(hard_cap=64)
    report["physicalCallBudget"]["observed"] = 65
    with pytest.raises(RuntimeError, match="physical-call cap"):
        e2e._assert_report_safe(report, {})


def test_report_guard_rejects_forged_passing_mutation_evidence() -> None:
    evidence = {
        scenario.case: _passing_evidence(scenario)
        for scenario in e2e.SCENARIOS
    }
    report = e2e._report(hard_cap=64, evidences=evidence)
    e2e._assert_report_safe(report, {})

    direct = next(row for row in report["cases"] if row["case"] == "direct_single_annotation")
    direct["writerCalls"] = 0
    with pytest.raises(RuntimeError, match="passed mutation"):
        e2e._assert_report_safe(report, {})


def test_local_model_capability_override_is_loopback_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback provider"):
        e2e.GatewayCertificationDriver(
            temp_root=tmp_path,
            api_key="synthetic",
            timeout_seconds=20.0,
            provider_endpoint="https://tokenrhythm.studio/v1",
            allow_local_test_model_overrides=True,
        )


def test_main_requires_both_attestations_before_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-rotated-key")
    monkeypatch.setattr(
        e2e,
        "_launch_worker",
        lambda **_kwargs: pytest.fail("worker must not start without attestations"),
    )
    monkeypatch.setattr(sys, "argv", ["e2e", "--output", str(output)])
    assert e2e.main() == 2
    assert not output.exists()

    monkeypatch.setattr(
        sys,
        "argv",
        ["e2e", "--output", str(output), "--confirm-live-cost"],
    )
    assert e2e.main() == 2
    assert not output.exists()


@pytest.mark.parametrize("matrix_timeout", [299, 901])
def test_main_rejects_out_of_bounds_matrix_timeout_before_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    matrix_timeout: int,
) -> None:
    output = tmp_path / "report.json"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-rotated-key")
    monkeypatch.setattr(
        e2e,
        "_launch_worker",
        lambda **_kwargs: pytest.fail("worker must not start with an invalid matrix timeout"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "e2e",
            "--output",
            str(output),
            "--confirm-live-cost",
            "--confirm-rotated-key",
            "--matrix-timeout-seconds",
            str(matrix_timeout),
        ],
    )

    assert e2e.main() == 2
    assert not output.exists()


def test_launch_worker_keeps_case_and_matrix_timeouts_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["timeout"] = kwargs["timeout"]
        observed["cwd"] = kwargs["cwd"]
        observed["env"] = kwargs["env"]
        kwargs["stdout"].write(
            json.dumps(e2e._incomplete_report(hard_cap=e2e.HARD_PHYSICAL_CALL_CAP))
        )
        kwargs["stdout"].flush()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(e2e.subprocess, "run", fake_run)

    report = e2e._launch_worker(
        api_key="synthetic-rotated-key",
        hard_cap=e2e.HARD_PHYSICAL_CALL_CAP,
        timeout_seconds=17.0,
        matrix_timeout_seconds=444.0,
    )

    command = observed["command"]
    assert isinstance(command, list)
    case_timeout_index = command.index("--timeout-seconds") + 1
    assert command[case_timeout_index] == "17.0"
    assert "--matrix-timeout-seconds" not in command
    assert observed["timeout"] == 444.0
    worker_root = Path(str(observed["cwd"])).resolve()
    worker_env = observed["env"]
    assert isinstance(worker_env, dict)
    assert Path(worker_env["HOME"]) == worker_root / "user-state"
    assert Path(worker_env["USERPROFILE"]) == worker_root / "user-state"
    assert report["certification"] == "incomplete"


def test_live_parser_defaults_to_glm_safe_case_timeout() -> None:
    args = e2e._parser().parse_args(
        [
            "--output",
            "/tmp/opensquilla-prompt-annotations.json",
            "--confirm-live-cost",
            "--confirm-rotated-key",
        ]
    )

    assert args.timeout_seconds == e2e.DEFAULT_CASE_TIMEOUT_SECONDS == 120.0


def test_launch_worker_rejects_unbounded_matrix_timeout() -> None:
    with pytest.raises(ValueError, match="bounded certification window"):
        e2e._launch_worker(
            api_key="synthetic-rotated-key",
            hard_cap=e2e.HARD_PHYSICAL_CALL_CAP,
            timeout_seconds=17.0,
            matrix_timeout_seconds=e2e.MAX_MATRIX_TIMEOUT_SECONDS + 1,
        )


def test_main_runs_real_isolated_scaffold_without_network_and_returns_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "report.json"
    key = "synthetic-rotated-key-never-persist"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", key)
    monkeypatch.delenv("TOKENRHYTHM_BASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "e2e",
            "--output",
            str(output),
            "--confirm-live-cost",
            "--confirm-rotated-key",
        ],
    )

    assert e2e.main() == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["certification"] == "incomplete"
    assert payload["physicalCallBudget"]["observed"] == 0
    assert key not in output.read_text(encoding="utf-8")
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr()
    assert key not in printed.out
    assert key not in printed.err
    assert "cases" not in printed.out
