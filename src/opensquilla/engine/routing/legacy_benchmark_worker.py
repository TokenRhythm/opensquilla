"""Diagnostic, result-blind replay of the shipped SquillaRouter production step.

Unlike the registered ``four_tier_mapping`` worker, this entry executes
``apply_squilla_router`` with the bundled V4 Phase 3 model and all its policy
stages. It never constructs a Provider or dispatches a downstream request.
Only independent, empty-state text inputs are supported in this version;
unsupported context is rejected, never silently discarded.

    python -m opensquilla.engine.routing.legacy_benchmark_worker --request request.json

``evidence.json`` is the success marker and is published last, without replacing
an existing file. This is diagnostic provenance, not native release attestation
or an operating-system security sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from opensquilla.engine.routing.benchmark_worker import (
    _iter_input_rows,
    _strict_json_loads,
    _validate_input_bundle,
    canonical_json_bytes,
)

REQUEST_SCHEMA = "legacy-squilla-request.v1"
EVIDENCE_SCHEMA = "legacy-squilla-evidence.v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIERS = ("C0", "C1", "C2", "C3")
_MODELS = (
    "qwen/qwen3.7-flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.3",
)
_REVISIONS = (
    "qwen3.7-flash-thinking",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro-0813",
    "glm-5.3",
)
_ASSETS = frozenset(
    {
        "bge_onnx/config.json",
        "bge_onnx/model.onnx",
        "bge_onnx/special_tokens_map.json",
        "bge_onnx/tokenizer.json",
        "bge_onnx/tokenizer_config.json",
        "bge_onnx/vocab.txt",
        "features/bge_pca.joblib",
        "features/config.pkl",
        "features/meta.json",
        "features/svd.pkl",
        "features/tfidf.pkl",
        "inference_manifest.json",
        "lgbm_aux.bin",
        "lgbm_main.bin",
        "mlp/model.onnx",
        "mlp/scaler.joblib",
        "router.runtime.yaml",
        "version.json",
    }
)
_OFFLINE_ENV = {
    "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


class LegacyReplayError(RuntimeError):
    """The legacy replay cannot produce complete, native diagnostic evidence."""


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["legacy-squilla-request.v1"]
    input_path: str
    input_sha256: str
    model_pool_path: str
    model_pool_sha256: str
    output_dir: str
    bundle_dir: str | None = None


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_file(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise LegacyReplayError("artifact path must be absolute and not a symlink")
    try:
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise LegacyReplayError("artifact path is not a regular file")
    except OSError as exc:
        raise LegacyReplayError("artifact path is unavailable") from exc
    return resolved


def _verified_bytes(path: str | Path, expected: str, *, maximum: int) -> bytes:
    if not _SHA256.fullmatch(expected):
        raise LegacyReplayError("expected hash must use sha256:<64 lowercase hex> format")
    resolved = _regular_file(path)
    if not 0 < resolved.stat().st_size <= maximum:
        raise LegacyReplayError("artifact size is outside the allowed range")
    payload = resolved.read_bytes()
    if len(payload) > maximum or _sha256(payload) != expected:
        raise LegacyReplayError("artifact SHA-256 mismatch")
    return payload


def _load_inputs(payload: bytes) -> list[Any]:
    stream = io.BytesIO(payload)
    _validate_input_bundle(stream, routing_session_mode="independent")
    stream.seek(0)
    rows = list(_iter_input_rows(stream, routing_session_mode="independent"))
    for row in rows:
        value = row.input
        unsupported = [
            field
            for field in (
                "task_anchor",
                "history_user",
                "previous_answer",
                "previous_usage",
                "route_history",
                "context",
                "tool_state",
                "attachments",
            )
            if getattr(value, field)
        ]
        if value.active_route_tier is not None:
            unsupported.append("active_route_tier")
        if value.previous_outcome != "unknown":
            unsupported.append("previous_outcome")
        if unsupported:
            raise LegacyReplayError(
                f"item {row.item_id}: unsupported nonempty legacy context: {unsupported}"
            )
    return rows


def _load_pool(payload: bytes) -> dict[str, dict[str, str]]:
    value = _strict_json_loads(payload, label="legacy model pool")
    if not isinstance(value, dict) or set(value) != set(_TIERS):
        raise LegacyReplayError("model pool must contain exactly C0-C3")
    for index, tier in enumerate(_TIERS):
        identity = value[tier]
        if not isinstance(identity, dict) or set(identity) != {
            "model_id",
            "revision",
            "definition_hash",
        }:
            raise LegacyReplayError("model pool identity has unexpected fields")
        definition = {
            "provider": "openrouter",
            "model": _MODELS[index],
            "reasoning": "thinking" if index == 0 else "max",
            "deployment_version": _REVISIONS[index],
        }
        if identity != {
            "model_id": _MODELS[index],
            "revision": _REVISIONS[index],
            "definition_hash": _sha256(canonical_json_bytes(definition)),
        }:
            raise LegacyReplayError(f"model pool {tier} differs from the fixed replay deployment")
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _bundle_identity(bundle: Path) -> dict[str, Any]:
    manifest_path = _regular_file(bundle / "artifact_manifest.json")
    manifest_payload = manifest_path.read_bytes()
    manifest = _strict_json_loads(manifest_payload, label="legacy bundle manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise LegacyReplayError("unsupported legacy bundle manifest")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != len(_ASSETS):
        raise LegacyReplayError("legacy bundle manifest has incomplete asset coverage")
    verified: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise LegacyReplayError("legacy bundle manifest entry is not an object")
        relative = entry.get("path")
        if relative not in _ASSETS or relative in verified:
            raise LegacyReplayError("legacy bundle manifest has unknown or duplicate asset")
        path = _regular_file(bundle / relative)
        if not path.is_relative_to(bundle):
            raise LegacyReplayError("legacy model asset escaped the bundle")
        payload = path.read_bytes()
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise LegacyReplayError("legacy bundle manifest has an invalid asset hash")
        size = entry.get("size_bytes")
        if type(size) is not int or len(payload) != size:
            raise LegacyReplayError(f"legacy asset size mismatch: {relative}")
        if payload.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise LegacyReplayError(f"legacy asset is a Git LFS pointer: {relative}")
        if _sha256(payload) != "sha256:" + expected:
            raise LegacyReplayError(f"legacy asset SHA-256 mismatch: {relative}")
        verified[relative] = "sha256:" + expected
    runtime_sources = {}
    for path in sorted((bundle / "runtime_src").rglob("*.py")):
        source = _regular_file(path)
        if not source.is_relative_to(bundle):
            raise LegacyReplayError("legacy runtime source escaped the bundle")
        runtime_sources[path.relative_to(bundle).as_posix()] = _sha256(source.read_bytes())
    if not runtime_sources or not (bundle / "runtime_src/src/router/inference/core.py").is_file():
        raise LegacyReplayError("legacy runtime source is missing")
    return {
        "bundle_manifest_path": str(manifest_path),
        "bundle_manifest_sha256": _sha256(manifest_payload),
        "asset_hashes": verified,
        "bundle_source_hashes": runtime_sources,
    }


def _source_hashes() -> dict[str, str]:
    # Freeze the package source and resource superset, not an inferred minimal
    # import list that could omit lazy production policy/catalog dependencies.
    root = _repo_root()
    result = {}
    for path in sorted((root / "src/opensquilla").rglob("*")):
        if path.suffix in {".py", ".json", ".toml", ".yaml", ".yml"} and path.is_file():
            source = _regular_file(path)
            if not source.is_relative_to(root):
                raise LegacyReplayError("OpenSquilla source escaped its repository")
            result[path.relative_to(root).as_posix()] = _sha256(source.read_bytes())
    if not result:
        raise LegacyReplayError("OpenSquilla source identity is empty")
    return result


class _NetworkGuard:
    def __init__(self) -> None:
        self.enabled = False
        self.attempts: list[str] = []
        sys.addaudithook(self._audit)

    def _audit(self, event: str, _args: tuple[Any, ...]) -> None:
        if self.enabled and event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.sendto",
            "subprocess.Popen",
            "os.system",
        }:
            self.attempts.append(event)
            raise LegacyReplayError("legacy route-only worker forbids network and subprocesses")

    @contextmanager
    def active(self) -> Iterator[None]:
        previous = {name: os.environ.get(name) for name in _OFFLINE_ENV}
        os.environ.update(_OFFLINE_ENV)
        self.enabled = True
        try:
            yield
        finally:
            self.enabled = False
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _configuration(bundle: Path) -> Any:
    from opensquilla.gateway.config import (
        RouterBudgetConfig,
        RouterSelfLearningConfig,
        SquillaRouterConfig,
    )

    # model_construct avoids BaseSettings/environment overrides. All defaults
    # are serialized into evidence; only the declared offline controls differ.
    config = SquillaRouterConfig.model_construct(
        enabled=True,
        strategy="v4_phase3",
        rollout_phase="full",
        v4_bundle_dir=str(bundle),
        v4_use_aux_head=True,
        require_router_runtime=True,
        calibration_enabled=False,
        self_learning=RouterSelfLearningConfig(enabled=False, capture_enabled=False),
        budget=RouterBudgetConfig(limit_usd=None),
    )
    config.tiers = {tier.lower(): dict(config.tiers[tier.lower()]) for tier in _TIERS}
    for index, tier in enumerate(_TIERS):
        if config.tiers[tier.lower()].get("model") != _MODELS[index]:
            raise LegacyReplayError("production default text ladder changed")
    return config


async def _route(rows: list[Any], pool: dict[str, Any], bundle: Path) -> tuple[list[dict], dict]:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.steps import squilla_router as step
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

    config = _configuration(bundle)
    step.invalidate_strategy_cache()
    step._history_store.clear()
    set_shared_catalog(ModelCatalog())  # Packaged cold facts, no live/user catalog injection.
    strategy = step.preload_strategy(config)
    if strategy.source != "v4_phase3" or getattr(strategy, "_available", False) is not True:
        raise LegacyReplayError(
            "legacy native runtime did not load; heuristic fallback is forbidden"
        )
    if strategy._core.aux_model is None:
        raise LegacyReplayError("production legacy aux head was not loaded")
    decisions = []
    for row in rows:
        text = row.input.current_request
        ctx = TurnContext(
            message=text,
            raw_message=text,
            session_key="legacy-replay:" + row.item_id,
            config=SimpleNamespace(
                squilla_router=config,
                llm=SimpleNamespace(
                    provider="openrouter", model=_MODELS[1], context_window_tokens=0
                ),
            ),
            provider=None,
            model=_MODELS[1],
            tool_defs=[],
            system_prompt="",
            attachments=[],
            metadata={
                "router_history_user_texts": [],
                "router_prev_assistant_text": "",
                "router_prev_assistant_usage": {},
                "_defer_squilla_router_history": True,
            },
        )
        ctx = await step.apply_squilla_router(ctx)
        info = ctx.metadata
        tier = str(info.get("routed_tier", "")).upper()
        confidence = info.get("routing_confidence")
        if (
            info.get("routing_source") != "v4_phase3"
            or tier not in pool
            or info.get("routed_model") != pool[tier]["model_id"]
            or ctx.model != pool[tier]["model_id"]
            or info.get("routing_applied") is not True
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise LegacyReplayError(f"item {row.item_id}: native production decision is invalid")
        trace = {
            "final_tier": tier,
            "final_model_id": pool[tier]["model_id"],
            "routing_source": info["routing_source"],
            "entrypoint": "opensquilla.engine.steps.squilla_router.apply_squilla_router",
            "mode": "legacy_squilla_router",
            "routing_extra": info.get("routing_extra"),
            "thinking_mode": info.get("thinking_mode"),
            "prompt_policy": info.get("prompt_policy"),
            "thinking_level": info.get("thinking_level"),
            "thinking_requested": info.get("thinking_requested", False),
            "effective_message": ctx.message,
            "prompt_modified": ctx.message != text,
            "routed_provider": info.get("routed_provider"),
            "routing_applied": info["routing_applied"],
            "router_fallback_chain": info.get("router_fallback_chain", []),
            "input_row_sha256": row.row_sha256,
        }
        decisions.append(
            {
                "item_id": row.item_id,
                "tier": tier,
                "model_id": pool[tier]["model_id"],
                "routing_source": info["routing_source"],
                "confidence": confidence,
                "trace": trace,
            }
        )
    return decisions, {
        "model_version": strategy._model_version,
        "feature_schema_version": strategy._feature_schema_version,
        "aux_head_loaded": True,
        "config": {
            "squilla_router": config.model_dump(mode="json"),
            "llm": {"provider": "openrouter", "model": _MODELS[1], "context_window_tokens": 0},
            "bundle_runtime_settings": strategy._config,
            "offline_environment": dict(_OFFLINE_ENV),
            "model_catalog": "cold_packaged_without_live_or_user_overrides",
            "input_scope": "independent_empty_state_text_only",
        },
    }


def _publish(path: Path, payload: bytes) -> None:
    # Linking a completed temporary file publishes atomically without any
    # overwrite window (os.replace would overwrite an unexpected existing file).
    fd, temporary = tempfile.mkstemp(prefix=".legacy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_request(request_path: str | Path) -> dict[str, Any]:
    path = _regular_file(request_path)
    if path.stat().st_size > 64 * 1024:
        raise LegacyReplayError("legacy request is too large")
    request = _Request.model_validate(_strict_json_loads(path.read_bytes(), label="legacy request"))
    input_payload = _verified_bytes(request.input_path, request.input_sha256, maximum=512 * 1024**2)
    pool_payload = _verified_bytes(
        request.model_pool_path, request.model_pool_sha256, maximum=65536
    )
    rows, pool = _load_inputs(input_payload), _load_pool(pool_payload)
    output = Path(request.output_dir)
    if not output.is_absolute() or output.is_symlink():
        raise LegacyReplayError("output_dir must be absolute and not a symlink")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir() or any(output.iterdir()):
        raise LegacyReplayError("output_dir must be empty; successful evidence is immutable")
    lock = output / ".legacy-worker.lock"
    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(lock_fd)
    guard = _NetworkGuard()
    try:
        with guard.active():
            from opensquilla.squilla_router.v4_phase3 import default_bundle_dir

            bundle = Path(request.bundle_dir) if request.bundle_dir else default_bundle_dir()
            if not bundle.is_absolute() or bundle.is_symlink():
                raise LegacyReplayError("bundle_dir must be absolute and not a symlink")
            bundle = bundle.resolve(strict=True)
            identity = _bundle_identity(bundle)
            sources = _source_hashes()
            decisions, runtime = asyncio.run(_route(rows, pool, bundle))
            if guard.attempts:
                raise LegacyReplayError(
                    "native replay attempted a forbidden network/subprocess call"
                )
            if _bundle_identity(bundle) != identity or _source_hashes() != sources:
                raise LegacyReplayError("legacy runtime source or model changed during replay")
            decisions_payload = b"".join(canonical_json_bytes(row) + b"\n" for row in decisions)
            evidence = {
                "schema_version": EVIDENCE_SCHEMA,
                "input_sha256": request.input_sha256,
                "model_pool_sha256": request.model_pool_sha256,
                "decisions_sha256": _sha256(decisions_payload),
                "decision_count": len(decisions),
                "model_identity": {
                    **identity,
                    **{k: v for k, v in runtime.items() if k != "config"},
                },
                "bundle_manifest_path": identity["bundle_manifest_path"],
                "source_hashes": sources,
                "source_hash_scope": "repository_package_python_and_runtime_resources",
                "config": runtime["config"],
                "environment": {
                    "python": sys.version,
                    "packages": {
                        name: metadata.version(name)
                        for name in (
                            "numpy",
                            "lightgbm",
                            "onnxruntime",
                            "joblib",
                            "scikit-learn",
                            "tokenizers",
                        )
                    },
                },
                "downstream_dispatch_count": 0,
                "network_attempts": list(guard.attempts),
                "diagnostic_only": True,
                "result_blind": True,
                "controller_effects_replayed": False,
            }
            _publish(output / "outputs.jsonl", decisions_payload)
            _publish(output / "evidence.json", canonical_json_bytes(evidence))
            return evidence
    finally:
        lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args(argv)
    import structlog

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(30),
    )
    try:
        run_request(arguments.request)
    except Exception as exc:  # noqa: BLE001 - process boundary reports failure, never fake results.
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
