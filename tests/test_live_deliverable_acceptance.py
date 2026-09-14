"""Offline checks for the opt-in deliverable acceptance harness."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from zipfile import BadZipFile

import httpx
import pytest
from pptx import Presentation
from pypdf.errors import PdfReadError
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from opensquilla.artifacts import ArtifactStore
from scripts import live_deliverable_acceptance as acceptance
from scripts.live_deliverable_acceptance import (
    MODEL,
    BoundedRelay,
    listed_artifacts,
    load_report,
    restored_sessions,
    validate_bytes,
)
from scripts.live_tokenrhythm_budget import BudgetRejectedError, FunctionalRequestLog

_REPLY = b'{"choices":[{"message":{"content":"offline response"}}]}'


@pytest.fixture
def offline_relay(
    tmp_path: Path,
) -> Iterator[tuple[BoundedRelay, FunctionalRequestLog, list[httpx.Request]]]:
    request_log = FunctionalRequestLog(tmp_path / "requests.sqlite3", enabled=True)
    request_log.select_phase(variant="new", case_id="offline-deliverable")
    sent: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, content=_REPLY, headers={"Content-Type": "application/json"}
        )

    # Exercise the real relay and SQLite recorder without starting a server or
    # creating an HTTP transport capable of reaching a provider.
    relay = BoundedRelay(
        None,
        {},
        api_key="synthetic-offline-secret",
        request_log=request_log,
        transport=httpx.MockTransport(upstream),
    )
    try:
        yield relay, request_log, sent
    finally:
        relay.close()


def _body(**fields: object) -> bytes:
    return json.dumps(
        {"model": MODEL, "messages": [{"role": "user", "content": "offline"}], **fields}
    ).encode()


def _forward(relay: BoundedRelay, body: bytes) -> None:
    with relay.forward(body) as response:
        assert response.status_code == 200
        assert b"".join(response.chunks) == _REPLY


def _assert_rejected(offline_relay, body: bytes, reason: str) -> None:
    relay, request_log, sent = offline_relay
    calls_before = relay.calls
    sent_before = list(sent)
    log_before = request_log.snapshot()
    with pytest.raises(BudgetRejectedError, match=f"^{reason}$"):
        with relay.forward(body):
            pytest.fail("rejected request yielded an upstream response")
    assert relay.calls == calls_before
    assert sent == sent_before
    assert request_log.snapshot() == log_before


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("limit", [1, 8192])
def test_relay_accepts_each_output_limit_boundary(offline_relay, field, limit) -> None:
    relay, request_log, sent = offline_relay
    body = _body(**{field: limit}, n=1)

    _forward(relay, body)

    assert relay.calls == 1
    assert len(sent) == 1
    assert sent[0].method == "POST"
    assert sent[0].url.path == "/v1/chat/completions"
    assert sent[0].content == body
    rows = request_log.snapshot()["requests"]
    assert len(rows) == 1
    assert rows[0]["model"] == MODEL
    assert rows[0]["status"] == "completed"
    assert rows[0]["request_bytes"] == len(body)
    assert rows[0]["response_bytes"] == len(_REPLY)


@pytest.mark.parametrize("model", [None, "", "different-model", MODEL.upper(), f" {MODEL}"])
def test_relay_rejects_wrong_model_before_dispatch(offline_relay, model) -> None:
    _assert_rejected(
        offline_relay, _body(model=model, max_tokens=1), "acceptance_request_limit"
    )


def test_relay_rejects_missing_model_before_dispatch(offline_relay) -> None:
    _assert_rejected(
        offline_relay,
        json.dumps({"messages": [], "max_tokens": 1}).encode(),
        "acceptance_request_limit",
    )


def test_relay_rejects_missing_output_limit_before_dispatch(offline_relay) -> None:
    _assert_rejected(offline_relay, _body(), "acceptance_output_limit")


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize(
    "limit",
    [None, 0, -1, 8193, "1", "8192", 1.0, [], {}, True, False],
    ids=["null", "zero", "negative", "over-limit", "string-one", "string-max",
         "float", "array", "object", "true", "false"],
)
def test_relay_rejects_invalid_output_limits_before_dispatch(offline_relay, field, limit) -> None:
    _assert_rejected(offline_relay, _body(**{field: limit}), "acceptance_output_limit")


@pytest.mark.parametrize(
    ("max_tokens", "max_completion_tokens"),
    [(1, 8193), (8193, 1), (1, 2)],
)
def test_relay_rejects_conflicting_output_limits_before_dispatch(
    offline_relay, max_tokens, max_completion_tokens
) -> None:
    _assert_rejected(
        offline_relay,
        _body(max_tokens=max_tokens, max_completion_tokens=max_completion_tokens),
        "acceptance_output_limit",
    )


@pytest.mark.parametrize("limit", [1, 8192])
def test_relay_accepts_equal_valid_output_limits(offline_relay, limit) -> None:
    relay, request_log, sent = offline_relay
    body = _body(max_tokens=limit, max_completion_tokens=limit)

    _forward(relay, body)

    assert relay.calls == 1
    assert len(sent) == 1
    assert sent[0].content == body
    assert len(request_log.snapshot()["requests"]) == 1


@pytest.mark.parametrize(
    "completions",
    [None, 0, -1, 2, 8192, "1", 1.0, True, False],
    ids=["null", "zero", "negative", "two", "many", "string", "float", "true", "false"],
)
def test_relay_rejects_non_single_completion_before_dispatch(offline_relay, completions) -> None:
    _assert_rejected(
        offline_relay, _body(max_tokens=8192, n=completions), "acceptance_output_limit"
    )


def test_relay_allows_exactly_ninety_requests_then_rejects_without_counting(offline_relay) -> None:
    relay, request_log, sent = offline_relay
    body = _body(max_tokens=1)

    for expected_calls in range(1, 91):
        _forward(relay, body)
        assert relay.calls == expected_calls
        assert len(sent) == expected_calls

    rows = request_log.snapshot()["requests"]
    assert len(rows) == 90
    assert all(row["status"] == "completed" for row in rows)
    for _ in range(2):
        _assert_rejected(offline_relay, body, "acceptance_request_limit")


def _pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, "Week one: interfaces completed")
    document.showPage()
    document.drawString(72, 720, "Week two: staged release")
    document.showPage()
    document.save()
    return buffer.getvalue()


def test_validate_pdf_counts_pages_and_extracts_text() -> None:
    result = validate_bytes(_pdf_bytes(), "weekly-report.PDF")

    assert result["pages"] == 2
    assert "Week one: interfaces completed" in result["text"]
    assert "Week two: staged release" in result["text"]


def _pptx_bytes() -> bytes:
    document = Presentation()
    for title in ("Coffee shop goals", "Budget: equipment 20000"):
        slide = document.slides.add_slide(document.slide_layouts[0])
        slide.shapes.title.text = title
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_validate_pptx_counts_slides_and_extracts_text() -> None:
    result = validate_bytes(_pptx_bytes(), "opening-plan.PPTX")

    assert result["slides"] == 2
    assert "Coffee shop goals" in result["text"]
    assert "Budget: equipment 20000" in result["text"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("onboarding.md", "# 入职清单\n\n- [ ] 开通账号\n\n| 账号 | 负责人 |\n| --- | --- |\n"),
        ("index.html", "<!doctype html><html><body><h1>北京四季</h1></body></html>\n"),
        ("expenses.csv", "date,category,amount\n2026-09-01,咖啡,28\n"),
    ],
)
def test_validate_text_deliverables_preserves_utf8_content(name, content, encoding) -> None:
    assert validate_bytes(content.encode(encoding), name) == {"text": content}


@pytest.mark.parametrize("name", ["broken.md", "broken.html", "broken.csv"])
def test_validate_text_deliverables_rejects_invalid_utf8(name) -> None:
    with pytest.raises(UnicodeDecodeError):
        validate_bytes(b"\xff\xfeinvalid text", name)


@pytest.mark.parametrize("data", [b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_validate_pdf_rejects_corrupt_data(data) -> None:
    with pytest.raises(PdfReadError):
        validate_bytes(data, "broken.pdf")


@pytest.mark.parametrize("data", [b"not a presentation", b"PK\x03\x04truncated"])
def test_validate_pptx_rejects_corrupt_data(data) -> None:
    with pytest.raises(BadZipFile):
        validate_bytes(data, "broken.pptx")


def test_listed_artifacts_reads_all_pages_and_uses_visibility_not_source(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    session_id = "deliverable-session"
    session_key = "agent:main:webchat:deliverable-session"
    materials = [
        ("weekly-report.pdf", "application/pdf", _pdf_bytes(), "publish_artifact"),
        (
            "opening-plan.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx_bytes(),
            "workspace-preview",
        ),
        ("onboarding.md", "text/markdown", b"# Onboarding\n", "working_files"),
        ("expenses.csv", "text/csv", b"category,amount\ncoffee,28\n", "publish_artifact"),
    ]
    visible = [
        store.publish_bytes(
            data,
            session_id=session_id,
            session_key=session_key,
            name=name,
            mime=mime,
            source=source,
        )
        for name, mime, data, source in materials
    ]
    internal = [
        store.publish_bytes(
            b"<h1>Internal revision</h1>",
            session_id=session_id,
            session_key=session_key,
            name=f"{source}.html",
            mime="text/html",
            source=source,
            visibility="internal",
        )
        for source in ("workspace-preview", "working_files", "publish_artifact")
    ]
    other_session = store.publish_bytes(
        b"# Private to another session\n",
        session_id="other-session",
        session_key="agent:main:webchat:other-session",
        name="other.md",
        mime="text/markdown",
        source="publish_artifact",
    )

    first_page = store.list_refs(session_id=session_id, limit=2)
    assert len(first_page.refs) == 2
    assert first_page.has_more is True
    result = listed_artifacts(tmp_path, session_id, session_key, page_size=2)

    assert result == [
        ref.to_dict() for ref in sorted(visible, key=lambda ref: (ref.created_at, ref.id))
    ]
    assert {row["name"] for row in result} == {
        "weekly-report.pdf", "opening-plan.pptx", "onboarding.md", "expenses.csv"
    }
    assert {row["source"] for row in result} == {
        "publish_artifact", "workspace-preview", "working_files"
    }
    assert not {ref.id for ref in internal}.intersection(row["id"] for row in result)
    assert other_session.id not in {row["id"] for row in result}


def test_load_report_resume_preserves_cases_and_archives_first_failure(tmp_path: Path) -> None:
    cases = [
        {
            "id": "html-create",
            "session_key": "agent:main:webchat:html-session",
            "failures": [],
            "downloads": [],
        }
    ]
    rejection = {
        "case_id": "html-edit",
        "status": 409,
        "response": {"error": "request already running"},
    }
    original = {
        "model": MODEL,
        "cases": cases,
        "source": "/original/checkout",
        "source_commit": "a" * 40,
        "provider_calls": 12,
        "provider_probe_status": 200,
        "runner_error": "HTTP 409 while submitting html-edit",
        "http_rejection": rejection,
        "blocked": "gateway_rejected_request",
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    bytes_before = path.read_bytes()

    report = load_report(tmp_path, resume=True)

    assert report["cases"] == cases
    assert report["model"] == MODEL
    assert len(report["attempts"]) == 1
    archived = report["attempts"][0]
    assert archived["runner_error"] == original["runner_error"]
    assert archived["http_rejection"] == rejection
    assert archived["blocked"] == original["blocked"]
    assert archived["case_ids"] == ["html-create"]
    assert archived["source"] == original["source"]
    assert archived["source_commit"] == original["source_commit"]
    assert archived["provider_calls"] == 12
    assert archived["provider_probe_status"] == 200
    assert isinstance(archived["archived_at"], float)
    assert archived["archived_at"] > 0
    assert "runner_error" not in report
    assert "http_rejection" not in report
    assert "blocked" not in report
    assert path.read_bytes() == bytes_before

    # A later restart must retain the first failure, not overwrite its evidence.
    report["runner_error"] = "second attempt interrupted"
    report["provider_calls"] = 13
    path.write_text(json.dumps(report), encoding="utf-8")
    resumed_again = load_report(tmp_path, resume=True)

    assert resumed_again["cases"] == cases
    assert len(resumed_again["attempts"]) == 2
    assert resumed_again["attempts"][0] == archived
    assert resumed_again["attempts"][1]["runner_error"] == "second attempt interrupted"
    assert resumed_again["attempts"][1]["provider_calls"] == 13
    assert "http_rejection" not in resumed_again["attempts"][1]


def test_load_report_without_resume_rejects_existing_report_without_overwriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"model": MODEL, "cases": []}), encoding="utf-8")
    bytes_before = path.read_bytes()

    with pytest.raises(ValueError, match="^run already has a report; use --resume$"):
        load_report(tmp_path, resume=False)

    assert path.read_bytes() == bytes_before


def test_restored_sessions_recovers_groups_from_legacy_cases() -> None:
    report = {
        "cases": [
            {"id": "html-create", "session_key": "legacy-html"},
            {"id": "html-edit", "session_key": "legacy-html"},
            {"id": "html-multipage", "session_key": "legacy-multi"},
            {"id": "pdf-create", "session_key": "legacy-pdf"},
            {"id": "pptx-create", "session_key": "legacy-pptx"},
            {"id": "markdown-create", "session_key": "legacy-md"},
            {"id": "csv-create", "session_key": "legacy-csv"},
            {"id": "mixed-delivery", "session_key": "legacy-mixed"},
        ]
    }

    assert restored_sessions(report) == {
        "html": "legacy-html",
        "multi": "legacy-multi",
        "pdf": "legacy-pdf",
        "pptx": "legacy-pptx",
        "md": "legacy-md",
        "csv": "legacy-csv",
        "mixed": "legacy-mixed",
    }
    assert "sessions" not in report


def test_restored_sessions_preserves_saved_groups_and_pending_session() -> None:
    report = {
        "sessions": {"html": "old-html", "pdf": "saved-pdf"},
        "cases": [{"id": "html-create", "session_key": "completed-html"}],
        "pending_case": {
            "id": "html-edit",
            "session_key": "pending-html",
            "client_request_id": "original-request-id",
            "before": {"session": []},
        },
    }

    assert restored_sessions(report) == {"html": "pending-html", "pdf": "saved-pdf"}
    assert report["sessions"] == {"html": "old-html", "pdf": "saved-pdf"}
    assert report["pending_case"]["client_request_id"] == "original-request-id"


def test_load_report_resume_keeps_pending_case_for_session_recovery(tmp_path: Path) -> None:
    pending = {
        "id": "pdf-create",
        "session_key": "pending-pdf-session",
        "client_request_id": "existing-provider-request",
        "before": {"session": [], "files": []},
        "accepted": {"runId": "accepted-run"},
    }
    original = {"model": MODEL, "cases": [], "pending_case": pending}
    (tmp_path / "report.json").write_text(json.dumps(original), encoding="utf-8")

    report = load_report(tmp_path, resume=True)

    assert report["pending_case"] == pending
    assert restored_sessions(report) == {"pdf": "pending-pdf-session"}


@pytest.fixture
def forbid_live_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("offline content validation attempted live execution or credential access")

    monkeypatch.setattr(acceptance.httpx, "Client", forbidden)
    monkeypatch.setattr(acceptance.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(acceptance.getpass, "getpass", forbidden)
    monkeypatch.setattr(acceptance.subprocess, "Popen", forbidden)


def _content_case(case_id: str) -> dict:
    return {
        "id": case_id,
        "session_key": f"agent:main:webchat:{case_id}",
        "task": {"status": "succeeded"},
        "before": {"files": [], "sources": [], "artifacts": [], "publications": []},
        "after": {
            "workspace": None,
            "files": [],
            "sources": [],
            "artifacts": [],
            "publications": [],
        },
        "downloads": [],
        "failures": [],
    }


def _download_case(
    run_root: Path,
    case_id: str,
    name: str,
    data: bytes,
    *,
    parsed: dict | None = None,
) -> dict:
    path = run_root / "downloads" / case_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    case = _content_case(case_id)
    case["downloads"] = [{
        "name": name,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "parsed": parsed or {},
    }]
    return case


def _workspace_case(run_root: Path, case_id: str, files: dict[str, str]) -> dict:
    workspace = run_root / "workspace" / case_id
    case = _content_case(case_id)
    case["after"]["workspace"] = {"root": str(workspace)}
    for name, text in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        case["after"]["files"].append({
            "path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()
        })
    return case


def _check(result: dict, name: str) -> dict:
    matches = [check for check in result["checks"] if check["name"] == name]
    assert len(matches) == 1, f"Expected exactly one {name!r} check, got {result['checks']!r}"
    return matches[0]


def _chinese_pdf_bytes(*pages: str) -> bytes:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    for text in pages:
        document.setFont("STSong-Light", 14)
        document.drawString(40, 720, text)
        document.showPage()
    document.save()
    return buffer.getvalue()


@pytest.mark.parametrize(("case_id", "release"), [("pdf-create", "上线"), ("pdf-edit", "灰度发布")])
def test_report_validation_rechecks_pdf_bytes_instead_of_cached_page_count(
    tmp_path: Path, forbid_live_validation, case_id, release
) -> None:
    content = f"海风计划 本周完成 接口联调 首页开发 下周计划 测试 {release}"
    data = _chinese_pdf_bytes(content)
    case = _download_case(
        tmp_path, case_id, "weekly-report.pdf", data, parsed={"pages": 2, "text": content}
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"model": MODEL, "cases": [case]}, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    before = report_path.read_bytes()
    mtime_before = report_path.stat().st_mtime_ns

    result = acceptance.validate_report(report_path)

    assert result["source_report"] == str(report_path)
    assert result["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert result["status"] == "failed"
    assert result["summary"] == {"passed": 0, "failed": 1, "inconclusive": 0}
    assert len(result["cases"]) == 1
    validated_case = result["cases"][0]
    assert validated_case["id"] == case_id
    assert validated_case["status"] == "failed"
    page_count = _check(validated_case, "pdf_page_count")
    assert page_count["status"] == "failed"
    assert page_count["expected"] == 2
    assert page_count["actual"] == 1
    assert report_path.read_bytes() == before
    assert report_path.stat().st_mtime_ns == mtime_before
    assert Path(case["downloads"][0]["path"]).read_bytes() == data


@pytest.mark.parametrize(("case_id", "release"), [("pdf-create", "上线"), ("pdf-edit", "灰度发布")])
def test_pdf_content_accepts_two_pages_with_the_requested_plan(
    tmp_path: Path, forbid_live_validation, case_id, release
) -> None:
    data = _chinese_pdf_bytes(
        "海风计划 本周完成 接口联调 首页开发", f"海风计划 下周计划 测试 {release}"
    )
    case = _download_case(tmp_path, case_id, "weekly-report.pdf", data)

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "passed"
    assert _check(result, "pdf_page_count")["status"] == "passed"


def test_pdf_edit_rejects_unchanged_release_plan(
    tmp_path: Path, forbid_live_validation
) -> None:
    data = _chinese_pdf_bytes(
        "海风计划 本周完成 接口联调 首页开发", "海风计划 下周计划 测试 上线"
    )
    case = _download_case(tmp_path, "pdf-edit", "weekly-report.pdf", data)

    result = acceptance.validate_case_content(case, tmp_path)

    assert _check(result, "pdf_page_count")["status"] == "passed"
    assert result["status"] == "failed"


_EXPECTED_CSV = (
    "date,category,amount\n"
    "2026-09-01,coffee,28\n"
    "2026-09-02,lunch,35\n"
    "2026-09-03,transport,12\n"
)


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [(_EXPECTED_CSV, "passed"), (_EXPECTED_CSV.replace("coffee,28", "coffee,29"), "failed")],
    ids=["exact-requested-rows", "wrong-amount"],
)
def test_csv_content_checks_actual_rows(
    tmp_path: Path, forbid_live_validation, content, expected_status
) -> None:
    case = _download_case(
        tmp_path, "csv-create", "expenses.csv", content.encode(), parsed={"text": _EXPECTED_CSV}
    )

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == expected_status
    assert _check(result, "csv_rows")["status"] == expected_status


_EXPECTED_MARKDOWN = (
    "# 新同事入职清单\n\n"
    "- [ ] 开通账号\n"
    "- [ ] 配置电脑\n"
    "- [ ] 加入团队\n\n"
    "| 账号 | 负责人 |\n"
    "| --- | --- |\n"
    "| 邮箱 | 王同事 |\n"
)


@pytest.mark.parametrize(
    ("content", "checkbox_status", "table_status"),
    [
        (_EXPECTED_MARKDOWN, "passed", "passed"),
        (_EXPECTED_MARKDOWN.replace("- [ ] 加入团队\n", ""), "failed", "passed"),
        (_EXPECTED_MARKDOWN.split("| 账号")[0], "passed", "failed"),
        (
            _EXPECTED_MARKDOWN.replace("| 负责人 |", "| 负责人 | 状态 |")
            .replace("| --- | --- |", "| --- | --- | --- |")
            .replace("| 王同事 |", "| 王同事 | 待办理 |"),
            "passed",
            "failed",
        ),
    ],
    ids=["complete-checklist", "only-two-checkboxes", "missing-table", "three-column-table"],
)
def test_markdown_content_requires_three_checkboxes_and_two_column_table(
    tmp_path: Path, forbid_live_validation, content, checkbox_status, table_status
) -> None:
    case = _download_case(
        tmp_path,
        "markdown-create",
        "onboarding.md",
        content.encode("utf-8-sig"),
        parsed={"text": _EXPECTED_MARKDOWN},
    )

    result = acceptance.validate_case_content(case, tmp_path)

    assert _check(result, "markdown_checkboxes")["status"] == checkbox_status
    assert _check(result, "markdown_table")["status"] == table_status
    expected = "passed" if checkbox_status == table_status == "passed" else "failed"
    assert result["status"] == expected


def _cafe_files() -> dict[str, str]:
    pages = [
        ("welcome.html", "首页", "欢迎来到咖啡店"),
        ("drinks.html", "菜单", "拿铁 28 元，美式 22 元"),
        ("reach-us.html", "联系方式", "地址：城市中心 电话：010-12345678"),
    ]
    navigation = "".join(f'<a href="{name}">{title}</a>' for name, title, _ in pages)
    return {
        **{
            f"cafe/{name}": (
                f"<!doctype html><html><head><title>{title}</title>"
                '<link rel="stylesheet" href="assets/common.css"></head>'
                f"<body><nav>{navigation}</nav><h1>{title}</h1><p>{content}</p></body></html>"
            )
            for name, title, content in pages
        },
        "cafe/assets/common.css": "body { color: #321; background: #fff; }\n",
    }


def test_html_multipage_accepts_arbitrary_filenames_with_links_and_shared_css(
    tmp_path: Path, forbid_live_validation
) -> None:
    case = _workspace_case(tmp_path, "html-multipage", _cafe_files())

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "passed"
    assert _check(result, "html_page_links")["status"] == "passed"
    assert _check(result, "html_shared_css")["status"] == "passed"


def test_html_multipage_accepts_branded_homepage_heading(
    tmp_path: Path, forbid_live_validation
) -> None:
    files = _cafe_files()
    files["cafe/welcome.html"] = files["cafe/welcome.html"].replace(
        "<title>首页</title>", "<title>海风咖啡</title>"
    ).replace("<h1>首页</h1>", "<h1>海风咖啡</h1>")
    case = _workspace_case(tmp_path, "html-multipage", files)

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "passed"
    assert _check(result, "html_page_links")["status"] == "passed"


def test_html_multipage_rejects_missing_cross_page_link(
    tmp_path: Path, forbid_live_validation
) -> None:
    files = _cafe_files()
    files["cafe/welcome.html"] = files["cafe/welcome.html"].replace(
        '<a href="reach-us.html">联系方式</a>', ""
    )
    case = _workspace_case(tmp_path, "html-multipage", files)

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "failed"
    assert _check(result, "html_page_links")["status"] == "failed"
    assert _check(result, "html_shared_css")["status"] == "passed"


def test_html_multipage_rejects_missing_css_dependency(
    tmp_path: Path, forbid_live_validation
) -> None:
    files = _cafe_files()
    files.pop("cafe/assets/common.css")
    case = _workspace_case(tmp_path, "html-multipage", files)

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "failed"
    assert _check(result, "html_shared_css")["status"] == "failed"


def test_changed_historical_html_snapshot_is_inconclusive_instead_of_wrong_title(
    tmp_path: Path, forbid_live_validation
) -> None:
    original = (
        "<!doctype html><html><head><title>北京四季</title></head>"
        "<body><h1>北京四季</h1><p>春季 夏季 秋季 冬季</p></body></html>"
    )
    case = _workspace_case(tmp_path, "html-create", {"beijing-site/index.html": original})
    current = original.replace("北京四季", "北京四季出行备忘")
    source_path = Path(case["after"]["workspace"]["root"]) / "beijing-site/index.html"
    source_path.write_text(current, encoding="utf-8")

    result = acceptance.validate_case_content(case, tmp_path)

    assert result["status"] == "inconclusive"
    assert _check(result, "source_snapshot_available")["status"] == "inconclusive"
    assert not [check for check in result["checks"] if check["status"] == "failed"]
    assert source_path.read_text(encoding="utf-8") == current
