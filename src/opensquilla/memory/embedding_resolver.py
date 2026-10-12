from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .embedding import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    NullEmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAIEmbeddingProvider,
)

ProviderName = Literal["none", "local", "openai", "ollama"]


@dataclass(frozen=True)
class MemoryEmbeddingDecision:
    requested_provider: str
    effective_provider: ProviderName
    model: str
    fingerprint: str
    reason: str | None = None
    dimensions: int | None = None
    local_onnx_dir: str | None = None
    remote_api_key: str | None = None
    remote_base_url: str | None = None
    remote_headers: dict[str, str] = field(default_factory=dict)
    ollama_base_url: str | None = None


def _normalise_provider(provider: str | None) -> str:
    value = (provider or "auto").strip().lower()
    return "openai" if value == "openai-compatible" else value


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = value.strip() if isinstance(value, str) else str(value).strip()
    return cleaned or None


def _resolve_user_path(value: Any) -> str | None:
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return str(Path(cleaned).expanduser().resolve())


def _fingerprint(parts: dict[str, Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret_fingerprint(value: str | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def local_bge_available(model: str, onnx_dir: str | None = None) -> bool:
    """Return whether auto mode may safely pick the local ONNX backend."""
    resolved: Path | None
    if onnx_dir:
        resolved = Path(onnx_dir).expanduser().resolve()
    else:
        resolved = LocalEmbeddingProvider._bundled_onnx_dir(model)
    return (
        resolved is not None
        and resolved.is_dir()
        and any(resolved.glob("*.onnx"))
        and importlib.util.find_spec("onnxruntime") is not None
        and importlib.util.find_spec("tokenizers") is not None
    )


def resolve_memory_embedding(
    memory_config: Any,
    *,
    local_available: Callable[[str, str | None], bool] | None = None,
) -> MemoryEmbeddingDecision:
    """Resolve memory embedding config into a pure provider decision.

    This function deliberately does not instantiate provider clients. It keeps
    config interpretation testable and separate from runtime resources.
    """
    retrieval_mode = getattr(memory_config, "retrieval_mode", "hybrid")
    embed_cfg = getattr(memory_config, "embedding", None)
    local_available = local_available or local_bge_available
    requested = _normalise_provider(
        getattr(embed_cfg, "requested_provider", None)
        if embed_cfg is not None
        else "auto"
    )

    top_model = _clean_str(getattr(embed_cfg, "model", None))
    legacy_dimensions = getattr(embed_cfg, "dimensions", None)
    local_cfg = getattr(embed_cfg, "local", None)
    remote_cfg = getattr(embed_cfg, "remote", None)
    ollama_cfg = getattr(embed_cfg, "ollama", None)

    local_model = LocalEmbeddingProvider.DEFAULT_MODEL
    local_onnx_dir = _resolve_user_path(getattr(local_cfg, "onnx_dir", None))

    remote_model = (
        _clean_str(getattr(remote_cfg, "model", None))
        or top_model
        or OpenAIEmbeddingProvider.DEFAULT_MODEL
    )
    remote_api_key_env = _clean_str(getattr(remote_cfg, "api_key_env", None))
    remote_api_key = (
        _clean_str(getattr(remote_cfg, "api_key", None))
        or _clean_str(getattr(embed_cfg, "api_key", None))
        or (os.environ.get(remote_api_key_env) if remote_api_key_env else None)
    )
    remote_base_url = (
        _clean_str(getattr(remote_cfg, "base_url", None))
        or _clean_str(getattr(embed_cfg, "base_url", None))
        or "https://api.openai.com/v1"
    )
    remote_headers = dict(getattr(remote_cfg, "headers", None) or {})
    remote_dimensions = getattr(remote_cfg, "dimensions", None) or legacy_dimensions

    ollama_model = (
        _clean_str(getattr(ollama_cfg, "model", None))
        or top_model
        or OllamaEmbeddingProvider.DEFAULT_MODEL
    )
    ollama_base_url = _clean_str(getattr(ollama_cfg, "base_url", None)) or "http://localhost:11434"

    if retrieval_mode == "fts_only" or requested == "none":
        return _decision(
            requested,
            "none",
            "fts-only",
            reason="fts_only" if retrieval_mode == "fts_only" else "disabled",
        )

    if requested == "local":
        return _decision(
            requested,
            "local",
            local_model,
            local_onnx_dir=local_onnx_dir,
        )

    if requested == "openai":
        if not remote_api_key:
            raise ValueError(
                "memory.embedding.provider='openai' requires "
                "memory.embedding.remote.api_key"
            )
        return _decision(
            requested,
            "openai",
            remote_model,
            dimensions=remote_dimensions,
            remote_api_key=remote_api_key,
            remote_base_url=remote_base_url,
            remote_headers=remote_headers,
        )

    if requested == "ollama":
        return _decision(
            requested,
            "ollama",
            ollama_model,
            ollama_base_url=ollama_base_url,
        )

    # Auto never consumes [llm] credentials. It is local-first to avoid
    # surprising network calls; set provider="openai" for intentional remote
    # embedding even when local BGE is available.
    if local_available(local_model, local_onnx_dir):
        return _decision(
            requested,
            "local",
            local_model,
            local_onnx_dir=local_onnx_dir,
        )
    if remote_api_key:
        return _decision(
            requested,
            "openai",
            remote_model,
            dimensions=remote_dimensions,
            remote_api_key=remote_api_key,
            remote_base_url=remote_base_url,
            remote_headers=remote_headers,
        )
    return _decision(requested, "none", "fts-only", reason="local_unavailable")


def create_embedding_provider(decision: MemoryEmbeddingDecision) -> EmbeddingProvider:
    provider: EmbeddingProvider
    if decision.effective_provider == "local":
        provider = LocalEmbeddingProvider(
            model_name=decision.model,
            onnx_dir=decision.local_onnx_dir or "auto",
        )
    elif decision.effective_provider == "openai":
        provider = OpenAIEmbeddingProvider(
            api_key=decision.remote_api_key or "",
            base_url=decision.remote_base_url or "https://api.openai.com/v1",
            model=decision.model,
            extra_headers=decision.remote_headers,
            dimensions=decision.dimensions,
        )
    elif decision.effective_provider == "ollama":
        provider = OllamaEmbeddingProvider(
            model=decision.model,
            base_url=decision.ollama_base_url or "http://localhost:11434",
        )
    else:
        provider = NullEmbeddingProvider()
    setattr(provider, "_provider_fingerprint", decision.fingerprint)
    if decision.dimensions is not None:
        setattr(provider, "_vector_dims", decision.dimensions)
    return provider


def _decision(
    requested_provider: str,
    effective_provider: ProviderName,
    model: str,
    *,
    reason: str | None = None,
    dimensions: int | None = None,
    local_onnx_dir: str | None = None,
    remote_api_key: str | None = None,
    remote_base_url: str | None = None,
    remote_headers: dict[str, str] | None = None,
    ollama_base_url: str | None = None,
) -> MemoryEmbeddingDecision:
    fingerprint = _fingerprint(
        {
            "provider": effective_provider,
            "model": model,
            "dimensions": dimensions,
            "local_onnx_dir": str(Path(local_onnx_dir).resolve()) if local_onnx_dir else "",
            "remote_base_url": (remote_base_url or "").rstrip("/"),
            "remote_api_key": _secret_fingerprint(remote_api_key),
            "remote_headers": remote_headers or {},
            "ollama_base_url": (ollama_base_url or "").rstrip("/"),
        }
    )
    return MemoryEmbeddingDecision(
        requested_provider=requested_provider,
        effective_provider=effective_provider,
        model=model,
        fingerprint=fingerprint,
        reason=reason,
        dimensions=dimensions,
        local_onnx_dir=local_onnx_dir,
        remote_api_key=remote_api_key,
        remote_base_url=remote_base_url,
        remote_headers=dict(remote_headers or {}),
        ollama_base_url=ollama_base_url,
    )
