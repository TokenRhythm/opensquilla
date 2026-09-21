"""Persistent, server-authoritative state for local Knowledge research."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

if __package__:
    from .claims import (
        ResearchStateError,
        apply_claim_batch,
        canonical_json,
        invalidate_finalized,
        sha256_json,
    )
    from .references import _clean_title as clean_source_title
    from .references import build_bibliography, cited_file_ids
    from .report import render_html_report
    from .review import review_preparation, review_requirements, table_item_hash
    from .writing_preparation import require_first_write_preparation, research_depth_summary
else:  # pragma: no cover - exercised by deployment entrypoint smoke tests
    from claims import (  # type: ignore[import-not-found,no-redef]
        ResearchStateError,
        apply_claim_batch,
        canonical_json,
        invalidate_finalized,
        sha256_json,
    )
    from references import (  # type: ignore[import-not-found,no-redef]
        _clean_title as clean_source_title,
    )
    from references import (  # type: ignore[import-not-found,no-redef]
        build_bibliography,
        cited_file_ids,
    )
    from report import render_html_report  # type: ignore[import-not-found,no-redef]
    from review import (  # type: ignore[import-not-found,no-redef]
        review_preparation,
        review_requirements,
        table_item_hash,
    )
    from writing_preparation import (  # type: ignore[import-not-found,no-redef]
        require_first_write_preparation,
        research_depth_summary,
    )

STATE_SCHEMA_VERSION = "opensquilla-knowledge-research-state/1"
LEDGER_SCHEMA_VERSION = "opensquilla-knowledge-evidence-ledger/1"
PROVENANCE_SCHEMA_VERSION = "opensquilla-knowledge-provenance/1"
PUBLIC_MANIFEST_VERSION = "opensquilla-public-artifact-manifest/1"

_RESEARCH_ID = re.compile(r"^kr_[0-9a-f]{32}$")
_NAVIGATION_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_INTERNAL_ID_HINT = re.compile(
    r"(?:kref_[0-9a-f]{8,32}_[a-z]+[0-9]+|"
    r"[A-Za-z0-9_-]{22}:(?:[DETC][1-9][0-9]*|[SN][0-9a-f]{24})|"
    r"\bev(?:idence)?\d*_[0-9a-f]{8,}\b|\b(?:tbl\d*|t\d+)_[0-9a-z]{8,}\b|"
    r"\bkr_[0-9a-f]{32}\b|\b(?:researchId|fileId|evidenceId|tableId)\b)",
    re.IGNORECASE,
)
_KNOWLEDGE_TOOLS = frozenset({"search", "searchByIds", "getFileDetails", "getTable"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_EXPECTED_CONTRACT_VERSION = "knowledge-vnext/2"
_EXPECTED_CHUNK_POLICY_ID = "hierarchical_token_v4"
_EXPECTED_INDEX_VERSION = "knowledge-index-v5"
_EXPECTED_RETRIEVAL_PROFILE = "hybrid_rrf_bge_m3_fts5"


def _string(value: Any, *, name: str, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchStateError(f"{name} must be a non-empty string")
    clean = value.strip()
    if len(clean) > maximum:
        raise ResearchStateError(f"{name} exceeds {maximum} characters")
    return clean


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_json[JsonValue](value: JsonValue) -> JsonValue:
    return cast(JsonValue, json.loads(canonical_json(value)))


def _decode_image(content: Sequence[Any]) -> tuple[bytes, str] | None:
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "image":
            continue
        data = block.get("data")
        mime = block.get("mimeType")
        if not isinstance(data, str) or not isinstance(mime, str):
            continue
        try:
            return base64.b64decode(data, validate=True), mime
        except (ValueError, TypeError):
            return None
    return None


def recover_structured_content(
    result: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Recover the duplicated MCP payload without trusting arbitrary prose."""

    structured = _mapping(result.get("structuredContent"))
    if structured is not None:
        return structured, "structuredContent"
    for block in _list(result.get("content")):
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            return None, None
        try:
            recovered = json.loads(text)
        except json.JSONDecodeError:
            return None, None
        return (_mapping(recovered), "content[0].text")
    return None, None


class KnowledgeResearchStore:
    """Atomic JSON state store keyed by an opaque research ID."""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike[str],
        private_root: str | os.PathLike[str] | None = None,
        media_root: str | os.PathLike[str] | None = None,
        pdf_renderer: Callable[[str, Path], bytes] | None = None,
        reading_coverage_resolver: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.private_root = (
            Path(private_root).expanduser().resolve()
            if private_root is not None
            else self.workspace / ".codex" / "knowledge-research"
        )
        self.media_root = (
            Path(media_root).expanduser().resolve() if media_root is not None else None
        )
        self.output_root = self.workspace / "knowledge-reports"
        self.pdf_renderer = pdf_renderer or _render_pdf
        self.reading_coverage_resolver = reading_coverage_resolver

    def begin(
        self,
        *,
        title: str,
        subtitle: str | None = None,
        mode: str = "standard",
        language: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(mode, str) or mode not in {"standard", "deep"}:
            raise ResearchStateError("mode must be standard or deep")
        if language is not None and (
            not isinstance(language, str) or language not in {"zh-CN", "en"}
        ):
            raise ResearchStateError("language must be zh-CN or en")
        clean_title = _string(title, name="title", maximum=300)
        clean_subtitle = (
            _string(subtitle, name="subtitle", maximum=500) if subtitle is not None else None
        )
        self._reject_internal_ids(clean_title, clean_subtitle or "")
        while True:
            research_id = f"kr_{secrets.token_hex(16)}"
            try:
                self._state_path(research_id).parent.mkdir(parents=True, mode=0o700)
                break
            except FileExistsError:
                continue
        state: dict[str, Any] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "researchId": research_id,
            "title": clean_title,
            "subtitle": clean_subtitle,
            "mode": mode,
            "language": language,
            "ledger": {
                "schemaVersion": LEDGER_SCHEMA_VERSION,
                "calls": [],
                "evidence": {},
                "files": {},
                "inventories": {},
                "tables": {},
            },
            "report": {"items": []},
            "finalized": None,
            "stateRevision": 0,
        }
        with self._research_lock(research_id):
            self._save(state)
        return {
            "researchId": research_id,
            "status": "ready",
            "verificationStatus": "server_authoritative",
            "mode": mode,
            "workflow": (
                "Discover, search within candidate files, draft small batches, evaluate the "
                "draft via researchNavigate view=review, apply the returned optimization "
                "actions, repeat evaluation until it passes, then finalize."
                if mode == "deep"
                else "Use relevant evidence and disclose limitations."
            ),
        }

    def exists(self, research_id: str) -> bool:
        return self._state_path(research_id).is_file()

    def snapshot(self, research_id: str) -> dict[str, Any]:
        with self._research_lock(research_id, shared=True):
            return _safe_json(self._load(research_id))

    def atomic_update[T](self, research_id: str, mutator: Callable[[dict[str, Any]], T]) -> T:
        """Serialize one research's read/modify/write, including callback changes.

        Mutators must not perform network I/O, nest transactions, or save state.
        A failure after file replacement may have committed; replay by batchKey.
        """

        with self._research_lock(research_id):
            state = self._load(research_id)
            before = canonical_json(state)
            revision = int(state.get("stateRevision", 0))
            result = mutator(state)
            if (
                state.get("researchId") != research_id
                or state.get("schemaVersion") != STATE_SCHEMA_VERSION
            ):
                raise ResearchStateError("a transaction cannot change research identity or schema")
            if canonical_json(state) != before:
                state["stateRevision"] = revision + 1
                self._save(state)
            return result

    def record_knowledge_call(
        self,
        *,
        research_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        metadata_only: bool = False,
        on_commit: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            return self._record_knowledge_call(
                state,
                research_id=research_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                metadata_only=metadata_only,
                on_commit=on_commit,
            )

        return self.atomic_update(research_id, update)

    def _record_knowledge_call(
        self,
        state: dict[str, Any],
        *,
        research_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        metadata_only: bool,
        on_commit: Callable[[dict[str, Any], dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if tool_name not in _KNOWLEDGE_TOOLS:
            raise ResearchStateError(f"unsupported Knowledge tool: {tool_name}")
        ledger = state["ledger"]
        structured, structured_source = recover_structured_content(result)
        if bool(result.get("isError")):
            status = "upstream_error"
            accepted = 0
        elif structured is None:
            status = "unverified_missing_structured_content"
            accepted = 0
        elif metadata_only and tool_name == "getFileDetails":
            scratch = {"files": dict(ledger["files"]), "inventories": {}}
            status, accepted = self._ingest_file_details(scratch, arguments, structured)
            if status == "verified":
                file_id = arguments["fileId"]
                ledger["files"][file_id] = scratch["files"][file_id]
                accepted = 1
        else:
            status, accepted = self._ingest(
                research_id=research_id,
                ledger=ledger,
                tool_name=tool_name,
                arguments=arguments,
                structured=structured,
                content=_list(result.get("content")),
            )
        call = {
            "sequence": len(ledger["calls"]) + 1,
            "toolName": tool_name,
            "arguments": _safe_json(dict(arguments)),
            "resultSha256": sha256_json(result),
            "verificationStatus": status,
            "acceptedRecordCount": accepted,
            "structuredSource": structured_source,
        }
        if metadata_only:
            call["purpose"] = "bibliography_metadata"
        if tool_name in {"search", "searchByIds"} and structured is not None:
            call["retrieval"] = self._retrieval_telemetry(structured)
        ledger["calls"].append(call)
        if on_commit is not None:
            on_commit(state, call)
        return _safe_json(call)

    def record_knowledge_error(
        self,
        *,
        research_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        error: Mapping[str, Any],
        on_commit: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Record an actual upstream JSON-RPC failure without accepting evidence."""

        if tool_name not in _KNOWLEDGE_TOOLS:
            raise ResearchStateError(f"unsupported Knowledge tool: {tool_name}")

        def update(state: dict[str, Any]) -> dict[str, Any]:
            ledger = state["ledger"]
            call = {
                "sequence": len(ledger["calls"]) + 1,
                "toolName": tool_name,
                "arguments": _safe_json(dict(arguments)),
                "resultSha256": sha256_json({"error": error}),
                "verificationStatus": "upstream_rpc_error",
                "acceptedRecordCount": 0,
            }
            ledger["calls"].append(call)
            if on_commit is not None:
                on_commit(state, call)
            return _safe_json(call)

        return self.atomic_update(research_id, update)

    def add_claim(
        self,
        *,
        research_id: str,
        section: str,
        text: str,
        evidence_ids: Sequence[str],
        batch_key: str | None = None,
        claim_key: str | None = None,
        expected_claim_hash: str | None = None,
    ) -> dict[str, Any]:
        claim: dict[str, Any] = {"section": section, "text": text, "evidenceIds": evidence_ids}
        if claim_key is not None:
            claim["claimKey"] = claim_key
        if expected_claim_hash is not None:
            claim["expectedClaimHash"] = expected_claim_hash
        result = self.add_claims(research_id=research_id, claims=[claim], batch_key=batch_key)
        return {**result, "item": result["items"][0]}

    def add_claims(
        self,
        *,
        research_id: str,
        claims: Sequence[Mapping[str, Any]],
        batch_key: str | None = None,
    ) -> dict[str, Any]:
        return self.atomic_update(
            research_id,
            lambda state: apply_claim_batch(
                state,
                claims,
                batch_key=batch_key,
                reject_text=lambda text: self._reject_internal_ids(text, state=state),
                before_commit=lambda prepared: require_first_write_preparation(state, prepared),
            ),
        )

    def add_table(
        self,
        *,
        research_id: str,
        section: str,
        table_id: str,
        caption: str,
        expected_table_hash: str | None = None,
    ) -> dict[str, Any]:
        return self.atomic_update(
            research_id,
            lambda state: self._add_table(
                state,
                section=section,
                table_id=table_id,
                caption=caption,
                expected_table_hash=expected_table_hash,
            ),
        )

    def _add_table(
        self,
        state: dict[str, Any],
        *,
        section: str,
        table_id: str,
        caption: str,
        expected_table_hash: str | None = None,
    ) -> dict[str, Any]:
        clean_section = _string(section, name="section", maximum=200)
        clean_caption = _string(caption, name="caption", maximum=1_000)
        self._reject_internal_ids(clean_section, clean_caption, state=state)
        if expected_table_hash is not None and (
            not isinstance(expected_table_hash, str)
            or _SHA256.fullmatch(expected_table_hash) is None
        ):
            raise ResearchStateError("expectedTableHash must be a SHA256 digest")
        for existing in state["report"]["items"]:
            if existing.get("kind") != "table" or existing.get("tableId") != table_id:
                continue
            issues = [
                {"code": "TABLE_ITEM_CONFLICT", "path": f"/{field}", "itemId": existing["itemId"]}
                for field, value in (("section", clean_section), ("caption", clean_caption))
                if existing.get(field) != value
            ]
            if issues and expected_table_hash != table_item_hash(existing):
                raise ResearchStateError(
                    "tableId is already included with different section or caption",
                    details={
                        "code": "TABLE_ITEM_CONFLICT",
                        "committed": False,
                        "tableId": table_id,
                        "currentTableHash": table_item_hash(existing),
                        "issues": issues,
                    },
                )
            if issues:
                existing.update(section=clean_section, caption=clean_caption)
                state["report"]["revision"] = int(state["report"].get("revision", 0)) + 1
                invalidate_finalized(state)
            return {
                "status": "accepted",
                "item": existing["itemId"],
                "tableCount": 1,
            }
        if expected_table_hash is not None:
            raise ResearchStateError("table has not been added; no caption exists to revise")
        table = state["ledger"]["tables"].get(table_id)
        if not isinstance(table, Mapping) or table.get("verificationStatus") != "verified":
            raise ResearchStateError("table was not verified by an actual getTable response")
        file_id = str(table.get("fileId") or "")
        inventory = state["ledger"]["inventories"].get(file_id)
        if not isinstance(inventory, Mapping) or not inventory.get("complete"):
            raise ResearchStateError("table inventory is incomplete; run getFileDetails first")
        if table_id not in inventory.get("tableIds", []):
            raise ResearchStateError("table is not present in the verified file inventory")
        if file_id not in state["ledger"]["files"]:
            raise ResearchStateError("table source metadata is unavailable")
        item = {
            "kind": "table",
            "itemId": f"table-{len(state['report']['items']) + 1:04d}",
            "section": clean_section,
            "caption": clean_caption,
            "tableId": table_id,
        }
        state["report"]["items"].append(item)
        state["report"]["revision"] = int(state["report"].get("revision", 0)) + 1
        invalidate_finalized(state)
        return {
            "status": "accepted",
            "item": item["itemId"],
            "tableCount": 1,
        }

    def pending_review(self, research_id: str) -> dict[str, Any] | None:
        return review_requirements(self.snapshot(research_id))

    def finalize(
        self,
        *,
        research_id: str,
        expected_claim_keys: Sequence[str] | None = None,
        expected_table_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Serialize local rendering with writes to this research, not other research."""

        return self.atomic_update(
            research_id,
            lambda state: self._finalize(
                state,
                expected_claim_keys=expected_claim_keys,
                expected_table_ids=expected_table_ids,
            ),
        )

    def _finalize(
        self,
        state: dict[str, Any],
        *,
        expected_claim_keys: Sequence[str] | None,
        expected_table_ids: Sequence[str] | None,
    ) -> dict[str, Any]:
        research_id = state["researchId"]
        items = state["report"]["items"]
        if not items or not any(item.get("kind") == "claim" for item in items):
            raise ResearchStateError("report requires at least one cited claim")
        self._check_expected_items(items, "claim", "claimKey", expected_claim_keys)
        self._check_expected_items(items, "table", "tableId", expected_table_ids)
        for item in items:
            if item.get("kind") == "claim":
                self._verified_evidence_ids(state, item["evidenceIds"])
        preparation = review_preparation(state)
        pending = review_requirements(state)
        if pending is not None:
            return pending
        rendered_state = state
        inputs = {
            name: state.get(name)
            for name in ("title", "subtitle", "ledger", "report", "language", "mode")
            if name in state
        }
        if self.reading_coverage_resolver is not None:
            reading_coverage = self.reading_coverage_resolver(state)
            rendered_state = {**state, "readingCoverage": reading_coverage}
            inputs["readingCoverage"] = reading_coverage
        input_hash = sha256_json(inputs)
        previous = state.get("finalized")
        if isinstance(previous, Mapping) and previous.get("inputSha256") == input_hash:
            self._check_artifacts(previous["files"])
            receipt: dict[str, Any] = _safe_json(previous["receipt"])
            receipt["review"] = preparation
            return receipt
        html = render_html_report(self._state_for_render(rendered_state))
        self._reject_report_leaks(html, state)
        pdf = self.pdf_renderer(html, self.workspace)
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF"):
            raise ResearchStateError("PDF renderer did not return a PDF")

        html_bytes = html.encode("utf-8")
        provenance = self._provenance(rendered_state, html_bytes=html_bytes, pdf_bytes=pdf)
        provenance_bytes = (
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self.output_root.resolve().relative_to(self.workspace)
        self.output_root.mkdir(parents=True, exist_ok=True)
        output_dir = self.output_root / research_id
        while output_dir.exists() or output_dir.is_symlink():
            output_dir = self.output_root / f"{research_id}-{secrets.token_hex(8)}"
        outputs = {
            "report.html": (html_bytes, "text/html"),
            "report.pdf": (pdf, "application/pdf"),
            "provenance.json": (provenance_bytes, "application/json"),
        }
        files: list[dict[str, Any]] = []
        # Only this attempt's temporary directory is disposable. A promoted
        # directory survives a failed state save and is never overwritten.
        with tempfile.TemporaryDirectory(
            prefix=f".{research_id}.staging-", dir=self.output_root
        ) as temporary:
            staging = Path(temporary)
            for name, (payload, mime) in outputs.items():
                self._atomic_write(staging / name, payload)
                files.append(
                    {
                        "path": (output_dir / name).relative_to(self.workspace).as_posix(),
                        "name": name,
                        "mime": mime,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "bundle": "none",
                    }
                )
            self._fsync_directory(staging)
            staging.rename(output_dir)
            self._fsync_directory(self.output_root)
        coverage = self._report_coverage(state)
        receipt = {
            "status": "finalized",
            "coverage": coverage,
            "review": preparation,
            "publicArtifactManifest": {
                "schemaVersion": PUBLIC_MANIFEST_VERSION,
                "files": files,
            },
            "note": (
                "Publish exactly these three files with bundle=none. Do not publish "
                "the report directory or any private research state."
            ),
        }
        invalidate_finalized(state)
        state["finalized"] = {
            "manifestVersion": PUBLIC_MANIFEST_VERSION,
            "files": files,
            "inputSha256": input_hash,
            "receipt": receipt,
        }
        state.setdefault("artifactHistory", []).append(_safe_json(state["finalized"]))
        return _safe_json(receipt)

    @staticmethod
    def _check_expected_items(
        items: Sequence[Mapping[str, Any]],
        kind: str,
        field: str,
        expected: Sequence[str] | None,
    ) -> None:
        if expected is None:
            return
        if (
            isinstance(expected, str | bytes)
            or not isinstance(expected, Sequence)
            or any(not isinstance(value, str) or not value for value in expected)
            or len(set(expected)) != len(expected)
        ):
            raise ResearchStateError("expected item keys must be an array of distinct strings")
        actual = [item.get(field) for item in items if item.get("kind") == kind]
        if None in actual or set(actual) != set(expected):
            raise ResearchStateError(
                "expected items do not match the submitted report",
                details={
                    "code": "EXPECTED_ITEMS_MISMATCH",
                    "committed": False,
                    "kind": kind,
                    "missing": sorted(set(expected) - set(actual)),
                    "unexpected": sorted(
                        value for value in set(actual) - set(expected) if value is not None
                    ),
                    "unkeyedCount": actual.count(None),
                },
            )

    def _check_artifacts(self, files: Sequence[Mapping[str, Any]]) -> None:
        if {item.get("name") for item in files} != {
            "report.html",
            "report.pdf",
            "provenance.json",
        } or len(files) != 3:
            raise ResearchStateError("finalized manifest is invalid")
        for item in files:
            target = (self.workspace / str(item["path"])).resolve()
            target.relative_to(self.output_root.resolve())
            if (
                not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise ResearchStateError(
                    "finalized artifact is missing or changed; preserved without overwriting"
                )

    def missing_reference_metadata(self, research_id: str) -> list[str]:
        state = self.snapshot(research_id)
        files = state["ledger"]["files"]
        return [
            file_id
            for file_id in cited_file_ids(state)
            if files[file_id].get("metadataSource") != "getFileDetails"
        ]

    def _ingest(
        self,
        research_id: str,
        ledger: dict[str, Any],
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
        content: list[Any],
    ) -> tuple[str, int]:
        if tool_name in {"search", "searchByIds"}:
            return self._ingest_search(
                ledger,
                tool_name=tool_name,
                arguments=arguments,
                structured=structured,
            )
        if tool_name == "getFileDetails":
            return self._ingest_file_details(ledger, arguments, structured)
        return self._ingest_table(
            research_id,
            ledger,
            arguments,
            structured,
            content,
        )

    @staticmethod
    def _ingest_search(
        ledger: dict[str, Any],
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
    ) -> tuple[str, int]:
        expected_strategy = (
            "pure_score" if tool_name == "searchByIds" else "hierarchical_interleave"
        )
        if (
            structured.get("contractVersion") != _EXPECTED_CONTRACT_VERSION
            or structured.get("chunkPolicyId") != _EXPECTED_CHUNK_POLICY_ID
            or structured.get("indexVersion") != _EXPECTED_INDEX_VERSION
            or structured.get("effectiveProfile") != _EXPECTED_RETRIEVAL_PROFILE
            or structured.get("retrievalProfile") != _EXPECTED_RETRIEVAL_PROFILE
            or structured.get("selectionStrategy") != expected_strategy
            or structured.get("scopeEnforced") is not True
            or structured.get("selectionSource") == "fallback"
            or structured.get("fallbackReason") not in {None, ""}
            or structured.get("warnings") != []
        ):
            return "unverified_retrieval_contract", 0
        results = structured.get("results")
        if not isinstance(results, list):
            return "unverified_invalid_payload", 0
        result_count = structured.get("count")
        if (
            not isinstance(result_count, int)
            or isinstance(result_count, bool)
            or result_count != len(results)
        ):
            return "unverified_invalid_payload", 0
        for count_name in ("lexicalCandidateCount", "vectorCandidateCount"):
            candidate_count = structured.get(count_name)
            if (
                not isinstance(candidate_count, int)
                or isinstance(candidate_count, bool)
                or candidate_count < 0
            ):
                return "unverified_invalid_payload", 0
        requested_file_ids: set[str] | None = None
        if tool_name == "searchByIds":
            raw_file_ids = arguments.get("fileIds")
            if not isinstance(raw_file_ids, list) or not raw_file_ids:
                return "unverified_invalid_scope", 0
            if not all(isinstance(value, str) and value for value in raw_file_ids):
                return "unverified_invalid_scope", 0
            requested_file_ids = set(raw_file_ids)
            if structured.get("scopeEnforced") is not True:
                return "unverified_scope_not_enforced", 0
        elif "collectionIds" in arguments and structured.get("scopeEnforced") is not True:
            return "unverified_scope_not_enforced", 0

        pending: dict[str, dict[str, Any]] = {}
        pending_files: dict[str, dict[str, Any]] = {}
        for raw in results:
            item = _mapping(raw)
            if item is None:
                return "unverified_invalid_payload", 0
            evidence_id = item.get("evidenceId")
            file_id = item.get("fileId")
            document_id = item.get("documentId")
            chunk_id = item.get("chunkId")
            revision = item.get("revision")
            content = item.get("content")
            if not all(
                isinstance(value, str) and value
                for value in (
                    evidence_id,
                    file_id,
                    document_id,
                    chunk_id,
                    revision,
                    content,
                )
            ):
                return "unverified_invalid_payload", 0
            assert isinstance(evidence_id, str)
            assert isinstance(file_id, str)
            assert isinstance(document_id, str)
            assert isinstance(chunk_id, str)
            assert isinstance(revision, str)
            assert isinstance(content, str)
            if requested_file_ids is not None and file_id not in requested_file_ids:
                return "unverified_scope_violation", 0
            locator = _mapping(item.get("locator"))
            if locator is None:
                return "unverified_invalid_payload", 0
            title = item.get("title")
            title = title if isinstance(title, str) and title.strip() else "Untitled local document"
            record = {
                "evidenceId": evidence_id,
                "fileId": file_id,
                "documentId": document_id,
                "chunkId": chunk_id,
                "revision": revision,
                "title": title,
                "locator": _safe_json(locator),
                "content": content,
                "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "contentKind": item.get("contentKind"),
                "parentChunkId": item.get("parentChunkId"),
                "previousChunkId": item.get("previousChunkId"),
                "nextChunkId": item.get("nextChunkId"),
                "verificationStatus": "verified",
            }
            existing = ledger["evidence"].get(evidence_id)
            if existing is not None and sha256_json(existing) != sha256_json(record):
                return "unverified_collision", 0
            duplicate = pending.get(evidence_id)
            if duplicate is not None and sha256_json(duplicate) != sha256_json(record):
                return "unverified_collision", 0
            pending[evidence_id] = record

            current_file = pending_files.get(file_id) or ledger["files"].get(file_id)
            merged_file: dict[str, Any]
            if current_file is None:
                merged_file = {
                    "fileId": file_id,
                    "documentId": document_id,
                    "revision": revision,
                    "title": title,
                    "filename": None,
                    "sourcePath": None,
                    "mediaType": None,
                    "observedLocators": [],
                    "metadataSource": "search",
                    "verificationStatus": "verified",
                }
            else:
                if current_file.get("documentId") not in {None, document_id}:
                    return "unverified_document_mismatch", 0
                if current_file.get("revision") not in {None, revision}:
                    return "unverified_revision_mismatch", 0
                merged_file = dict(_safe_json(current_file))
                if merged_file.get("title") == "Untitled local document":
                    merged_file["title"] = title
            observed_locator = _safe_json(locator)
            observed_locators = merged_file.get("observedLocators")
            if not isinstance(observed_locators, list):
                return "unverified_invalid_file_record", 0
            if observed_locator not in observed_locators:
                observed_locators.append(observed_locator)
            pending_files[file_id] = merged_file
        ledger["evidence"].update(pending)
        ledger["files"].update(pending_files)
        return "verified", len(pending)

    @staticmethod
    def _ingest_file_details(
        ledger: dict[str, Any],
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
    ) -> tuple[str, int]:
        if structured.get("contractVersion") != _EXPECTED_CONTRACT_VERSION:
            return "unverified_contract_version", 0
        file_payload = _mapping(structured.get("file"))
        tables = structured.get("tables")
        requested_file = arguments.get("fileId")
        if file_payload is None or not isinstance(tables, list):
            return "unverified_invalid_payload", 0
        file_id = file_payload.get("fileId")
        if not isinstance(file_id, str) or not file_id or file_id != requested_file:
            return "unverified_identity_mismatch", 0
        document_id = file_payload.get("documentId")
        revision = file_payload.get("revision")
        if not isinstance(document_id, str) or not document_id:
            return "unverified_invalid_payload", 0
        if not isinstance(revision, str) or not revision:
            return "unverified_invalid_payload", 0
        existing_file = ledger["files"].get(file_id)
        if isinstance(existing_file, Mapping):
            if existing_file.get("documentId") not in {None, document_id}:
                return "unverified_document_mismatch", 0
            if existing_file.get("revision") not in {None, revision}:
                return "unverified_revision_mismatch", 0
            observed_locators = _safe_json(existing_file.get("observedLocators") or [])
        else:
            observed_locators = []
        file_record = {
            "fileId": file_id,
            "documentId": document_id,
            "title": clean_source_title(str(file_payload.get("title") or ""))
            or (
                clean_source_title(str(existing_file.get("title") or ""))
                if isinstance(existing_file, Mapping)
                else ""
            )
            or file_payload.get("filename")
            or "Untitled local document",
            "filename": file_payload.get("filename"),
            "sourcePath": file_payload.get("sourcePath"),
            "mediaType": file_payload.get("mediaType"),
            "revision": revision,
            "observedLocators": observed_locators,
            "metadataSource": "getFileDetails",
            "verificationStatus": "verified",
        }

        cursor = arguments.get("cursor")
        first_page = cursor is None or cursor == ""
        complete_inventory = structured.get("inventoryComplete") is True
        if "inventoryComplete" in structured and not isinstance(
            structured.get("inventoryComplete"), bool
        ):
            return "unverified_invalid_inventory", 0
        if complete_inventory and not first_page:
            return "unverified_cursor_sequence", 0
        current = ledger["inventories"].get(file_id)
        inventory: dict[str, Any]
        if complete_inventory:
            inventory_page_count = structured.get("inventoryPageCount")
            inventory_table_count = structured.get("inventoryTableCount")
            if (
                not isinstance(inventory_page_count, int)
                or isinstance(inventory_page_count, bool)
                or inventory_page_count < 1
                or not isinstance(inventory_table_count, int)
                or isinstance(inventory_table_count, bool)
                or inventory_table_count < 0
                or inventory_table_count != len(tables)
            ):
                return "unverified_inventory_count_mismatch", 0
            inventory = {
                "fileId": file_id,
                "tableIds": [],
                "complete": False,
                "pageCount": inventory_page_count,
                "nextCursor": None,
                "reportedTableCount": inventory_table_count,
                "verificationStatus": "verified",
            }
        elif first_page:
            inventory = {
                "fileId": file_id,
                "tableIds": [],
                "complete": False,
                "pageCount": 0,
                "nextCursor": None,
                "verificationStatus": "verified",
            }
        else:
            if not isinstance(cursor, str) or not cursor:
                return "unverified_cursor_sequence", 0
            if not isinstance(current, Mapping) or current.get("complete"):
                return "unverified_cursor_sequence", 0
            if current.get("nextCursor") != cursor:
                return "unverified_cursor_sequence", 0
            inventory = dict(_safe_json(current))

        pending_table_ids: list[str] = []
        for raw in tables:
            table = _mapping(raw)
            if table is None:
                return "unverified_invalid_payload", 0
            table_id = table.get("tableId")
            if not isinstance(table_id, str) or not table_id:
                return "unverified_invalid_payload", 0
            projected_file_id = table.get("fileId")
            if projected_file_id is None:
                if not complete_inventory:
                    return "unverified_identity_mismatch", 0
            elif projected_file_id != file_id:
                return "unverified_identity_mismatch", 0
            if table_id in pending_table_ids or table_id in inventory["tableIds"]:
                return "unverified_duplicate_table", 0
            pending_table_ids.append(table_id)

        next_cursor = structured.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            return "unverified_invalid_cursor", 0
        for table_id in pending_table_ids:
            if table_id not in inventory["tableIds"]:
                inventory["tableIds"].append(table_id)
        if not complete_inventory:
            inventory["pageCount"] += 1
        inventory["nextCursor"] = next_cursor
        inventory["complete"] = next_cursor is None
        if complete_inventory and next_cursor is not None:
            return "unverified_invalid_inventory", 0

        extraction = _mapping(structured.get("tableExtraction"))
        extraction_count = extraction.get("tableCount") if extraction is not None else None
        if extraction_count is not None and (
            not isinstance(extraction_count, int)
            or isinstance(extraction_count, bool)
            or extraction_count < 0
        ):
            return "unverified_inventory_count_mismatch", 0
        if inventory["complete"] and isinstance(extraction_count, int):
            if extraction_count != len(inventory["tableIds"]):
                return "unverified_inventory_count_mismatch", 0
            inventory["reportedTableCount"] = extraction_count
        ledger["files"][file_id] = file_record
        ledger["inventories"][file_id] = inventory
        return "verified", len(pending_table_ids)

    def _ingest_table(
        self,
        research_id: str,
        ledger: dict[str, Any],
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
        content: list[Any],
    ) -> tuple[str, int]:
        if structured.get("schemaVersion") != "knowledge-table-artifact/2":
            return "unverified_contract_version", 0
        table_id = structured.get("tableId")
        file_id = structured.get("fileId")
        if table_id != arguments.get("tableId") or file_id != arguments.get("fileId"):
            return "unverified_identity_mismatch", 0
        text = _mapping(structured.get("text"))
        screenshot = _mapping(structured.get("screenshot"))
        if (
            not isinstance(table_id, str)
            or not isinstance(file_id, str)
            or text is None
            or screenshot is None
        ):
            return "unverified_invalid_payload", 0
        text_content = text.get("content")
        text_sha = text.get("sha256")
        screenshot_sha = screenshot.get("sha256")
        if not isinstance(text_content, str) or not isinstance(text_sha, str):
            return "unverified_invalid_payload", 0
        if hashlib.sha256(text_content.encode("utf-8")).hexdigest() != text_sha:
            return "unverified_hash_mismatch", 0
        if not isinstance(screenshot_sha, str) or _SHA256.fullmatch(screenshot_sha) is None:
            return "unverified_invalid_payload", 0
        image_bytes, image_mime, image_status = self._verified_screenshot(
            structured,
            screenshot,
            content,
        )
        if image_bytes is None or image_mime is None:
            return image_status, 0
        if hashlib.sha256(image_bytes).hexdigest() != screenshot_sha:
            return "unverified_hash_mismatch", 0
        source_file = ledger["files"].get(file_id)
        if isinstance(source_file, Mapping):
            source_revision = source_file.get("revision")
            table_revision = structured.get("revision")
            if source_revision and table_revision and source_revision != table_revision:
                return "unverified_revision_mismatch", 0
        private_path = self._private_screenshot_path(
            research_id,
            digest=screenshot_sha,
            media_type=image_mime,
        )
        safe_screenshot = {
            key: value
            for key, value in screenshot.items()
            if key not in {"dataBase64", "localPath"}
        }
        record = {
            "tableId": table_id,
            "fileId": file_id,
            "documentId": structured.get("documentId"),
            "revision": structured.get("revision"),
            "page": structured.get("page"),
            "locator": _safe_json(structured.get("locator") or {}),
            "text": _safe_json(text),
            "screenshot": _safe_json(safe_screenshot),
            "screenshotPrivatePath": private_path.relative_to(self.private_root).as_posix(),
            "verificationStatus": "verified",
        }
        for field in ("textTruncated", "textPreviewTruncatedForTransport", "tableTextProjection"):
            if field in structured:
                record[field] = _safe_json(structured[field])
        existing = ledger["tables"].get(table_id)
        if existing is not None and sha256_json(existing) != sha256_json(record):
            return "unverified_collision", 0
        self._write_private_screenshot(private_path, image_bytes)
        ledger["tables"][table_id] = record
        return "verified", 1

    @staticmethod
    def _retrieval_telemetry(structured: Mapping[str, Any]) -> dict[str, Any]:
        return _safe_json(
            {
                "requestedProfile": structured.get("requestedProfile"),
                "contractVersion": structured.get("contractVersion"),
                "chunkPolicyId": structured.get("chunkPolicyId"),
                "indexVersion": structured.get("indexVersion"),
                "effectiveProfile": structured.get("effectiveProfile")
                or structured.get("retrievalProfile"),
                "retrievalProfile": structured.get("retrievalProfile"),
                "selectionSource": structured.get("selectionSource"),
                "fallbackReason": structured.get("fallbackReason"),
                "warnings": structured.get("warnings")
                if isinstance(structured.get("warnings"), list)
                else [],
                "scopeEnforced": structured.get("scopeEnforced"),
                "selectionStrategy": structured.get("selectionStrategy"),
                "budgetExceeded": structured.get("budgetExceeded"),
                "lexicalCandidateCount": structured.get("lexicalCandidateCount"),
                "vectorCandidateCount": structured.get("vectorCandidateCount"),
            }
        )

    def _verified_screenshot(
        self,
        structured: Mapping[str, Any],
        screenshot: Mapping[str, Any],
        content: Sequence[Any],
    ) -> tuple[bytes | None, str | None, str]:
        if structured.get("screenshotMaterializationError") is not None:
            return None, None, "unverified_screenshot_materialization"

        declared_media_type = screenshot.get("mediaType")
        if not isinstance(declared_media_type, str):
            return None, None, "unverified_invalid_payload"
        image = _decode_image(content)
        local_values = [
            value
            for value in (
                structured.get("screenshotLocalPath"),
                screenshot.get("localPath"),
            )
            if value is not None
        ]
        local_payload: bytes | None = None
        if local_values:
            if self.media_root is None:
                return None, None, "unverified_screenshot_root_unconfigured"
            if not all(isinstance(value, str) and value for value in local_values):
                return None, None, "unverified_screenshot_path"
            try:
                resolved_paths = {
                    Path(value).expanduser().resolve(strict=True) for value in local_values
                }
                if len(resolved_paths) != 1:
                    return None, None, "unverified_screenshot_path_mismatch"
                local_payload = self._read_bounded_file(
                    resolved_paths.pop(),
                    allowed_root=self.media_root,
                )
            except (OSError, ResearchStateError, RuntimeError, ValueError):
                return None, None, "unverified_screenshot_path"

        if local_payload is None and image is None:
            return None, None, "unverified_missing_screenshot"
        image_payload = image[0] if image is not None else None
        if local_payload is not None and image_payload is not None:
            if hashlib.sha256(local_payload).digest() != hashlib.sha256(image_payload).digest():
                return None, None, "unverified_screenshot_source_mismatch"
        payload = local_payload if local_payload is not None else image_payload
        assert payload is not None
        detected_media_type = self._detect_image_media_type(payload)
        if detected_media_type is None:
            return None, None, "unverified_screenshot_magic"
        if detected_media_type != declared_media_type:
            return None, None, "unverified_screenshot_media_type"
        if image is not None and image[1] != detected_media_type:
            return None, None, "unverified_screenshot_media_type"
        declared_size = screenshot.get("sizeBytes")
        if declared_size is not None and (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size != len(payload)
        ):
            return None, None, "unverified_screenshot_size"
        return payload, detected_media_type, "verified"

    @staticmethod
    def _detect_image_media_type(payload: bytes) -> str | None:
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        return None

    @staticmethod
    def _read_bounded_file(path: Path, *, allowed_root: Path) -> bytes:
        root = allowed_root.resolve(strict=True)
        target = path.resolve(strict=True)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ResearchStateError("screenshot path is outside its allowed root") from exc
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ResearchStateError("screenshot path is not a regular file")
            if metadata.st_size <= 0 or metadata.st_size > _MAX_SCREENSHOT_BYTES:
                raise ResearchStateError("screenshot size is outside the allowed range")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(_MAX_SCREENSHOT_BYTES + 1)
            if len(payload) != metadata.st_size or len(payload) > _MAX_SCREENSHOT_BYTES:
                raise ResearchStateError("screenshot changed while it was being read")
            return payload
        finally:
            os.close(descriptor)

    def _private_screenshot_path(
        self,
        research_id: str,
        *,
        digest: str,
        media_type: str,
    ) -> Path:
        self._state_path(research_id)
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }[media_type]
        target = (self.private_root / research_id / "media" / f"{digest}{extension}").resolve()
        target.relative_to((self.private_root / research_id).resolve())
        return target

    @staticmethod
    def _write_private_screenshot(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        if path.exists():
            if path.read_bytes() != payload:
                raise ResearchStateError("private screenshot digest collision")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)

    def _state_for_render(self, state: Mapping[str, Any]) -> dict[str, Any]:
        rendered = dict(_safe_json(state))
        research_id = str(state["researchId"])
        research_root = (self.private_root / research_id).resolve()
        selected_table_ids = {
            item["tableId"] for item in rendered["report"]["items"] if item.get("kind") == "table"
        }
        for table_id in selected_table_ids:
            record = rendered["ledger"]["tables"].get(table_id)
            if not isinstance(record, dict):
                raise ResearchStateError("selected table is missing from the ledger")
            relative_path = record.get("screenshotPrivatePath")
            screenshot = record.get("screenshot")
            if not isinstance(relative_path, str) or not isinstance(screenshot, dict):
                raise ResearchStateError("selected table screenshot metadata is invalid")
            target = (self.private_root / relative_path).resolve()
            payload = self._read_bounded_file(target, allowed_root=research_root)
            expected_digest = screenshot.get("sha256")
            expected_media_type = screenshot.get("mediaType")
            if hashlib.sha256(payload).hexdigest() != expected_digest:
                raise ResearchStateError("private screenshot SHA256 mismatch")
            if self._detect_image_media_type(payload) != expected_media_type:
                raise ResearchStateError("private screenshot media type mismatch")
            record["screenshotDataBase64"] = base64.b64encode(payload).decode("ascii")
        return rendered

    @staticmethod
    def _verified_evidence_ids(state: Mapping[str, Any], values: Sequence[str]) -> list[str]:
        if isinstance(values, str) or not isinstance(values, Sequence) or not values:
            raise ResearchStateError("evidenceIds must be a non-empty array")
        evidence = state["ledger"]["evidence"]
        ids: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise ResearchStateError("evidenceIds must contain non-empty strings")
            record = evidence.get(value)
            if not isinstance(record, Mapping) or record.get("verificationStatus") != "verified":
                raise ResearchStateError(
                    f"evidence was not verified by an actual search result: {value}"
                )
            if value not in ids:
                ids.append(value)
        return ids

    @staticmethod
    def _reject_internal_ids(*values: str, state: Mapping[str, Any] | None = None) -> None:
        known_ids: set[str] = set()
        if state is not None:
            ledger = state["ledger"]
            known_ids.update(ledger["evidence"])
            known_ids.update(ledger["files"])
            known_ids.update(ledger["tables"])
            extensions = _mapping(state.get("extensions")) or {}
            navigation = _mapping(extensions.get("navigation")) or {}
            namespace = navigation.get("namespace")
            if isinstance(namespace, str) and _NAVIGATION_NAMESPACE.fullmatch(namespace):
                # Match the whole namespace, including snapshot/cursor suffixes,
                # without treating ordinary D1/E1 labels as internal identifiers.
                known_ids.add(namespace + ":")
            for bucket_name in (
                "files",
                "evidence",
                "tables",
                "scopes",
                "snapshots",
                "projections",
            ):
                bucket = _mapping(navigation.get(bucket_name)) or {}
                for entry in bucket.values():
                    row = _mapping(entry) or {}
                    for field in ("ref", "scopeRef", "snapshotRef", "nextCursor", "resumeCursor"):
                        reference = row.get(field)
                        if isinstance(reference, str):
                            prefix, separator, _ = reference.partition(":")
                            if separator and _NAVIGATION_NAMESPACE.fullmatch(prefix):
                                known_ids.add(reference)
            for record in ledger["evidence"].values():
                if not isinstance(record, Mapping):
                    continue
                for name in (
                    "documentId",
                    "chunkId",
                    "parentChunkId",
                    "previousChunkId",
                    "nextChunkId",
                ):
                    identifier = record.get(name)
                    if isinstance(identifier, str) and identifier:
                        known_ids.add(identifier)
        for value in values:
            if _INTERNAL_ID_HINT.search(value) or any(
                identifier in value for identifier in known_ids
            ):
                raise ResearchStateError("human-facing report text contains an internal identifier")

    @classmethod
    def _reject_report_leaks(cls, html: str, state: Mapping[str, Any]) -> None:
        cls._reject_internal_ids(html, state=state)

    def _provenance(
        self,
        state: Mapping[str, Any],
        *,
        html_bytes: bytes,
        pdf_bytes: bytes,
    ) -> dict[str, Any]:
        ledger = _safe_json(state["ledger"])
        for record in ledger["evidence"].values():
            record.pop("content", None)
        for record in ledger["tables"].values():
            record.pop("screenshotDataBase64", None)
            record.pop("screenshotPrivatePath", None)
            text = record.get("text")
            if isinstance(text, dict):
                text.pop("content", None)
        coverage = state.get("readingCoverage")
        result = {
            "schemaVersion": PROVENANCE_SCHEMA_VERSION,
            "researchId": state["researchId"],
            "title": state["title"],
            "mode": state.get("mode", "standard"),
            "language": state.get("language"),
            "reviewPreparation": review_preparation(state),
            "ledger": ledger,
            "report": _safe_json(state["report"]),
            "bibliography": build_bibliography(state),
            "researchDepth": research_depth_summary(state),
            "artifacts": {
                "report.html": hashlib.sha256(html_bytes).hexdigest(),
                "report.pdf": hashlib.sha256(pdf_bytes).hexdigest(),
            },
        }
        if isinstance(coverage, Mapping):
            # Keep the current top-level field and the older extension name so
            # recovery/audit consumers can read both report generations.
            result["readingCoverage"] = _safe_json(coverage)
            result["extensions"] = {"bibliographyReadingCoverage": _safe_json(coverage)}
        return result

    @staticmethod
    def _report_coverage(state: Mapping[str, Any]) -> dict[str, int]:
        evidence_ids: set[str] = set()
        table_ids: set[str] = set()
        source_ids: set[str] = set()
        claim_count = 0
        for item in state["report"]["items"]:
            if item.get("kind") == "claim":
                claim_count += 1
                evidence_ids.update(item.get("evidenceIds", []))
            elif item.get("kind") == "table":
                table_ids.add(str(item["tableId"]))
        for evidence_id in evidence_ids:
            record = state["ledger"]["evidence"].get(evidence_id)
            if isinstance(record, Mapping):
                source_ids.add(str(record["fileId"]))
        for table_id in table_ids:
            record = state["ledger"]["tables"].get(table_id)
            if isinstance(record, Mapping):
                source_ids.add(str(record["fileId"]))
        return {
            "claimCount": claim_count,
            "tableCount": len(table_ids),
            "sourceCount": build_bibliography(state)["sourceCount"],
            "sourceFileCount": len(source_ids),
            "evidenceCount": len(evidence_ids),
        }

    def _state_path(self, research_id: str) -> Path:
        if not isinstance(research_id, str) or _RESEARCH_ID.fullmatch(research_id) is None:
            raise ResearchStateError("researchId is invalid")
        return self.private_root / research_id / "state.json"

    @contextmanager
    def _research_lock(self, research_id: str, *, shared: bool = False) -> Iterator[None]:
        lock_path = self._state_path(research_id).parent / ".state.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileNotFoundError as exc:
            raise ResearchStateError("researchId was not found") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ResearchStateError("research lock is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _load(self, research_id: str) -> dict[str, Any]:
        path = self._state_path(research_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchStateError("researchId was not found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchStateError("research state is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise ResearchStateError("research state schema is invalid")
        return payload

    def _save(self, state: Mapping[str, Any]) -> None:
        path = self._state_path(str(state["researchId"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self._atomic_write(
            path,
            (
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                + "\n"
            ).encode("utf-8"),
        )

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        tmp = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
            KnowledgeResearchStore._fsync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _render_pdf(html: str, base_url: Path) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ResearchStateError(
            "knowledge report PDF rendering requires opensquilla[document-extras]"
        ) from exc
    payload = HTML(string=html, base_url=str(base_url)).write_pdf()
    if not isinstance(payload, bytes):
        raise ResearchStateError("PDF renderer returned an invalid payload")
    return payload
