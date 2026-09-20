from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest

from scripts import live_attachment_acceptance as harness
from scripts import live_harness_security as security
from scripts.live_tokenrhythm_budget import ATTACHMENT_PHASE_CALL_LIMITS, FunctionalRequestLog


def assert_private_file(path):
    if os.name == "nt":
        from opensquilla.private_paths import windows_path_has_private_dacl

        assert windows_path_has_private_dacl(path, directory=False, require_protected=True)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.fixture
def prepared(tmp_path):
    fixtures = tmp_path / "fixtures"
    manifest = tmp_path / "oracle.json"
    value = harness.prepare_fixtures(fixtures, manifest)
    return fixtures, manifest, value


def test_prepare_and_preflight_verify_content_and_scan_pages_without_network(prepared, monkeypatch):
    fixtures, manifest, value = prepared

    def no_network(*args, **kwargs):
        pytest.fail("offline preparation attempted network access")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    result = harness.preflight(fixtures, manifest)
    assert result["ok"] and result["physical_calls"] == 0
    assert len(value["fixtures"]) == len({item["id"] for item in value["fixtures"]}) == 14
    assert {item["case"]: item["textless_pages"] for item in result["checks"]
            if item["pages"]} == {
                "five-pages-text.pdf": 0, "five-pages-scan.pdf": 5, "five-pages-mixed.pdf": 2,
                **{f"receipt-{index}.pdf": 0 for index in range(1, 6)},
            }
    batch = value["consumption_batches"]["ten_document_uploads"]
    assert len(batch) == 10 and sum(name.endswith(".pdf") for name in batch) == 5
    receipts = [item for item in value["fixtures"] if item["name"].startswith("receipt-")]
    assert len({item["answers"][0] for item in receipts}) == 5
    assert all(item["pages"] == 1 for item in receipts)
    assert sum(value["phase_limits"].values()) == 60
    assert len(value["pressure_files"]) == 3
    for item in value["fixtures"]:
        assert all(answer not in item["prompt"] for answer in item["answers"])
        assert all(answer not in item["name"] for answer in item["answers"])
        assert_private_file(fixtures / item["name"])
    for item in value["pressure_files"]:
        assert_private_file(fixtures / item["name"])
    assert_private_file(manifest)
    (fixtures / "record.txt").write_text("changed synthetic source")
    assert harness.preflight(fixtures, manifest)["ok"] is False


def test_fixture_oracle_and_existing_sources_are_protected(tmp_path, prepared):
    fixtures, manifest, _ = prepared
    with pytest.raises(ValueError, match="oracle_must_be_outside"):
        harness.prepare_fixtures(tmp_path / "other", tmp_path / "other" / "oracle.json")
    original = (fixtures / "record.txt").read_bytes()
    with pytest.raises(ValueError, match="fixture_root_must_be_empty"):
        harness.prepare_fixtures(fixtures, manifest)
    assert (fixtures / "record.txt").read_bytes() == original


def test_serve_requires_live_opt_in_before_relay_or_provider(capsys):
    assert harness.main(["serve"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "live_opt_in_required"


def test_wrong_checkout_fails_before_relay_or_provider(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        harness.main(["serve", "--live", "--expected-source-root", str(tmp_path)])
    assert error.value.code == 2
    assert "expected-source-root does not match" in capsys.readouterr().err


@pytest.mark.parametrize("payload,ok,status", [
    ({"status": "blocked", "message": "private-synthetic-detail"}, False, "blocked"),
    ({"status": "elevation_required", "path": "private-synthetic-path"},
     False, "elevation_required"),
    ({"error": "private-synthetic-error"}, False, None),
    ({"status": "ok", "content": "private-synthetic-content"}, True, "ok"),
    ({"status": "private synthetic content"}, True, None),
])
def test_tool_outcome_preserves_denials_without_exporting_content(payload, ok, status):
    result = harness.tool_outcome(json.dumps(payload))
    assert result["ok"] is ok and result.get("status") == status
    assert "private-synthetic" not in json.dumps(result)


def relay_ready(tmp_path):
    log_path = tmp_path / "requests.sqlite"
    log = FunctionalRequestLog(log_path, enabled=True, phase_limits=ATTACHMENT_PHASE_CALL_LIMITS)
    ready = tmp_path / "ready.json"
    ready.write_text(json.dumps({
        "mode": "functional", "enabled": True, "base_url": "http://127.0.0.1:8765/v1",
        "client_key": "live-budget-placeholder-synthetic-acceptance-test",
        "request_log": str(log_path),
    }))
    return ready, log


def test_relay_requires_shared_phase_allocations_and_never_resets_count(tmp_path):
    ready, log = relay_ready(tmp_path)
    log.select_phase(variant="baseline", case_id="synthetic-case", phase="baseline")
    request = log.start_request(model="synthetic-model", request_bytes=17)
    log.finish_request(request, completed=False, reason="transport_error", response_bytes=0)
    _, reopened = harness.open_relay(ready)
    assert reopened.snapshot()["callsRemaining"] == 59
    assert reopened.snapshot()["phaseCallsRemaining"]["baseline"] == 7
    plain = FunctionalRequestLog(tmp_path / "plain.sqlite", enabled=True)
    data = json.loads(ready.read_text())
    data["request_log"] = str(plain.path)
    ready.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="attachment_phase_allocations_required"):
        harness.open_relay(ready)


@pytest.mark.parametrize("authorized,vision,modality,error", [
    (True, True, True, None),
    (False, True, True, "selected_model_not_in_both_catalogs"),
    (True, False, True, "vision_model_capability_not_verified"),
    (True, True, False, "vision_model_capability_not_verified"),
])
async def test_catalog_requires_both_sources_and_explicit_vision(
    monkeypatch, authorized, vision, modality, error,
):
    seen = []

    async def respond(client, request, **kwargs):
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, request=request, json={
                "data": [{"id": harness.VISION_MODEL}] if authorized else [],
            })
        return httpx.Response(200, request=request, json={"data": [{
            "id": harness.VISION_MODEL, "contextWindow": 262144, "maxOutputTokens": 8192,
            "modalities": ["text", "image"] if modality else ["text"],
            "capabilities": {"vision": vision},
        }]})

    monkeypatch.setattr(httpx.AsyncClient, "send", respond)
    if error:
        with pytest.raises(ValueError, match=error):
            await harness.catalog_evidence("synthetic-placeholder", harness.VISION_MODEL)
    else:
        result = await harness.catalog_evidence("synthetic-placeholder", harness.VISION_MODEL)
        assert result["vision"] and result["context_window_tokens"] == 262144
        assert "synthetic-placeholder" not in json.dumps(result)
    assert len(seen) == 2
    assert seen[0].headers["authorization"] == "Bearer synthetic-placeholder"
    assert "authorization" not in seen[1].headers


async def test_evaluator_reads_actual_schema_and_exports_no_answers(tmp_path, prepared):
    from opensquilla.session.storage import SessionStorage

    fixtures, manifest, value = prepared
    root = tmp_path / "gateway"
    (root / "state").mkdir(parents=True)
    database = root / "state" / "sessions.db"
    storage = await SessionStorage.open(str(database))
    await storage.close()
    item = value["fixtures"][0]
    # Unit-test data only: the live driver never inserts transcript or summary rows.
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO transcript_entries "
                   "(session_id,session_key,message_id,role,content,created_at) "
                   "VALUES ('synthetic','synthetic','synthetic','assistant',?,1)",
                   (item["answers"][0],))
        db.commit()
    result = harness.evaluate(root, fixtures, manifest)
    assert result["cases"][0]["complete_answer_present"] is True
    assert result["cases"][1]["complete_answer_present"] is False
    assert all(case["source_unchanged"] for case in result["cases"])
    assert all(answer not in json.dumps(result) for row in value["fixtures"]
               for answer in row["answers"])
    assert result["status"] == "requires_case_and_browser_review"


@pytest.mark.parametrize("relative_root", ["workspace/project", "state/tasks/managed"])
async def test_workcopy_evidence_uses_session_project_and_survives_reopen(tmp_path, relative_root):
    from opensquilla.session.storage import SessionStorage

    root = tmp_path / "gateway"
    (root / "state").mkdir(parents=True)
    database = root / "state" / "sessions.db"
    storage = await SessionStorage.open(str(database))
    await storage.close()
    project = root / relative_root
    project.mkdir(parents=True)
    source, working = project / "source.txt", project / "working.txt"
    source.write_text("synthetic original")
    working.write_text("synthetic edited")
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO sessions "
                   "(session_key,session_id,created_at,updated_at,origin,execution_workspace) "
                   "VALUES ('synthetic','synthetic',1,1,?,?)", (
                       json.dumps({"attachment_working_files": {"source.txt": {
                           "path": "working.txt", "session_id": "synthetic",
                           "sha256": harness.digest(source.read_bytes()),
                       }}}), json.dumps({"root": str(project)}),
                   ))
        db.commit()
    result = harness.working_file_evidence(root)
    assert len(result) == 1
    assert result[0]["source_unchanged"] and result[0]["copy_differs"]
    assert result[0]["session_matches"]
    assert "synthetic original" not in json.dumps(result) and str(project) not in json.dumps(result)
    assert harness.working_file_evidence(root) == result
    source.write_text("unexpected source edit")
    assert harness.working_file_evidence(root)[0]["source_unchanged"] is False


def test_serve_installs_relay_inside_clean_environment_and_restores_it(
    tmp_path, monkeypatch, capsys,
):
    from scripts import live_tokenrhythm_transport

    ready, _ = relay_ready(tmp_path)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("synthetic UI")
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-ambient-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-unrelated-secret")
    installed = []

    def install():
        assert "UNRELATED_SECRET" not in os.environ
        assert os.environ["TOKENRHYTHM_API_KEY"].startswith("live-budget-placeholder-")
        installed.append(True)
        return lambda: installed.append(False)

    async def serve(args, relay, log):
        assert installed == [True]
        assert os.environ["OPENSQUILLA_LIVE_TRANSPORT"] == "1"
        raise RuntimeError("synthetic-provider-detail-must-not-escape")

    monkeypatch.setattr(live_tokenrhythm_transport, "install_from_env", install)
    monkeypatch.setattr(harness, "serve_gateway", serve)
    report = tmp_path / "report.json"
    assert harness.main([
        "serve", "--live", "--relay-ready", str(ready), "--phase", "file_consumption",
        "--gateway-root", str(tmp_path / "gateway"), "--report", str(report),
        "--ui-dist", str(ui),
    ]) == 1
    assert installed == [True, False]
    assert os.environ["TOKENRHYTHM_API_KEY"] == "synthetic-ambient-secret"
    assert os.environ["UNRELATED_SECRET"] == "synthetic-unrelated-secret"
    assert_private_file(report)
    public = capsys.readouterr().out + report.read_text()
    assert "synthetic-provider-detail" not in public and "synthetic-ambient-secret" not in public


def test_desktop_owner_uses_production_binding_without_network(tmp_path):
    from unittest.mock import patch

    from opensquilla.gateway.boot import _desktop_ownership_profile_home
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.desktop_ownership import (
        DesktopGatewayOwnership,
        desktop_gateway_auth_token,
    )

    root, private = tmp_path / "gateway", tmp_path / "private" / "desktop.json"
    config = GatewayConfig(port=19223)
    with patch.dict(os.environ, {}, clear=True):
        handoff = harness.prepare_desktop_owner(config, root, private)
        owner = DesktopGatewayOwnership.from_environment(
            profile_home=_desktop_ownership_profile_home(config), port=config.port,
        )
        assert owner is not None
        assert owner.instance_id == handoff["instanceId"]
        assert owner.profile_fingerprint == handoff["profileFingerprint"]
        assert desktop_gateway_auth_token(owner.instance_nonce) == handoff["authToken"]
        assert "OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE" not in os.environ
        harness.write_private_desktop_handoff(private, handoff)
    assert json.loads(private.read_text()) == handoff
    assert_private_file(private)
    assert handoff["httpUrl"] == "http://127.0.0.1:19223"
    assert not private.is_relative_to(root)


def test_desktop_owner_refuses_model_visible_handoff_and_existing_file(tmp_path):
    from opensquilla.gateway.config import GatewayConfig

    root = tmp_path / "gateway"
    with pytest.raises(ValueError, match="outside_gateway_root"):
        harness.prepare_desktop_owner(GatewayConfig(), root, root / "workspace" / "owner.json")
    private = tmp_path / "desktop.json"
    private.write_text("existing private file")
    with pytest.raises(FileExistsError):
        harness.write_private_desktop_handoff(private, {"schemaVersion": 1})
    assert private.read_text() == "existing private file"


@pytest.mark.parametrize("kind", ["handoff", "report"])
@pytest.mark.parametrize("acl_fails", [False, True])
def test_windows_output_requires_bound_private_acl_before_writing(
    tmp_path, monkeypatch, kind, acl_fails,
):
    output = tmp_path / "output.json"
    if kind == "report":
        output.write_text("existing report")
    verified = []

    def apply_acl(path, *, directory, expected_device, expected_inode):
        metadata = path.lstat()
        assert directory is False
        assert (expected_device, expected_inode) == (metadata.st_dev, metadata.st_ino)
        assert path.read_bytes() == b""
        verified.append(path)
        if acl_fails:
            raise PermissionError("synthetic ACL failure")

    monkeypatch.setattr(security, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(security, "apply_windows_private_dacl", apply_acl, raising=False)

    def write():
        if kind == "handoff":
            harness.write_private_desktop_handoff(output, {"nonce": "synthetic-nonce"})
        else:
            harness.write_safe_report(output, {"status": "synthetic report"}, ())

    if acl_fails:
        with pytest.raises(PermissionError, match="synthetic ACL failure"):
            write()
        if kind == "handoff":
            assert not output.exists()
        else:
            assert output.read_text() == "existing report"
        assert all(not path.exists() for path in verified)
    else:
        write()
        assert json.loads(output.read_text()) == (
            {"nonce": "synthetic-nonce"} if kind == "handoff"
            else {"status": "synthetic report"}
        )
    assert len(verified) == 1
    assert not list(tmp_path.glob(".output.json.tmp-*"))


def test_windows_permissions_reject_path_that_no_longer_matches_open_file(tmp_path, monkeypatch):
    opened, replacement = tmp_path / "opened", tmp_path / "replacement"
    opened.write_bytes(b"")
    replacement.write_text("synthetic unrelated file")
    verified = []

    def apply_acl(path, *, directory, expected_device, expected_inode):
        assert directory is False
        assert (expected_device, expected_inode) == (opened.stat().st_dev, opened.stat().st_ino)
        assert (expected_device, expected_inode) != (path.stat().st_dev, path.stat().st_ino)
        verified.append(path)
        raise OSError("synthetic bound identity mismatch")

    monkeypatch.setattr(security, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(security, "apply_windows_private_dacl", apply_acl)
    with opened.open("wb") as stream:
        with pytest.raises(OSError, match="bound identity mismatch"):
            security.restrict_private_file_permissions(replacement, descriptor=stream.fileno())
    assert verified == [replacement]
    assert opened.read_bytes() == b""
    assert replacement.read_text() == "synthetic unrelated file"
