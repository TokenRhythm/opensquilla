from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.gateway.config import GatewayConfig
from opensquilla.memory.embedding import (
    LocalEmbeddingProvider,
    NullEmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from opensquilla.memory.embedding_resolver import local_bge_available, resolve_memory_embedding
from opensquilla.memory.meta import MemoryIndexMeta
from opensquilla.memory.store import LongTermMemoryStore

_LOCAL_AVAILABLE_PATH = "opensquilla.memory.embedding_resolver.local_bge_available"


class _FakeStore:
    providers: list[object] = []

    def __init__(self, db_path, embedding_provider=None, query_embedding_cache_mode="on"):
        self.db_path = db_path
        self.embedding_provider = embedding_provider
        self.query_embedding_cache_mode = query_embedding_cache_mode
        self.closed = False
        _FakeStore.providers.append(embedding_provider)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeSyncManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _patch_manager_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakeStore.providers = []
    monkeypatch.setattr("opensquilla.memory.store.LongTermMemoryStore", _FakeStore)
    monkeypatch.setattr("opensquilla.memory.sync_manager.MemorySyncManager", _FakeSyncManager)
    monkeypatch.setattr("opensquilla.agents.scope.maybe_migrate_legacy_memory", lambda *_: None)
    monkeypatch.setattr(
        "opensquilla.agents.scope.resolve_agent_memory_db",
        lambda agent_id, state_dir: tmp_path / "state" / agent_id / "memory.db",
    )
    monkeypatch.setattr(
        "opensquilla.agents.scope.resolve_agent_workspace_dir",
        lambda agent_id, config: tmp_path / "workspace" / agent_id,
    )
    monkeypatch.setattr(
        "opensquilla.agents.scope.resolve_agent_data_dir",
        lambda agent_id, state_dir=None: (
            (Path(state_dir) if state_dir is not None else tmp_path / "data") / "agents" / agent_id
        ),
    )
    monkeypatch.setattr(
        "opensquilla.agents.scope.resolve_agent_memory_dir",
        lambda agent_id, state_dir=None: (
            (Path(state_dir) if state_dir is not None else tmp_path / "memory")
            / "agents"
            / agent_id
            / "memory"
        ),
    )


async def _build_one(config: GatewayConfig, monkeypatch, tmp_path, *, session_storage=None):
    from opensquilla.memory.manager import build_memory_managers

    _patch_manager_dependencies(monkeypatch, tmp_path)
    managers = await build_memory_managers(
        config,
        ["main"],
        session_storage=session_storage,
    )
    try:
        assert len(_FakeStore.providers) == 1
        return _FakeStore.providers[0], managers
    except Exception:
        for manager in managers.values():
            await manager.close()
        raise


@pytest.mark.asyncio
async def test_build_memory_leaves_session_source_indexer_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    _provider, managers = await _build_one(
        GatewayConfig(),
        monkeypatch,
        tmp_path,
        session_storage=object(),
    )
    try:
        assert managers["main"].sync_manager.kwargs["session_indexer"] is None
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_enables_session_source_indexer_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    _provider, managers = await _build_one(
        GatewayConfig(memory={"session_source_enabled": True}),
        monkeypatch,
        tmp_path,
        session_storage=object(),
    )
    try:
        assert managers["main"].sync_manager.kwargs["session_indexer"] is not None
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_state_memory_source_uses_configured_external_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    external_state = tmp_path / "external-state"
    _provider, managers = await _build_one(
        GatewayConfig(
            state_dir=str(external_state),
            memory={"source": "state"},
        ),
        monkeypatch,
        tmp_path,
    )
    try:
        manager = managers["main"]
        assert manager.memory_dir == external_state / "agents" / "main" / "memory"
        assert manager.workspace_dir == external_state / "agents" / "main"
        assert os.path.samefile(
            manager.sync_manager.kwargs["memory_dir"],
            external_state / "agents" / "main" / "memory",
        )
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
async def test_extended_length_state_keeps_memory_sync_and_capture_operational(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.memory.manager import _native_io_path, build_memory_managers

    monkeypatch.setattr(
        "opensquilla.agents.scope.maybe_migrate_legacy_memory",
        lambda *_: None,
    )
    long_root = tmp_path / "long-state"
    state_dir = long_root
    index = 0
    while len(str(state_dir / "agents" / "main" / "memory" / "memory.db")) < 300:
        state_dir /= f"state-segment-{index:02d}-0123456789"
        index += 1
    workspace = state_dir / "agents" / "main"
    memory_dir = workspace / "memory"
    os.makedirs(_native_io_path(memory_dir))
    (_native_io_path(workspace) / "MEMORY.md").write_text(
        "# Memory\n\npersistent fact\n",
        encoding="utf-8",
    )
    (_native_io_path(memory_dir) / "topic.md").write_text(
        "# Topic\n\nindexed fact\n",
        encoding="utf-8",
    )
    config = GatewayConfig(
        state_dir=str(state_dir),
        memory={
            "source": "state",
            "retrieval_mode": "fts_only",
            "sync_interval_minutes": 0.0,
            "ttl_sweep_interval_minutes": 0.0,
            "capture_assistant": True,
        },
    )
    managers = {}
    try:
        managers = await build_memory_managers(config, ["main"])
        manager = managers["main"]

        assert manager.db_path == workspace / "memory.db"
        assert manager.workspace_dir == workspace
        assert manager.memory_dir == memory_dir
        assert not str(manager.db_path).startswith("\\\\?\\")
        assert await manager.store.list_paths() == ["MEMORY.md", "memory/topic.md"]

        rel_path = await manager.capture_turn(
            session_key="agent:main:long-state",
            session_id="long-state-session",
            user_text="remember this",
            assistant_text="remembered",
        )
        assert rel_path is not None
        capture_path = workspace / rel_path
        assert os.path.isfile(_native_io_path(capture_path))
        with open(_native_io_path(capture_path), encoding="utf-8") as handle:
            captured = handle.read()
        assert "### User\nremember this" in captured
        assert "### Assistant\nremembered" in captured

        status = await manager.status()
        assert status["file_count"] == 2
        assert status["chunk_count"] >= 2
        assert status["degraded"] == []
    finally:
        for manager in managers.values():
            await manager.close()
        native_root = _native_io_path(long_root)
        if native_root.exists():
            shutil.rmtree(native_root)


@pytest.mark.asyncio
async def test_memory_manager_reports_effective_local_hybrid_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    provider, managers = await _build_one(GatewayConfig(), monkeypatch, tmp_path)
    try:
        assert isinstance(provider, LocalEmbeddingProvider)
        metadata = managers["main"].effective_retrieval_metadata()
        assert metadata["configured_retrieval_mode"] == "hybrid"
        assert metadata["retrieval_mode"] == "hybrid"
        assert metadata["embedding_requested_provider"] == "auto"
        assert metadata["embedding_effective_provider"] == "local"
        assert metadata["embedding_model"] == "BAAI/bge-small-zh-v1.5"
        assert metadata["vector_weight"] == "0.7"
        assert metadata["text_weight"] == "0.3"
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_memory_manager_reports_effective_fts_only_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    provider, managers = await _build_one(
        GatewayConfig(memory={"retrieval_mode": "fts_only"}),
        monkeypatch,
        tmp_path,
    )
    try:
        assert isinstance(provider, NullEmbeddingProvider)
        metadata = managers["main"].effective_retrieval_metadata()
        assert metadata["configured_retrieval_mode"] == "fts_only"
        assert metadata["retrieval_mode"] == "fts_only"
        assert metadata["embedding_requested_provider"] == "auto"
        assert metadata["embedding_effective_provider"] == "none"
        assert metadata["embedding_model"] == "fts-only"
        assert metadata["vector_weight"] == "0.0"
        assert metadata["text_weight"] == "1.0"
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_memory_manager_reports_explicit_remote_embedding_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    provider, managers = await _build_one(
        GatewayConfig(
            memory={
                "embedding": {
                    "provider": "openai-compatible",
                    "remote": {
                        "api_key": "mem-key",
                        "base_url": "https://embeddings.example/v1",
                        "model": "embed-model",
                    },
                }
            }
        ),
        monkeypatch,
        tmp_path,
    )
    try:
        assert isinstance(provider, OpenAIEmbeddingProvider)
        metadata = managers["main"].effective_retrieval_metadata()
        assert metadata["configured_retrieval_mode"] == "hybrid"
        assert metadata["retrieval_mode"] == "hybrid"
        assert metadata["embedding_requested_provider"] == "openai"
        assert metadata["embedding_effective_provider"] == "openai"
        assert metadata["embedding_model"] == "embed-model"
    finally:
        for manager in managers.values():
            await manager.close()


def test_memory_embedding_provider_wins_over_legacy_mode() -> None:
    cfg = GatewayConfig(memory={"embedding": {"provider": "local", "mode": "openai"}})
    assert cfg.memory.embedding.requested_provider == "local"


def test_memory_embedding_legacy_mode_still_selects_provider() -> None:
    cfg = GatewayConfig(memory={"embedding": {"mode": "local"}})
    assert cfg.memory.embedding.provider == "auto"
    assert cfg.memory.embedding.requested_provider == "local"


def test_memory_embedding_legacy_flat_remote_maps_to_decision() -> None:
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "mode": "openai",
                "api_key": "mem-key",
                "base_url": "https://embeddings.example/v1",
                "model": "embed-model",
            }
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)
    assert decision.effective_provider == "openai"
    assert decision.remote_api_key == "mem-key"
    assert decision.remote_base_url == "https://embeddings.example/v1"
    assert decision.model == "embed-model"


def test_memory_embedding_openai_compatible_provider_maps_to_remote_decision() -> None:
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "openai-compatible",
                "remote": {
                    "api_key": "mem-key",
                    "base_url": "https://embeddings.example/v1",
                    "model": "embed-model",
                    "dimensions": 512,
                },
            }
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: True)
    assert decision.requested_provider == "openai"
    assert decision.effective_provider == "openai"
    assert decision.remote_api_key == "mem-key"
    assert decision.remote_base_url == "https://embeddings.example/v1"
    assert decision.model == "embed-model"
    assert decision.dimensions == 512


def test_memory_embedding_local_onnx_dir_resolves_user_path() -> None:
    expected = str(Path("models/bge-onnx").expanduser().resolve())
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "local",
                "local": {"onnx_dir": "models/bge-onnx"},
            }
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)
    assert decision.effective_provider == "local"
    assert decision.local_onnx_dir == expected


def test_memory_embedding_nested_configs_validate() -> None:
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "ollama",
                "remote": {"api_key": "k", "base_url": "https://e/v1"},
                "local": {"onnx_dir": "models/bge"},
                "ollama": {"base_url": "http://localhost:11434", "model": "nomic"},
            }
        }
    )
    dumped = cfg.memory.embedding.model_dump(mode="python")
    assert dumped["remote"]["api_key"] == "k"
    assert dumped["local"]["onnx_dir"] == "models/bge"
    assert dumped["ollama"]["model"] == "nomic"


def test_resolver_auto_uses_local_when_available_and_reports_fingerprint() -> None:
    cfg = GatewayConfig()
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: True)
    assert decision.requested_provider == "auto"
    assert decision.effective_provider == "local"
    assert decision.model == "BAAI/bge-small-zh-v1.5"
    assert decision.fingerprint
    assert decision.reason is None


def test_resolver_auto_prefers_local_over_memory_remote_key_when_available() -> None:
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "auto",
                "remote": {"api_key": "mem-key", "base_url": "https://embeddings.example/v1"},
            }
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: True)
    assert decision.effective_provider == "local"
    assert decision.model == "BAAI/bge-small-zh-v1.5"
    assert decision.remote_api_key is None


def test_resolver_auto_uses_memory_remote_when_local_unavailable() -> None:
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "auto",
                "remote": {"api_key": "mem-key", "base_url": "https://embeddings.example/v1"},
            }
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)
    assert decision.effective_provider == "openai"
    assert decision.remote_api_key == "mem-key"


def test_resolver_explicit_remote_uses_memory_env_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_EMBEDDINGS_API_KEY", "mem-env-key")
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "openai",
                "remote": {"api_key_env": "OPENAI_EMBEDDINGS_API_KEY"},
            }
        }
    )

    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)

    assert decision.effective_provider == "openai"
    assert decision.remote_api_key == "mem-env-key"


def test_resolver_explicit_remote_requires_memory_api_key() -> None:
    cfg = GatewayConfig(memory={"embedding": {"provider": "openai"}})
    with pytest.raises(ValueError, match="memory.embedding.remote.api_key"):
        resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)


def test_resolver_auto_never_uses_llm_openrouter_key() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "api_key": "or-key",
            "base_url": "https://openrouter.ai/api/v1",
        }
    )
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: False)
    assert decision.effective_provider == "none"
    assert decision.reason == "local_unavailable"


def test_local_bge_available_uses_tokenizers_not_transformers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_dir = tmp_path / "bge_onnx"
    onnx_dir.mkdir()
    (onnx_dir / "model.onnx").write_bytes(b"onnx")

    def fake_find_spec(name: str):
        if name in {"onnxruntime", "tokenizers"}:
            return object()
        if name == "transformers":
            return None
        raise AssertionError(name)

    monkeypatch.setattr(
        "opensquilla.memory.embedding_resolver.importlib.util.find_spec",
        fake_find_spec,
    )

    assert local_bge_available("BAAI/bge-small-zh-v1.5", str(onnx_dir))


def test_resolver_explicit_none_and_fts_only() -> None:
    cfg = GatewayConfig(memory={"embedding": {"provider": "none"}})
    assert resolve_memory_embedding(cfg.memory).effective_provider == "none"
    cfg = GatewayConfig(memory={"retrieval_mode": "fts_only"})
    decision = resolve_memory_embedding(cfg.memory, local_available=lambda *_: True)
    assert decision.effective_provider == "none"


@pytest.mark.asyncio
async def test_build_memory_default_auto_uses_local_bge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    provider, managers = await _build_one(GatewayConfig(), monkeypatch, tmp_path)
    try:
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider.model == "BAAI/bge-small-zh-v1.5"
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_legacy_mode_local_uses_bundled_bge_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        memory={"embedding": {"mode": "local"}},
    )
    provider, managers = await _build_one(config, monkeypatch, tmp_path)
    try:
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider.provider_id == "local"
        assert provider.model == "BAAI/bge-small-zh-v1.5"
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_openrouter_llm_key_still_uses_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: True)
    config = GatewayConfig(llm={"provider": "openrouter", "api_key": "or-key"})
    provider, managers = await _build_one(config, monkeypatch, tmp_path)
    try:
        assert isinstance(provider, LocalEmbeddingProvider)
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_auto_fts_when_local_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_LOCAL_AVAILABLE_PATH, lambda *_: False)
    provider, managers = await _build_one(
        GatewayConfig(llm={"api_key": "llm-key"}),
        monkeypatch,
        tmp_path,
    )
    try:
        assert isinstance(provider, NullEmbeddingProvider)
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_explicit_remote_uses_openai_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = GatewayConfig(
        memory={
            "embedding": {
                "provider": "openai",
                "remote": {
                    "api_key": "mem-key",
                    "base_url": "https://embeddings.example/v1",
                    "dimensions": 512,
                },
                "model": "embed",
            }
        }
    )
    provider, managers = await _build_one(config, monkeypatch, tmp_path)
    try:
        assert isinstance(provider, OpenAIEmbeddingProvider)
        assert provider.model == "embed"
        assert getattr(provider, "_base_url") == "https://embeddings.example/v1"
        assert getattr(provider, "_dimensions") == 512
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_explicit_ollama_uses_ollama_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = GatewayConfig(
        memory={"embedding": {"provider": "ollama", "ollama": {"model": "nomic-x"}}}
    )
    provider, managers = await _build_one(config, monkeypatch, tmp_path)
    try:
        assert isinstance(provider, OllamaEmbeddingProvider)
        assert provider.model == "nomic-x"
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_build_memory_local_onnx_dir_passed_to_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_dir = tmp_path / "onnx"
    config = GatewayConfig(
        memory={"embedding": {"provider": "local", "local": {"onnx_dir": str(onnx_dir)}}}
    )
    provider, managers = await _build_one(config, monkeypatch, tmp_path)
    try:
        assert isinstance(provider, LocalEmbeddingProvider)
        assert getattr(provider, "_onnx_dir") == onnx_dir
    finally:
        for manager in managers.values():
            await manager.close()


class _FakeEmbeddingProvider:
    def __init__(self, *, fingerprint: str = "fp-new", dims: int | None = None) -> None:
        self._provider_fingerprint = fingerprint
        if dims is not None:
            self._vector_dims = dims

    @property
    def provider_id(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return "BAAI/bge-small-zh-v1.5"

    async def embed_query(self, text: str) -> list[float]:
        return [0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    async def probe(self) -> tuple[bool, str | None]:
        return True, None


@pytest.mark.asyncio
async def test_memory_provider_fingerprint_change_drops_old_vec_table() -> None:
    store = LongTermMemoryStore(
        ":memory:",
        embedding_provider=_FakeEmbeddingProvider(fingerprint="fp-new"),
    )
    store._db = await aiosqlite.connect(":memory:")  # type: ignore[assignment]
    try:
        await store._ensure_schema()
        store._vec_available = True
        old_meta = MemoryIndexMeta(
            model="BAAI/bge-small-zh-v1.5",
            provider="local",
            chunk_tokens=400,
            chunk_overlap=50,
            vector_dims=None,
            fts_tokenizer="unicode61",
            sources=["memory"],
            provider_fingerprint="fp-old",
        )
        await store._db.execute(  # type: ignore[union-attr]
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("memory_provider_meta", old_meta.to_json()),
        )
        await store._db.execute("CREATE TABLE chunks_vec(id TEXT PRIMARY KEY)")  # type: ignore[union-attr]
        await store._db.commit()  # type: ignore[union-attr]

        await store._check_meta_and_reindex()

        async with store._db.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE name='chunks_vec'"
        ) as cur:
            assert await cur.fetchone() is None
    finally:
        await store._db.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_memory_provider_or_model_change_drops_old_vec_table() -> None:
    store = LongTermMemoryStore(
        ":memory:",
        embedding_provider=_FakeEmbeddingProvider(),
    )
    store._db = await aiosqlite.connect(":memory:")  # type: ignore[assignment]
    try:
        await store._ensure_schema()
        store._vec_available = True
        old_meta = MemoryIndexMeta(
            model="text-embedding-3-small",
            provider="openai",
            chunk_tokens=400,
            chunk_overlap=50,
            vector_dims=None,
            fts_tokenizer="unicode61",
            sources=["memory"],
        )
        await store._db.execute(  # type: ignore[union-attr]
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("memory_provider_meta", old_meta.to_json()),
        )
        await store._db.execute("CREATE TABLE chunks_vec(id TEXT PRIMARY KEY)")  # type: ignore[union-attr]
        await store._db.commit()  # type: ignore[union-attr]

        await store._check_meta_and_reindex()

        async with store._db.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE name='chunks_vec'"
        ) as cur:
            assert await cur.fetchone() is None
    finally:
        await store._db.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_memory_vector_dimension_change_drops_old_vec_table() -> None:
    store = LongTermMemoryStore(
        ":memory:",
        embedding_provider=_FakeEmbeddingProvider(fingerprint="fp", dims=768),
    )
    store._db = await aiosqlite.connect(":memory:")  # type: ignore[assignment]
    try:
        await store._ensure_schema()
        store._vec_available = True
        old_meta = MemoryIndexMeta(
            model="BAAI/bge-small-zh-v1.5",
            provider="local",
            chunk_tokens=400,
            chunk_overlap=50,
            vector_dims=512,
            fts_tokenizer="unicode61",
            sources=["memory"],
            provider_fingerprint="fp",
        )
        await store._db.execute(  # type: ignore[union-attr]
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("memory_provider_meta", old_meta.to_json()),
        )
        await store._db.execute("CREATE TABLE chunks_vec(id TEXT PRIMARY KEY)")  # type: ignore[union-attr]
        await store._db.commit()  # type: ignore[union-attr]

        await store._check_meta_and_reindex()

        async with store._db.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE name='chunks_vec'"
        ) as cur:
            assert await cur.fetchone() is None
    finally:
        await store._db.close()  # type: ignore[union-attr]


def test_memory_meta_tolerates_old_and_unknown_json_fields() -> None:
    raw = (
        '{"model":"m","provider":"p","chunk_tokens":400,"chunk_overlap":50,'
        '"vector_dims":null,"fts_tokenizer":"unicode61","sources":["memory"],'
        '"unknown":"ignored"}'
    )
    meta = MemoryIndexMeta.from_json(raw)
    assert meta is not None
    assert meta.provider_fingerprint is None


def test_embedding_cache_key_uses_provider_fingerprint() -> None:
    store_a = LongTermMemoryStore(
        ":memory:",
        embedding_provider=_FakeEmbeddingProvider(fingerprint="a"),
    )
    store_b = LongTermMemoryStore(
        ":memory:",
        embedding_provider=_FakeEmbeddingProvider(fingerprint="b"),
    )
    assert store_a._cache_key_prefix() != store_b._cache_key_prefix()


def test_remote_api_key_changes_provider_fingerprint() -> None:
    cfg_a = GatewayConfig(
        memory={"embedding": {"provider": "openai", "remote": {"api_key": "key-a"}}}
    )
    cfg_b = GatewayConfig(
        memory={"embedding": {"provider": "openai", "remote": {"api_key": "key-b"}}}
    )

    decision_a = resolve_memory_embedding(cfg_a.memory, local_available=lambda *_: False)
    decision_b = resolve_memory_embedding(cfg_b.memory, local_available=lambda *_: False)

    assert decision_a.fingerprint != decision_b.fingerprint
    assert "key-a" not in decision_a.fingerprint
    assert "key-b" not in decision_b.fingerprint


@pytest.mark.asyncio
async def test_rebuild_preserves_embedding_cache_rows() -> None:
    store = LongTermMemoryStore(
        ":memory:",
        embedding_provider=NullEmbeddingProvider(),
    )
    store._db = await aiosqlite.connect(":memory:")  # type: ignore[assignment]
    try:
        await store._ensure_schema()
        await store._db.execute(  # type: ignore[union-attr]
            """INSERT INTO embedding_cache
               (provider, model, provider_key, hash, embedding, dims, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("p", "m", "k", "h", "[0.0]", 1, 1.0),
        )
        await store._db.commit()  # type: ignore[union-attr]
        await store.index_file("MEMORY.md", "cache survives rebuild")

        await store.rebuild()

        async with store._db.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM embedding_cache"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        assert await store.file_count() == 0
    finally:
        await store._db.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_fts_only_reindex_after_provider_change_restores_lexical_search() -> None:
    store = LongTermMemoryStore(
        ":memory:",
        embedding_provider=NullEmbeddingProvider(),
    )
    store._db = await aiosqlite.connect(":memory:")  # type: ignore[assignment]
    try:
        await store._ensure_schema()
        store._fts_available = True
        await store.index_file(
            "MEMORY.md",
            "alpha memory survives provider changes",
        )
        initial_results, initial_mode = await store.search("alpha memory", max_results=3)
        assert initial_mode.value == "fts-only"
        assert initial_results

        old_meta = MemoryIndexMeta(
            model="text-embedding-3-small",
            provider="openai",
            chunk_tokens=400,
            chunk_overlap=50,
            vector_dims=None,
            fts_tokenizer="unicode61",
            sources=["memory"],
            provider_fingerprint="fp-old",
        )
        await store._db.execute(  # type: ignore[union-attr]
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("memory_provider_meta", old_meta.to_json()),
        )
        await store._db.commit()  # type: ignore[union-attr]

        await store._check_meta_and_reindex()

        await store.index_file(
            "MEMORY.md",
            "alpha memory survives provider changes",
        )
        results, mode = await store.search("alpha memory", max_results=3)

        assert mode.value == "fts-only"
        assert results
        assert results[0].path == "MEMORY.md"
    finally:
        await store._db.close()  # type: ignore[union-attr]
