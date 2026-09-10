"""Export HTML bytes and classified tool metadata, excluding message/thought text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tomllib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from opensquilla.artifacts import ArtifactStore, artifact_bundle_manifest, collect_artifact_bundle
from opensquilla.paths import media_root_from_config

_EVIDENCE_TOOLS = frozenset({
    "read_file", "read_text", "write_file", "write_text", "edit_file", "apply_patch",
    "list_dir", "glob_search", "grep_search", "publish_artifact", "browser", "exec_command",
})


def _safe_tool_result_diagnostic(
    tool_name: str, is_error: int | None, result: str | None,
    execution_status: str | None, persisted_truncated: int | None,
) -> str:
    """SQLite projection: return fixed classifications, never tool payload values."""
    diagnostic = {"is_error": bool(is_error) if is_error in (0, 1) else None}
    category = "unknown" if is_error != 0 else "none"
    try:
        execution = json.loads(execution_status or "null")
    except (TypeError, ValueError, RecursionError):
        execution = None
    if isinstance(execution, dict) and isinstance(execution.get("truncated"), bool):
        diagnostic["execution_status_truncated"] = execution["truncated"]
    if persisted_truncated in (0, 1):
        diagnostic["persisted_result_truncated"] = bool(persisted_truncated)
    if is_error == 1:
        try:
            payload = json.loads(result or "null")
        except (TypeError, ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict):
            error_class = payload.get("error_class")
            category = {
                "InvalidToolArgumentsError": "schema",
                "PermissionError": "permission",
                "PermissionDenied": "permission",
                "policy_denial": "permission",
                "FileNotFoundError": "file_not_found",
                "ProjectedToolArgumentsError": "projected_arguments",
            }.get(error_class, "unknown") if isinstance(error_class, str) else "unknown"
            message = payload.get("user_message")
            if error_class == "RetryableToolInputError" and isinstance(message, str):
                patterns = {
                    "exact_text_missing": (
                        r"edit_file could not find (?:old_text|edits\[\d+\]\.old_text) in "
                    ),
                    "exact_text_multiple": (
                        r"edit_file (?:old_text|edits\[\d+\]\.old_text) matches \d+ locations in "
                    ),
                    "patch_context_mismatch": (
                        r"apply_patch (?:context mismatch at line \d+:|"
                        r"hunk context/delete exceeds file length at line \d+\.)"
                    ),
                    "patch_format": (
                        r"(?:apply_patch needs a patch (?:beginning|ending) with |"
                        r"Invalid apply_patch hunk header\.|"
                        r"apply_patch did not find any file operations\.)"
                    ),
                }
                for label, pattern in patterns.items():
                    if re.match(pattern, message):
                        category = label
                        break
                if message.startswith(
                    f"{tool_name} must read existing workspace file before modifying it: "
                ):
                    category = "fresh_read_required"
                if category == "unknown" and re.search(
                    r" changed since it was read\. Use read_file without offset or limit "
                    r"to refresh context, then retry (?:edit_file|write_file|apply_patch)\.$",
                    message,
                ):
                    category = "stale_read"
        if category == "unknown" and isinstance(execution, dict):
            category = {
                "invalid_tool_arguments": "schema", "denied": "permission",
            }.get(execution.get("reason"), "unknown") if isinstance(
                execution.get("reason"), str
            ) else "unknown"
    diagnostic["diagnostic_class"] = category
    return json.dumps(diagnostic)


def safe_sqlite_timeline(connection: sqlite3.Connection, tables: set[str]) -> dict:
    """Project identifiers/timestamps in SQLite; never select message or thought text."""
    result: dict = {
        "schemaVersion": 1,
        "timeMeaning": "persisted transcript order/timestamps, not streaming tool duration",
        "toolCalls": [],
        "toolResults": [],
        "routerDecisions": [],
        "turnErrors": [],
    }

    def safe_rows(cursor: sqlite3.Cursor) -> list[dict]:
        return [
            {
                key: value
                for key, value in dict(row).items()
                if value is None
                or isinstance(value, (bool, int, float))
                or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,160}", value))
            }
            for row in cursor
        ]

    if "transcript_entries" in tables:
        connection.create_function("html_tool_diagnostic", 5, _safe_tool_result_diagnostic)
        rows = connection.execute("""
            WITH segments AS (
              SELECT t.id AS transcript_id,t.message_id,t.created_at,j.key AS call_index,
                CASE WHEN j.type='object' THEN j.value ELSE '{}' END AS segment
              FROM transcript_entries AS t,
                json_each(CASE WHEN json_valid(t.tool_calls) THEN t.tool_calls ELSE '[]' END) j
              WHERE t.role='assistant'
            ), tools AS (
              SELECT transcript_id,message_id,created_at,call_index,
                json_extract(segment,'$.type') AS segment_type,
                coalesce(json_extract(segment,'$.tool_use_id'),json_extract(segment,'$.id'))
                  AS tool_use_id,
                coalesce(json_extract(segment,'$.name'),json_extract(segment,'$.function.name'))
                  AS tool_name,
                segment
              FROM segments
            )
            SELECT transcript_id,message_id,created_at,call_index,segment_type,
              tool_use_id,tool_name,
              CASE WHEN segment_type='tool_result' THEN html_tool_diagnostic(
                tool_name,
                CASE json_type(segment,'$.is_error') WHEN 'true' THEN 1 WHEN 'false' THEN 0 END,
                json_extract(segment,'$.result'),json_extract(segment,'$.execution_status'),
                CASE json_type(segment,'$.result_truncated')
                  WHEN 'true' THEN 1 WHEN 'false' THEN 0 END
              ) END AS diagnostic
            FROM tools
            WHERE tool_name IN (SELECT value FROM json_each(?))
              AND (segment_type IN ('tool_use','tool_result','function') OR segment_type IS NULL)
            ORDER BY transcript_id,CAST(call_index AS INTEGER)
        """, (json.dumps(sorted(_EVIDENCE_TOOLS)),))
        calls: dict[tuple, dict] = {}
        outcomes: dict[tuple, dict] = {}
        for raw_row in rows:
            row = dict(raw_row)
            diagnostic = row.pop("diagnostic")
            segment_type = row.pop("segment_type")
            tool_id = row.get("tool_use_id")
            if not isinstance(tool_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", tool_id):
                continue
            row = {
                key: value for key, value in row.items()
                if value is None or isinstance(value, (int, float))
                or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value))
            }
            key = (row["transcript_id"], tool_id)
            if segment_type == "tool_result":
                row.update(json.loads(diagnostic))
                outcomes[key] = row
            else:
                calls.setdefault(key, row)
        result["toolCalls"] = list(calls.values())
        result["toolResults"] = list(outcomes.values())
        # Older provider-native rows carry no reliable success flag.
        legacy_results = safe_rows(
            connection.execute("""
            SELECT id AS transcript_id,message_id,tool_call_id,created_at
            FROM transcript_entries WHERE role='tool' AND tool_call_id IS NOT NULL ORDER BY id
        """)
        )
        for row in legacy_results:
            tool_id = row.get("tool_call_id")
            if not isinstance(tool_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", tool_id):
                continue
            row.update(is_error=None, diagnostic_class="unknown")
            result["toolResults"].append(row)
    projections = {
        "router_decisions": (
            "routerDecisions",
            [
                "decision_id",
                "turn_index",
                "ts_ms",
                "classifier",
                "proposed_tier",
                "final_tier",
                "requested_provider",
                "requested_model",
                "provider",
                "model",
                "executed_provider",
                "executed_model",
                "fallback_reason",
                "source",
                "thinking_level",
                "executed_kind",
                "ensemble_profile",
                "fallback_hops",
            ],
        ),
        "turn_errors": (
            "turnErrors",
            [
                "error_id",
                "turn_id",
                "ts_ms",
                "surface",
                "error_class",
                "provider",
                "model",
                "fallback_hops",
            ],
        ),
    }
    for table, (name, fields) in projections.items():
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        selected = ",".join(field for field in fields if field in columns)
        result[name] = safe_rows(connection.execute(f"SELECT {selected} FROM {table}"))
    return result


def evidence_media_root(profile: Path, run_root: Path) -> Path:
    home = profile / "opensquilla"
    config_path = home / "config.toml"
    raw = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    config = SimpleNamespace(
        state_dir=raw.get("state_dir", str(home / "state")),
        config_path=str(config_path),
        attachments=SimpleNamespace(media_root=raw.get("attachments", {}).get("media_root")),
    )
    root = media_root_from_config(config)
    if not root.resolve().is_relative_to(run_root.resolve()):
        raise ValueError("evidence_media_outside_attempt")
    return root


def working_rows(connection: sqlite3.Connection, tables: set[str]) -> list[dict]:
    if "artifact_working_files" not in tables:
        return []
    revision_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(artifact_revisions)")
    }
    result = []
    for row in connection.execute("SELECT * FROM artifact_working_files"):
        binding = dict(row)
        binding.setdefault("base_revision_id", "")
        if "media_type" in revision_columns and binding["base_revision_id"]:
            revision = connection.execute(
                "SELECT media_type FROM artifact_revisions WHERE revision_id=?",
                (binding["base_revision_id"],),
            ).fetchone()
            if revision is not None:
                binding["entry_mime"] = revision["media_type"]
        if "artifact_working_sources" in tables:
            source = connection.execute(
                "SELECT source_path,bundle_mode,bundle_root FROM artifact_working_sources "
                "WHERE document_id=?", (binding["document_id"],),
            ).fetchone()
            if source is not None:
                binding.update(dict(source))
        result.append(binding)
    return result


def working_bundle(binding: dict):
    try:
        from opensquilla.artifact_session.working_files import WorkingFiles
    except ModuleNotFoundError as error:
        if error.name != "opensquilla.artifact_session.working_files":
            raise
        # Historical subjects predate source provenance and own their copied directory.
        root = Path(binding["workspace"]) / binding["relative_root"]
        return collect_artifact_bundle(
            root / binding["entrypoint"], workspace_root=binding["workspace"],
            mode="directory", bundle_root=root,
        )
    return WorkingFiles(**binding).bundle()


def rescue_html_resources(profile: Path, output: Path, run_root: Path) -> dict:
    """Preserve local generated material even if its metadata cannot be exported."""
    artifact_root = evidence_media_root(profile, run_root) / "artifacts"
    database = profile / "opensquilla" / "state" / "sessions.db"
    errors = []
    resources: dict[Path, Path] = {}
    if artifact_root.exists():
        resources.update(
            (Path("artifact-store") / path.relative_to(artifact_root), path)
            for path in artifact_root.rglob("*") if path.is_file()
        )
    if database.exists():
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
                store = ArtifactStore(evidence_media_root(profile, run_root))
                for binding in working_rows(connection, tables):
                    workspace = Path(binding["workspace"])
                    entry = workspace / (binding.get("source_path") or str(
                        Path(binding["relative_root"]) / binding["entrypoint"]
                    ))
                    logical_entry = PurePosixPath(binding["entrypoint"])
                    root = entry.parents[len(logical_entry.parts) - 1]
                    known_paths = {binding["entrypoint"]}
                    try:
                        bundle = working_bundle(binding)
                        if bundle is not None:
                            known_paths.update(item.path for item in bundle.files)
                    except Exception as error:
                        errors.append(type(error).__name__)
                    if "artifact_revisions" in tables and "artifact_documents" in tables:
                        revisions = connection.execute(
                            "SELECT r.artifact_id,d.session_id FROM artifact_revisions r "
                            "JOIN artifact_documents d ON d.document_id=r.document_id "
                            "WHERE r.document_id=?", (binding["document_id"],),
                        )
                        for revision in revisions:
                            try:
                                manifest = store.describe_preview_bundle(
                                    revision["artifact_id"], session_id=revision["session_id"],
                                )
                                if manifest is not None:
                                    known_paths.update(item.path for item in manifest.files)
                            except Exception as error:
                                errors.append(type(error).__name__)
                    for logical in known_paths:
                        path = entry if logical == binding["entrypoint"] else root / logical
                        if path.is_file():
                            resources[Path("working-" + binding["document_id"]) / logical] = path
        except sqlite3.Error as error:
            errors.append(type(error).__name__)
    files = []
    for relative, path in resources.items():
        if relative.is_absolute() or ".." in relative.parts:
            errors.append("UnsafeResourceDestination")
            continue
        if not path.resolve().is_relative_to(run_root.resolve()) or any(
            candidate.is_symlink() for candidate in (path, *path.parents)
            if candidate.is_relative_to(run_root)
        ):
            errors.append("UnsafeResourcePath")
            continue
        try:
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = path.read_bytes()
            target.write_bytes(data)
            target.chmod(0o600)
            files.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
        except OSError as error:
            errors.append(type(error).__name__)
    return {"files": files, "errors": errors, "directory": str(output.relative_to(run_root))}


def audit_exported_project(directory: Path, entrypoint: str, mime: str) -> dict:
    from opensquilla.artifacts import (
        _extract_bundle_references,
        _is_sensitive_bundle_path,
        _resolve_bundle_reference,
    )

    bundle = collect_artifact_bundle(
        directory / entrypoint, workspace_root=directory, mode="auto", entry_mime=mime,
    )
    if bundle is None:
        return {"complete": False, "warningCodes": ["unavailable_entrypoint"], "missingPaths": []}
    paths = {item.path for item in bundle.files}
    missing = set()
    for item in bundle.files:
        try:
            references, _ = _extract_bundle_references(
                item.path, item.data, force_html=item.path == entrypoint,
            )
        except ValueError:
            continue
        for reference in references:
            try:
                dependency = _resolve_bundle_reference(reference, source_path=item.path)
                if (
                    Path(item.path).suffix.casefold()
                    in {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
                    and "/" not in reference
                    and not reference.startswith(".")
                    and not Path(reference).suffix
                ):
                    continue
                if (
                    dependency and dependency not in paths
                    and not _is_sensitive_bundle_path(dependency)
                ):
                    missing.add(dependency)
            except ValueError:
                # The collector has already recorded unsafe references.
                continue
    return {
        "complete": bundle.collection_status == "complete",
        "warningCodes": list(bundle.warning_codes), "missingPaths": sorted(missing),
    }


def evidence_source_path_allowed(logical: str) -> bool:
    from opensquilla.artifacts import _is_sensitive_bundle_path

    parts = PurePosixPath(logical)
    web_suffixes = {
        ".html", ".htm", ".xhtml", ".css", ".js", ".mjs", ".cjs", ".svg",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
        ".woff", ".woff2", ".ttf", ".otf",
    }
    reserved = {"memory", "transcript", "transcripts", "replay", "state", "logs", "plans"}
    return (
        not _is_sensitive_bundle_path(logical)
        and parts.suffix.casefold() in web_suffixes
        and not any(PurePosixPath(part).stem.casefold() in reserved for part in parts.parts)
    )


def collect_evidence_source(entry: Path, workspace: Path, mime: str):
    """Collect render dependencies without opening private workspace documents."""
    from opensquilla.artifacts import (
        DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
        DEFAULT_ARTIFACT_BUNDLE_MAX_FILES,
        ArtifactBundle,
        ArtifactBundleSourceFile,
        _ensure_no_bundle_link_components,
        _extract_bundle_references,
        _read_regular_bundle_file,
        _resolve_bundle_reference,
        artifact_mime_for_name,
    )

    root = entry.parent
    queue, seen, files, warnings = [entry.name], set(), [], set()
    total = 0
    # This evidence path is deliberately narrower than the product's generic
    # artifact collector. Runtime notes/databases/transcripts are never read.
    while queue:
        logical = queue.pop(0)
        if logical in seen:
            continue
        if len(seen) >= DEFAULT_ARTIFACT_BUNDLE_MAX_FILES:
            warnings.add("evidence_resource_budget_exceeded")
            break
        seen.add(logical)
        parts = PurePosixPath(logical)
        if not evidence_source_path_allowed(logical):
            warnings.add("evidence_resource_type_or_path_excluded")
            continue
        source = root.joinpath(*parts.parts)
        try:
            _ensure_no_bundle_link_components(workspace, source)
            size = source.stat().st_size
            if len(files) >= DEFAULT_ARTIFACT_BUNDLE_MAX_FILES or (
                total + size > DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES
            ):
                warnings.add("evidence_resource_budget_exceeded")
                continue
            data = _read_regular_bundle_file(source)
            if total + len(data) > DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES:
                warnings.add("evidence_resource_budget_exceeded")
                continue
            total += len(data)
            files.append(ArtifactBundleSourceFile(
                logical, mime if logical == entry.name else artifact_mime_for_name(logical), data,
            ))
        except FileNotFoundError:
            warnings.add("missing_dependency")
            continue
        except (ValueError, OSError):
            warnings.add("unsafe_dependency")
            continue
        try:
            references, dynamic = _extract_bundle_references(
                logical, data, force_html=logical == entry.name,
            )
        except ValueError:
            warnings.add("unsupported_dependency_encoding")
            continue
        if dynamic:
            warnings.add("dynamic_dependency")
        for reference in references:
            try:
                dependency = _resolve_bundle_reference(reference, source_path=logical)
            except ValueError:
                warnings.add("outside_or_unsafe_dependency")
                continue
            if dependency is not None and dependency not in seen and dependency not in queue:
                if len(seen) + len(queue) >= DEFAULT_ARTIFACT_BUNDLE_MAX_FILES:
                    warnings.add("evidence_resource_budget_exceeded")
                    continue
                queue.append(dependency)
    return ArtifactBundle(
        entrypoint=entry.name, files=tuple(sorted(files, key=lambda item: item.path)),
        collection_status="partial" if warnings else "complete",
        warning_codes=tuple(sorted(warnings)),
    )


def referenced_source_bundles(connection, profile, run_root, session_id, ref):
    """Read only publish path locators, then collect their bounded static references.

    This is evidence recovery, not a mutation or an alternative preview resource root.
    No transcript content or tool body is returned from SQLite.
    """
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.tools.path_aliases import resolve_workspace_alias

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if "transcript_entries" not in tables:
        return []
    columns = {row[1] for row in connection.execute("PRAGMA table_info(transcript_entries)")}
    if not {"session_id", "id", "tool_calls"} <= columns:
        return []
    session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    selected = ",".join(
        name for name in ("session_id", "agent_id", "workspace_id") if name in session_columns
    )
    row = connection.execute(
        f"SELECT {selected} FROM sessions WHERE session_id=?", (session_id,),
    ).fetchone()
    if row is None:
        return []
    row = dict(row)
    config_path = profile / "opensquilla" / "config.toml"
    raw = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    config = SimpleNamespace(
        workspace_dir=raw.get("workspace_dir") or str(profile / "opensquilla" / "workspace"),
        agents=raw.get("agents", []),
    )
    workspace = resolve_agent_workspace_dir(row.get("agent_id") or "main", config)
    if row.get("workspace_id"):
        if "project_workspaces" not in tables:
            return []
        project = connection.execute(
            "SELECT path,removed_at,trusted_at FROM project_workspaces WHERE workspace_id=?",
            (row["workspace_id"],),
        ).fetchone()
        if project is None or project["removed_at"] is not None or not project["trusted_at"]:
            return []
        workspace = Path(project["path"])
    if (
        not workspace.is_absolute()
        or not workspace.is_relative_to(run_root)
        or not workspace.resolve().is_relative_to(run_root.resolve())
    ):
        return []
    # JSON operations stay in SQLite; select only a path, never arguments/content.
    locators = connection.execute("""
        WITH calls AS (
          SELECT t.id, j.value AS call
          FROM transcript_entries t,
            json_each(CASE WHEN json_valid(t.tool_calls) THEN t.tool_calls ELSE '[]' END) j
          WHERE t.session_id=? AND json_valid(j.value)
        ), paths AS (
          SELECT id,
            coalesce(json_extract(call,'$.function.name'),json_extract(call,'$.name')) AS name,
            coalesce(json_extract(call,'$.function.arguments'),json_extract(call,'$.arguments'),
                     json_extract(call,'$.input')) AS args
          FROM calls
        )
        SELECT DISTINCT
          json_extract(CASE WHEN json_valid(args) THEN args ELSE '{}' END,'$.path') AS path
        FROM paths WHERE name='publish_artifact' ORDER BY id DESC LIMIT 129
    """, (session_id,)).fetchall()
    if len(locators) > 128:
        raise ValueError("source_locator_evidence_limit")
    recovered = []
    seen = set()
    for locator in locators:
        raw_path = locator["path"]
        if not isinstance(raw_path, str) or not raw_path or len(raw_path.encode()) > 4096:
            continue
        candidate = Path(raw_path)
        alias = resolve_workspace_alias(candidate, workspace)
        candidate = alias or (candidate if candidate.is_absolute() else workspace / candidate)
        candidate = Path(os.path.abspath(candidate))
        if not candidate.resolve().is_relative_to(workspace.resolve()):
            continue
        if any(
            p.is_symlink() for p in (candidate, *candidate.parents) if p.is_relative_to(run_root)
        ):
            continue
        if not evidence_source_path_allowed(candidate.relative_to(workspace).as_posix()):
            continue
        if candidate in seen or not candidate.is_file() or candidate.stat().st_size != ref.size:
            continue
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != ref.sha256:
            continue
        seen.add(candidate)
        bundle = collect_evidence_source(candidate, workspace, ref.mime)
        if any(
            item.path == bundle.entrypoint and hashlib.sha256(item.data).hexdigest() == ref.sha256
            for item in bundle.files
        ):
            recovered.append((candidate.relative_to(workspace).as_posix(), bundle))
    return recovered


def export_html_evidence(profile: Path, output: Path, run_root: Path) -> dict:
    home = profile / "opensquilla"
    database = home / "state" / "sessions.db"
    result: dict = {
        "published": [], "working": [], "unpublishedDependencies": [],
        "errors": [], "complete": True,
    }
    if not database.is_file():
        return result
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    result["timeline"] = safe_sqlite_timeline(connection, tables)
    sessions = [row[0] for row in connection.execute("SELECT session_id FROM sessions")]
    heads: dict[tuple[str, str], list[str]] = {}
    if "artifact_documents" in tables:
        for row in connection.execute(
            "SELECT d.document_id,d.session_id,r.artifact_id FROM artifact_documents d "
            "JOIN artifact_revisions r ON r.revision_id=d.head_revision_id"
        ):
            heads.setdefault((row["session_id"], row["artifact_id"]), []).append(row["document_id"])
    store = ArtifactStore(evidence_media_root(profile, run_root))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save_file(directory: Path, logical: str, data: bytes) -> dict:
        parts = PurePosixPath(logical)
        if parts.is_absolute() or ".." in parts.parts or "\\" in logical or ":" in logical:
            raise ValueError("invalid_evidence_resource_path")
        target = directory.joinpath(*parts.parts)
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("invalid_evidence_destination")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)
        return {"path": logical, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    for session_id in sessions:
        refs = {}
        try:
            refs.update(
                (ref.id, ref) for ref in store.list_refs(session_id=session_id, limit=5000).refs
            )
        except Exception as error:
            result["errors"].append({"sessionId": session_id, "errorClass": type(error).__name__})
            result["complete"] = False
        if {"artifact_revisions", "artifact_documents"} <= tables:
            try:
                revisions = connection.execute(
                    "SELECT DISTINCT r.artifact_id FROM artifact_revisions r "
                    "JOIN artifact_documents d ON d.document_id=r.document_id "
                    "WHERE d.session_id=?", (session_id,),
                ).fetchall()
            except Exception as error:
                result["errors"].append(
                    {"sessionId": session_id, "errorClass": type(error).__name__}
                )
                result["complete"] = False
                revisions = []
            for revision in revisions:
                artifact_id = revision["artifact_id"]
                try:
                    refs[artifact_id] = store.get_ref(
                        session_id=session_id, artifact_id=artifact_id,
                    )
                except Exception as error:
                    result["errors"].append(
                        {"artifactId": artifact_id, "errorClass": type(error).__name__}
                    )
                    result["complete"] = False
        for ref in refs.values():
            if ref.mime not in {"text/html", "application/xhtml+xml"} and Path(
                ref.name
            ).suffix.lower() not in {".html", ".htm"}:
                continue
            try:
                manifest = store.describe_preview_bundle(ref.id, session_id=session_id)
                directory = output / "published" / ref.id
                paths = [item.path for item in manifest.files] if manifest else [ref.name]
                files = []
                for logical in paths:
                    resource = store.resolve_preview_resource(
                        ref.id, session_id=session_id, logical_path=logical
                    )
                    data = resource.path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != resource.sha256:
                        raise ValueError("artifact_evidence_hash_mismatch")
                    files.append(save_file(directory, logical, data))
                entry = {
                    "artifactId": ref.id,
                    "headDocumentIds": heads.get((session_id, ref.id), []),
                    "entrypoint": manifest.entrypoint if manifest else ref.name,
                    "files": files,
                    "directory": str(directory.relative_to(run_root)),
                }
                if manifest:
                    entry["bundleDigest"] = manifest.bundle_digest
                    entry["collectionStatus"] = manifest.collection_status
                    (directory.parent / (ref.id + "-bundle-manifest.json")).write_text(
                        json.dumps(manifest.to_dict(), indent=2)
                    )
                # Integrity-complete storage is different from a complete webpage.
                # Check the bytes already exported, without executing script or fetching.
                reference_audit = audit_exported_project(directory, entry["entrypoint"], ref.mime)
                entry["storageEvidenceComplete"] = True
                entry["projectEvidence"] = reference_audit
                result["published"].append(entry)
                if not reference_audit["complete"]:
                    result["complete"] = False
                    result["errors"].append({
                        "artifactId": ref.id, "code": "PUBLISHED_PROJECT_DEPENDENCIES_INCOMPLETE",
                    })
                    # Do not merge these bytes into immutable published evidence or
                    # relabel the original publication complete after a successful rescue.
                    for index, (source_path, source_bundle) in enumerate(referenced_source_bundles(
                        connection, profile, run_root, session_id, ref,
                    )):
                        destination = output / "unpublished-dependencies" / ref.id / str(index)
                        result["unpublishedDependencies"].append({
                            "artifactId": ref.id, "sourceLocator": source_path,
                            "identityProof": "same_session_publish_path_and_entry_sha256",
                            "directory": str(destination.relative_to(run_root)),
                            "entrypoint": source_bundle.entrypoint,
                            "complete": source_bundle.collection_status == "complete",
                            "warningCodes": list(source_bundle.warning_codes),
                            "files": [
                                save_file(destination, item.path, item.data)
                                for item in source_bundle.files
                            ],
                        })
            except Exception as error:
                result["errors"].append({"artifactId": ref.id, "errorClass": type(error).__name__})
                result["complete"] = False
    if "artifact_working_files" in tables:
        for row in working_rows(connection, tables):
            try:
                workspace = Path(row["workspace"])
                root = workspace / row["relative_root"]
                if not root.resolve().is_relative_to(run_root.resolve()):
                    raise ValueError("working_evidence_outside_run")
                bundle = working_bundle(row)
                if bundle is None:
                    raise ValueError("working_evidence_bundle_missing")
                directory = output / "working" / row["document_id"]
                files = [save_file(directory, item.path, item.data) for item in bundle.files]
                result["working"].append(
                    {
                        "documentId": row["document_id"],
                        "entrypoint": bundle.entrypoint,
                        "files": files,
                        "bundleDigest": artifact_bundle_manifest(bundle).bundle_digest,
                        "directory": str(directory.relative_to(run_root)),
                    }
                )
            except Exception as error:
                result["errors"].append(
                    {"documentId": row["document_id"], "errorClass": type(error).__name__}
                )
                result["complete"] = False
    connection.close()
    if not result["complete"]:
        result["rescue"] = rescue_html_resources(profile, output / "raw-rescue", run_root)
    return result


if __name__ == "__main__":
    if "--timeline-only" in sys.argv:
        with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            print(json.dumps(safe_sqlite_timeline(connection, tables)))
    else:
        operation = rescue_html_resources if "--rescue" in sys.argv else export_html_evidence
        print(json.dumps(operation(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))))
