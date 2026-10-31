from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.config import VideoGenerationConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import tokenrhythm_video_generation
from opensquilla.provider.media_jobs import job_record
from opensquilla.provider.video_generation import VideoGenerationResult
from opensquilla.provider.video_generation_credentials import VideoGenerationCredential
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import media
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

_AUTHORITY = ("https://tokenrhythm.studio", "synthetic-account")
_BASE = "https://tokenrhythm.studio/v1"
_MANUAL_KEY = "synthetic-manual-video-key"
_LEASES = ["os_local_" + character * 43 for character in "ab"]


# Keep real-process SQLite recovery outside the parallel CI worker pool.
@pytest.mark.ci_serial
def test_video_receipts_survive_a_process_restart(
    tmp_path: Path, migrated_db_factory: Callable[[str | None], Path],
) -> None:
    # This verifies receipt recovery, not cold session-schema migrations.
    migrated_db_factory("usage.sqlite3")
    environment = {**os.environ, "PYTHONPATH": str(Path(media.__file__).parents[3])}
    outputs = []
    for phase in ("write", "read"):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), phase, str(tmp_path)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(json.loads(result.stdout))
    assert outputs == [{"saved": 4}, {"restored": 4, "resumed": 2, "rejected": 6}]


@pytest.mark.parametrize("authority", ["invalid", [], ["origin"], ["origin", 42], ["", "id"]])
def test_malformed_video_authority_cannot_be_restored_as_manual(
    monkeypatch: pytest.MonkeyPatch, authority: Any,
) -> None:
    from opensquilla.provider import media_jobs

    monkeypatch.setattr(media, "_video_job_sessions", {})
    monkeypatch.setattr(media_jobs, "job_records", lambda _namespace: {
        "synthetic-job": {
            "session_key": "synthetic-session",
            "credential_fingerprint": "synthetic-fingerprint",
            "provider": "tokenrhythm",
            "model": "wan3.0-video",
            "base_url": _BASE,
            "native_job_id": "synthetic-job",
            "account_authority": authority,
        },
    })
    with pytest.raises(ToolError, match="credential binding is invalid"):
        media._video_job_receipt("synthetic-job")


async def _receipt_process(phase: str, directory: Path) -> dict[str, int]:
    storage = SessionStorage(str(directory / "usage.sqlite3"))
    await storage.connect()
    token = current_tool_context.set(ToolContext(
        caller_kind=CallerKind.WEB, is_owner=True,
        session_key="synthetic-session", workspace_dir=str(directory),
    ))
    scope = UsageAccountingScope(
        sink=SessionUsageEventSink(storage),
        context=UsageExecutionContext(
            execution_id="synthetic-execution", agent_run_id="synthetic-run",
            session_id="synthetic-session",
        ),
    )
    config = VideoGenerationConfig(enabled=True, provider="tokenrhythm", primary="wan3.0-video")
    resumed: list[str] = []
    rejected = 0

    async def resume(**kwargs: Any) -> VideoGenerationResult:
        resumed.append(kwargs["job_id"])
        return VideoGenerationResult(
            job_id=kwargs["job_id"], output_path=kwargs["output_path"], model=kwargs["model"],
            bytes_written=12, provider="tokenrhythm",
        )

    async def delivered(result: VideoGenerationResult, **_kwargs: Any) -> str:
        return json.dumps({"status": "ok", "job_id": result.job_id})

    async def no_billing(**_kwargs: Any) -> None:
        # This test covers task identity/delivery; billing has separate ledger tests.
        pass

    try:
        with bind_usage_accounting_scope(scope), pytest.MonkeyPatch.context() as patches:
            patches.setattr(media, "_video_adapter", lambda _provider: (None, resume))
            patches.setattr(media, "_video_result_payload", delivered)
            patches.setattr(
                tokenrhythm_video_generation, "refresh_tokenrhythm_video_billing", no_billing,
            )
            for source in ("account", "manual"):
                authority = _AUTHORITY if source == "account" else None
                for completed in (False, True):
                    job_id = f"synthetic-{source}-{'completed' if completed else 'pending'}"
                    if phase == "write":
                        media._remember_video_job(
                            job_id, _LEASES[0] if authority else _MANUAL_KEY,
                            "tokenrhythm", config.primary, _BASE, account_authority=authority,
                        )
                        if completed:
                            media._remember_video_completion(
                                job_id, None, json.dumps({"status": "ok", "job_id": job_id}),
                            )
                        record = job_record("handles", job_id)
                        assert record is not None
                        assert _MANUAL_KEY not in json.dumps(record)
                        assert _LEASES[0] not in json.dumps(record)
                        if source == "manual":
                            # Records written before account support have no authority field.
                            record.pop("account_authority", None)
                            job_record("handles", job_id, record)
                        continue

                    assert phase == "read"
                    if authority is not None:
                        for foreign in (
                            (_AUTHORITY[0], "synthetic-other-account"),
                            ("https://other.invalid", _AUTHORITY[1]),
                            None,
                        ):
                            candidate = VideoGenerationCredential(
                                available=True, api_key=_LEASES[1], account_authority=foreign,
                            )
                            patches.setattr(
                                media, "_video_request_config", lambda: (config, candidate),
                            )
                            with pytest.raises(ToolError, match="unavailable in this session"):
                                await media.video_status(job_id)
                            rejected += 1
                    candidate = VideoGenerationCredential(
                        available=True, api_key=_LEASES[1] if authority else _MANUAL_KEY,
                        account_authority=authority,
                    )
                    patches.setattr(media, "_video_request_config", lambda: (config, candidate))
                    result = json.loads(await media.video_status(job_id))
                    assert result == {"status": "ok", "job_id": job_id}
                    receipt = media._video_job_receipt(job_id)
                    assert receipt is not None and receipt.account_authority == authority
            return {"saved": 4} if phase == "write" else {
                "restored": 4, "resumed": len(resumed), "rejected": rejected,
            }
    finally:
        current_tool_context.reset(token)
        await storage.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_receipt_process(sys.argv[1], Path(sys.argv[2])))))
