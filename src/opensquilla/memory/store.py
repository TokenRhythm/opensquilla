"""Long-term persistent memory store using SQLite + sqlite-vec + FTS5."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import struct
import time
from pathlib import Path
from typing import Any

import structlog

from opensquilla.compat import aiosqlite

from .embedding import (
    EmbeddingProvider,
    NullEmbeddingProvider,
    _estimate_tokens,
    chunk_hash,
    chunk_text,
)
from .meta import MemoryIndexMeta
from .types import (
    DEFAULT_MEMORY_SEARCH_MIN_SCORE,
    DEFAULT_MEMORY_SEARCH_RESULTS,
    LEXICAL_GUARANTEE_METADATA_KEY,
    LEXICAL_GUARANTEE_METADATA_VALUE,
    RELAXED_KEYWORD_MATCH_METADATA_KEY,
    RELAXED_KEYWORD_MATCH_METADATA_VALUE,
    MemorySearchResult,
    MemorySource,
    SearchMode,
)

logger = structlog.get_logger(__name__)

_JIEBA_WARNED = False
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

SCHEMA_VERSION = "v2"
META_KEY = "memory_index_meta_v1"
VECTOR_NORMALIZATION_META_KEY = "memory_vector_normalization_v1"
VECTOR_NORMALIZATION_META_VALUE = "l2"

# schema_version carried on every memory table so future shape changes
# route through a yoyo migration rather than an in-product ALTER TABLE.
# See migrations/V004__memory_schema_version.py for the back-fill path
# on older databases.
MEMORY_SCHEMA_VERSION = 1

DDL_FILES = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
"""

DDL_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    hash TEXT NOT NULL,
    model TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding TEXT,
    updated_at REAL NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
"""

DDL_EMBEDDING_CACHE = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    hash TEXT NOT NULL,
    embedding TEXT NOT NULL,
    dims INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider, model, provider_key, hash)
);
"""

DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
"""

DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    id UNINDEXED,
    path UNINDEXED,
    source UNINDEXED,
    model UNINDEXED,
    start_line UNINDEXED,
    end_line UNINDEXED,
    tokenize='unicode61'
);
"""

DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
"""


def _float_list_to_blob(floats: list[float]) -> bytes:
    return struct.pack(f"{len(floats)}f", *floats)


def _l2_normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return vector
    return [value / norm for value in vector]


def _vector_distance_to_score(distance: float) -> float:
    return max(0.0, 1.0 - distance / 2.0)


def _chunk_id(
    source: str, path: str, start_line: int, end_line: int, text_hash: str, model: str
) -> str:
    raw = f"{source}:{path}:{start_line}:{end_line}:{text_hash}:{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _query_embedding_hash(query: str) -> str:
    return hashlib.sha256(f"query\0{query}".encode("utf-8", errors="replace")).hexdigest()


def _estimate_embedding_cost_usd(model: str, input_tokens: int, provider: str = "") -> float:
    if not model or input_tokens <= 0:
        return 0.0
    try:
        from opensquilla.engine.pricing import lookup_price

        price = lookup_price(model, provider)
        return input_tokens * price.input_per_m / 1_000_000
    except Exception:  # noqa: BLE001
        return 0.0


def _zero_embedding_usage(provider: EmbeddingProvider) -> dict[str, Any]:
    return {
        "request_count": 0,
        "input_count": 0,
        "input_tokens": 0,
        "cache_hit_count": 0,
        "cache_write_count": 0,
        "cost_usd": 0.0,
        "billed_cost": 0.0,
        "model": provider.model,
        "provider": provider.provider_id,
        "cost_source": "none",
    }


class LongTermMemoryStore:
    """
    Persistent memory store backed by SQLite.
    Uses FTS5 for keyword search.
    Optionally uses sqlite-vec extension for vector similarity search.
    """

    def __init__(
        self,
        db_path: str | Path,
        embedding_provider: EmbeddingProvider | None = None,
        query_embedding_cache_mode: str = "on",
    ) -> None:
        self._db_path = str(db_path)
        self._provider: EmbeddingProvider = embedding_provider or NullEmbeddingProvider()
        self._query_embedding_cache_mode = query_embedding_cache_mode
        self._vec_available = False
        self._fts_available = False
        self._db: aiosqlite.Connection | None = None
        self._dirty = False
        self._embedding_usage: dict[str, Any] = _zero_embedding_usage(self._provider)

    async def initialize(self) -> None:
        """Open DB, ensure schema, probe vector extension."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._ensure_schema()
        await self._probe_vec_extension()
        self._fts_available = True  # FTS5 is always available in standard SQLite
        await self._check_meta_and_reindex()

    def consume_embedding_usage(self) -> dict[str, Any]:
        """Return and reset embedding audit counters since the previous call."""
        usage = dict(self._embedding_usage)
        self._embedding_usage = _zero_embedding_usage(self._provider)
        return usage

    def _record_embedding_cache_hits(self, count: int) -> None:
        if count <= 0:
            return
        self._embedding_usage["cache_hit_count"] = int(
            self._embedding_usage.get("cache_hit_count", 0)
        ) + count
        if self._embedding_usage.get("cost_source") == "none":
            self._embedding_usage["cost_source"] = "cache"

    def _record_embedding_request(self, texts: list[str]) -> None:
        if not texts:
            return
        input_tokens = sum(_estimate_tokens(text) for text in texts)
        self._embedding_usage["request_count"] = int(
            self._embedding_usage.get("request_count", 0)
        ) + 1
        self._embedding_usage["input_count"] = int(
            self._embedding_usage.get("input_count", 0)
        ) + len(texts)
        self._embedding_usage["input_tokens"] = int(
            self._embedding_usage.get("input_tokens", 0)
        ) + input_tokens
        self._embedding_usage["cost_usd"] = float(
            self._embedding_usage.get("cost_usd", 0.0) or 0.0
        ) + _estimate_embedding_cost_usd(
            self._provider.model, input_tokens, self._provider.provider_id
        )
        self._embedding_usage["model"] = self._provider.model
        self._embedding_usage["provider"] = self._provider.provider_id
        self._embedding_usage["cost_source"] = "opensquilla_static_estimate"

    def _record_embedding_cache_writes(self, count: int) -> None:
        if count <= 0:
            return
        self._embedding_usage["cache_write_count"] = int(
            self._embedding_usage.get("cache_write_count", 0)
        ) + count

    async def _check_meta_and_reindex(self) -> None:
        """Compare stored MemoryIndexMeta with current config. Clear stale index on mismatch."""
        assert self._db is not None

        current_meta = MemoryIndexMeta(
            model=self._provider.model,
            provider=self._provider.provider_id,
            chunk_tokens=400,
            chunk_overlap=50,
            vector_dims=self._provider_vector_dims(),
            fts_tokenizer="unicode61",
            sources=["memory", "sessions"],
            provider_fingerprint=self._provider_fingerprint(),
        )

        async with self._db.execute(
            "SELECT value FROM meta WHERE key = ?", ("memory_provider_meta",)
        ) as cur:
            row = await cur.fetchone()

        stored_meta = MemoryIndexMeta.from_json(row[0] if row else None)

        if stored_meta is not None and stored_meta.requires_reindex(current_meta):
            logger.info(
                "meta_change_detected",
                old_model=stored_meta.model,
                new_model=current_meta.model,
            )
            # Clear all indexed data so next file sync rebuilds from disk.
            await self._db.execute("DELETE FROM chunks_fts")
            await self._db.execute("DELETE FROM chunks")
            await self._db.execute("DELETE FROM files")
            if self._vec_available:
                try:
                    await self._db.execute("DROP TABLE IF EXISTS chunks_vec")
                except Exception:
                    pass
            self._dirty = True
            logger.info("meta_reindex_cleared", reason="config_change")

        # Always write current meta
        await self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("memory_provider_meta", current_meta.to_json()),
        )
        await self._db.commit()
        await self._ensure_vector_normalization()

    async def _ensure_vector_normalization(self) -> None:
        """Normalize stored vectors once so sqlite-vec distances stay meaningful."""
        assert self._db is not None

        async with self._db.execute(
            "SELECT value FROM meta WHERE key = ?", (VECTOR_NORMALIZATION_META_KEY,)
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] == VECTOR_NORMALIZATION_META_VALUE:
            if not self._vec_available:
                return
            async with self._db.execute(
                "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
            ) as cur:
                chunk_count_row = await cur.fetchone()
            chunk_count = chunk_count_row[0] if chunk_count_row else 0
            if chunk_count == 0:
                return
            try:
                async with self._db.execute("SELECT COUNT(*) FROM chunks_vec") as cur:
                    vec_count_row = await cur.fetchone()
                vec_count = vec_count_row[0] if vec_count_row else 0
            except Exception:
                vec_count = 0
            if vec_count >= chunk_count:
                return

        async with self._db.execute(
            "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

        normalized_rows: list[tuple[str, list[float]]] = []
        dims: int | None = None
        for chunk_id, embedding_json in rows:
            try:
                raw = json.loads(embedding_json)
                if not isinstance(raw, list):
                    continue
                vector = [float(value) for value in raw]
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("memory_embedding_normalization_skipped", chunk_id=chunk_id)
                continue
            normalized = _l2_normalize_vector(vector)
            if dims is None:
                dims = len(normalized)
            if len(normalized) != dims:
                logger.warning(
                    "memory_embedding_normalization_dim_mismatch",
                    chunk_id=chunk_id,
                    dims=len(normalized),
                    expected=dims,
                )
                continue
            normalized_rows.append((chunk_id, normalized))

        if normalized_rows:
            if self._vec_available and dims:
                try:
                    await self._db.execute("DROP TABLE IF EXISTS chunks_vec")
                    await self._ensure_vec_table(dims)
                except Exception as exc:
                    logger.warning("memory_vec_rebuild_for_normalization_failed", error=str(exc))

            for chunk_id, normalized in normalized_rows:
                await self._db.execute(
                    "UPDATE chunks SET embedding = ? WHERE id = ?",
                    (json.dumps(normalized), chunk_id),
                )
                if self._vec_available and dims and len(normalized) == dims:
                    try:
                        await self._db.execute(
                            "INSERT OR REPLACE INTO chunks_vec(id, embedding) VALUES (?, ?)",
                            (chunk_id, _float_list_to_blob(normalized)),
                        )
                    except Exception as exc:
                        logger.warning(
                            "memory_vec_reinsert_after_normalization_failed",
                            chunk_id=chunk_id,
                            error=str(exc),
                        )

        await self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (VECTOR_NORMALIZATION_META_KEY, VECTOR_NORMALIZATION_META_VALUE),
        )
        await self._db.commit()

    async def _ensure_schema(self) -> None:
        assert self._db is not None
        await self._db.execute(DDL_FILES)
        await self._db.execute(DDL_CHUNKS)
        await self._db.execute(DDL_EMBEDDING_CACHE)
        await self._db.execute(DDL_META)
        await self._migrate_schema_version_columns()

        # Migrate old external-content FTS to contentless mode
        async with self._db.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks_fts' AND type='table'"
        ) as cur:
            row = await cur.fetchone()
        if row and "content=chunks" in (row[0] or ""):
            logger.info("fts_migration", reason="dropping old content=chunks FTS table")
            await self._db.execute("DROP TABLE chunks_fts")

        await self._db.execute(DDL_FTS)

        # --- Schema version check ---
        async with self._db.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)) as cur:
            row = await cur.fetchone()

        stored_version = row[0] if row else None

        if stored_version is not None and stored_version != SCHEMA_VERSION:
            # Schema mismatch — drop everything for rebuild
            # Order matters: drop FTS first (references chunks data), then chunks, then files
            logger.info("schema_version_mismatch", stored=stored_version, expected=SCHEMA_VERSION)
            await self._db.execute("DROP TABLE IF EXISTS chunks_fts")
            await self._db.execute("DROP TABLE IF EXISTS chunks")
            await self._db.execute("DROP TABLE IF EXISTS files")
            await self._db.execute("DROP TABLE IF EXISTS embedding_cache")
            try:
                await self._db.execute("DROP TABLE IF EXISTS chunks_vec")
            except Exception:
                pass  # chunks_vec may not exist if sqlite-vec was never loaded
            # Recreate core tables (chunks_vec recreated lazily by _ensure_vec_table)
            await self._db.execute(DDL_FILES)
            await self._db.execute(DDL_CHUNKS)
            await self._db.execute(DDL_EMBEDDING_CACHE)
            await self._db.execute(DDL_FTS)

        # Populate FTS from existing chunks if empty (after migration or fresh DB)
        async with self._db.execute("SELECT count(*) FROM chunks_fts") as cur:
            fts_row = await cur.fetchone()
            fts_count = fts_row[0] if fts_row else 0
        if fts_count == 0:
            async with self._db.execute("SELECT count(*) FROM chunks") as cur:
                chunks_row = await cur.fetchone()
                chunks_count = chunks_row[0] if chunks_row else 0
            if chunks_count > 0:
                logger.info("fts_rebuild", chunks=chunks_count)
                async with self._db.execute(
                    "SELECT id, path, source, model, start_line, end_line, text FROM chunks"
                ) as cur:
                    for r in await cur.fetchall():
                        await self._db.execute(
                            "INSERT INTO chunks_fts"
                            "(text, id, path, source, model, start_line, end_line)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_segment_for_fts(r[6]), r[0], r[1], r[2], r[3], r[4], r[5]),
                        )

        # Execute indexes (idempotent CREATE INDEX IF NOT EXISTS)
        await self._db.executescript(DDL_INDEXES)

        # Always write current schema version
        await self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (META_KEY, SCHEMA_VERSION),
        )

        await self._db.commit()

    async def _migrate_schema_version_columns(self) -> None:
        """Idempotently add ``schema_version`` in place on legacy memory tables.

        Databases created before the column existed carry ``files``,
        ``chunks``, ``embedding_cache``, and ``meta`` without it. The yoyo
        back-fill (migrations/V004__memory_schema_version.py) only ever runs
        against the session DB, never against per-agent ``memory.db`` files,
        so the column must be added here at connect time — otherwise every
        ``embedding_cache`` INSERT on a legacy database fails with
        "no column named schema_version" and the cache silently never works.
        Column DDL matches V004 exactly.
        """
        assert self._db is not None
        for table in ("files", "chunks", "embedding_cache", "meta"):
            async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if not columns or "schema_version" in columns:
                continue
            await self._db.execute(
                f"ALTER TABLE {table} ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )
        await self._db.commit()

    async def _probe_vec_extension(self) -> None:
        """Attempt to load sqlite-vec extension."""
        assert self._db is not None
        try:
            import sqlite_vec  # type: ignore

            await self._db.enable_load_extension(True)
            try:
                await self._db.load_extension(sqlite_vec.loadable_path())
            finally:
                await self._db.enable_load_extension(False)

            # Create vec table if needed
            # We need to know embedding dims; defer table creation until first insert
            self._vec_available = True
            logger.info("sqlite_vec_loaded")
        except Exception as e:
            logger.warning("sqlite_vec_unavailable", error=str(e))
            self._vec_available = False

    async def _ensure_vec_table(self, dims: int) -> None:
        """Create or verify the vec virtual table with correct dimensions."""
        assert self._db is not None
        await self._db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{dims}]
            )
            """
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Embedding cache helpers
    # ------------------------------------------------------------------

    def _cache_key_prefix(self) -> tuple[str, str, str]:
        """Return (provider_id, model, provider_key) for the current provider."""
        return (self._provider.provider_id, self._provider.model, self._provider_fingerprint())

    def _provider_fingerprint(self) -> str:
        value = getattr(self._provider, "_provider_fingerprint", None)
        if value:
            return str(value)
        provider_key = getattr(self._provider, "_base_url", "") or getattr(
            self._provider, "_onnx_dir", ""
        )
        return str(provider_key)

    def _provider_vector_dims(self) -> int | None:
        value = getattr(self._provider, "_vector_dims", None)
        if isinstance(value, int) and value > 0:
            return value
        try:
            loaded = getattr(self._provider, "loaded", False)
            if loaded:
                dim = getattr(self._provider, "dim", None)
                if isinstance(dim, int) and dim > 0:
                    return dim
        except Exception:
            return None
        return None

    async def _lookup_embedding_cache(
        self, hashes: list[str]
    ) -> dict[str, list[float]]:
        """Return {hash: embedding} for hashes already cached for the current
        provider+model+provider_key. Misses are absent from the result dict.

        Fully best-effort: SQL errors, missing table, JSON decode failures,
        or malformed rows fall through to ``{}`` so a corrupt cache row
        cannot abort an indexing call. Worst case is a redundant
        ``provider.embed_batch`` call, which is the pre-cache baseline.
        """
        assert self._db is not None
        if not hashes:
            return {}

        provider_id, model, provider_key = self._cache_key_prefix()
        placeholders = ",".join("?" * len(hashes))
        try:
            async with self._db.execute(
                f"""SELECT hash, embedding FROM embedding_cache
                    WHERE provider = ? AND model = ? AND provider_key = ?
                      AND hash IN ({placeholders})""",
                (provider_id, model, provider_key, *hashes),
            ) as cur:
                rows = await cur.fetchall()
        except Exception as exc:
            if "no such table" not in str(exc).lower():
                logger.warning("embedding_cache_lookup_failed", error=str(exc))
            return {}

        result: dict[str, list[float]] = {}
        for row in rows:
            try:
                vec = json.loads(row[1])
                if isinstance(vec, list):
                    result[row[0]] = vec
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "embedding_cache_row_malformed",
                    hash=row[0],
                    error=str(exc),
                )
        return result

    async def _store_embedding_cache(
        self, items: list[tuple[str, list[float]]]
    ) -> None:
        """Write (hash, embedding) pairs to the cache for the current provider.
        Uses INSERT OR IGNORE so concurrent writers are safe.

        Best-effort: a transient SQLite error mid-write rolls back so the
        connection is left without a dangling transaction (the next
        ``BEGIN IMMEDIATE`` in ``index_file`` would otherwise fail).
        Failures are logged and swallowed.
        """
        assert self._db is not None
        if not items:
            return

        provider_id, model, provider_key = self._cache_key_prefix()
        now = time.time()
        try:
            for h, vec in items:
                await self._db.execute(
                    """INSERT OR IGNORE INTO embedding_cache
                       (provider, model, provider_key, hash, embedding, dims,
                        updated_at, schema_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        provider_id,
                        model,
                        provider_key,
                        h,
                        json.dumps(vec),
                        len(vec),
                        now,
                        MEMORY_SCHEMA_VERSION,
                    ),
                )
            await self._db.commit()
        except Exception as exc:
            # Roll back so a partial INSERT batch does not leave the
            # connection in an open transaction state — the next
            # `BEGIN IMMEDIATE` in `index_file` would otherwise raise
            # `cannot start a transaction within a transaction`.
            try:
                await self._db.rollback()
            except Exception:
                pass
            if "no such table" in str(exc).lower():
                logger.warning("embedding_cache_table_missing", error=str(exc))
                return
            logger.warning("embedding_cache_write_failed", error=str(exc))

    async def _embed_query_cached(self, query: str) -> list[float]:
        """Embed a search query with the same rebuildable cache used for chunks."""
        if self._query_embedding_cache_mode not in {"on", "shadow"}:
            return await self._provider.embed_query(query)

        query_hash = _query_embedding_hash(query)
        if self._query_embedding_cache_mode == "on":
            cached = await self._lookup_embedding_cache([query_hash])
            if query_hash in cached:
                return cached[query_hash]

        vec = await self._provider.embed_query(query)
        await self._store_embedding_cache([(query_hash, vec)])
        return vec

    # ------------------------------------------------------------------
    # File indexing
    # ------------------------------------------------------------------

    async def index_file(
        self,
        path: str,
        content: str,
        source: MemorySource = MemorySource.memory,
        mtime: float | None = None,
        chunk_tokens: int = 400,
        chunk_overlap: int = 50,
    ) -> int:
        """Index a file into memory. Returns number of chunks created."""
        assert self._db is not None

        raw = content.encode()
        file_hash = _file_hash(raw)
        mtime = mtime or time.time()
        size = len(raw)

        # Check if file changed
        async with self._db.execute("SELECT hash FROM files WHERE path = ?", (path,)) as cur:
            row = await cur.fetchone()
            if row and row[0] == file_hash:
                # Unchanged content — but if a real embedding provider is now
                # configured and this file has chunks left un-embedded by a
                # prior transient failure, re-index so those chunks get vectors
                # (the embedding cache makes already-embedded chunks free). A
                # bare hash-match short-circuit stranded them forever.
                model_now = self._provider.model
                if model_now != "fts-only" and not isinstance(
                    self._provider, NullEmbeddingProvider
                ):
                    async with self._db.execute(
                        "SELECT COUNT(*) FROM chunks WHERE path = ? AND embedding IS NULL",
                        (path,),
                    ) as cur2:
                        null_row = await cur2.fetchone()
                    if not (null_row and int(null_row[0]) > 0):
                        return 0  # unchanged and fully embedded
                else:
                    return 0  # unchanged

        # Compute chunks + embeddings BEFORE the transaction (network I/O outside DB lock)
        chunks = chunk_text(content, chunk_tokens, chunk_overlap)
        model = self._provider.model
        now = time.time()

        chunk_records = []
        for start_line, end_line, text in chunks:
            text_hash = chunk_hash(text)
            cid = _chunk_id(source.value, path, start_line, end_line, text_hash, model)
            chunk_records.append(
                (cid, path, source.value, start_line, end_line, text_hash, model, text, now)
            )

        # Get embeddings if provider available
        embeddings: list[list[float] | None] = [None] * len(chunk_records)
        if model != "fts-only" and not isinstance(self._provider, NullEmbeddingProvider):
            # Cache lookup is read-only and idempotent — happens BEFORE the transaction.
            text_hashes = [r[5] for r in chunk_records]
            cached = await self._lookup_embedding_cache(text_hashes)
            self._record_embedding_cache_hits(len(cached))

            to_embed_indices: list[int] = []
            to_embed_texts: list[str] = []
            for i, record in enumerate(chunk_records):
                text_hash = record[5]
                if text_hash in cached:
                    embeddings[i] = cached[text_hash]
                else:
                    to_embed_indices.append(i)
                    to_embed_texts.append(record[7])

            try:
                if to_embed_texts:
                    self._record_embedding_request(to_embed_texts)
                    new_vecs = await self._provider.embed_batch(to_embed_texts)
                    for idx, vec in zip(to_embed_indices, new_vecs, strict=False):
                        embeddings[idx] = vec
                    # Write new embeddings back to cache (best-effort; _store_embedding_cache
                    # catches its own exceptions and logs them).
                    new_cache_items = [
                        (chunk_records[idx][5], vec)
                        for idx, vec in zip(to_embed_indices, new_vecs, strict=False)
                    ]
                    await self._store_embedding_cache(new_cache_items)
                    self._record_embedding_cache_writes(len(new_cache_items))
                    if self._vec_available and new_vecs:
                        await self._ensure_vec_table(len(new_vecs[0]))
                elif cached and self._vec_available:
                    # All chunks hit the cache — derive dims from first cached vector
                    first_vec = next(iter(cached.values()))
                    await self._ensure_vec_table(len(first_vec))
            except Exception as e:
                logger.warning("embedding_failed_fallback_fts", path=path, error=str(e))

        # Wrap all SQLite mutations in an explicit transaction so a crash mid-operation
        # leaves the DB in a consistent state (either fully updated or fully rolled back).
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            # Delete old chunks — vec first (needs chunk IDs before they're deleted)
            if self._vec_available:
                try:
                    await self._db.execute(
                        "DELETE FROM chunks_vec WHERE id IN (SELECT id FROM chunks WHERE path = ?)",
                        (path,),
                    )
                except Exception:
                    pass
            await self._db.execute("DELETE FROM chunks_fts WHERE path = ?", (path,))
            await self._db.execute("DELETE FROM chunks WHERE path = ?", (path,))

            # Insert new chunks
            for i, (cid, p, src, sl, el, h, mdl, txt, ts) in enumerate(chunk_records):
                emb = embeddings[i]
                stored_emb = _l2_normalize_vector(emb) if emb else None
                emb_json = json.dumps(stored_emb) if stored_emb else None
                await self._db.execute(
                    """INSERT OR REPLACE INTO chunks
                       (id, path, source, start_line, end_line, hash, model,
                        text, embedding, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cid, p, src, sl, el, h, mdl, txt, emb_json, ts),
                )
                # FTS insert — segment CJK for proper tokenization
                await self._db.execute(
                    "INSERT INTO chunks_fts"
                    "(text, id, path, source, model, start_line, end_line)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_segment_for_fts(txt), cid, p, src, mdl, sl, el),
                )
                # Vec insert
                if self._vec_available and stored_emb:
                    try:
                        blob = _float_list_to_blob(stored_emb)
                        await self._db.execute(
                            "INSERT OR REPLACE INTO chunks_vec(id, embedding) VALUES (?, ?)",
                            (cid, blob),
                        )
                    except Exception as e:
                        logger.warning("vec_insert_failed", chunk_id=cid, error=str(e))

            # Upsert file record
            await self._db.execute(
                """INSERT OR REPLACE INTO files (path, source, hash, mtime, size)
                   VALUES (?, ?, ?, ?, ?)""",
                (path, source.value, file_hash, mtime, size),
            )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise

        self._dirty = False
        return len(chunk_records)

    async def remove_file(self, path: str) -> None:
        assert self._db is not None
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            # Vec first — needs chunk IDs before they're deleted (same pattern as index_file)
            if self._vec_available:
                try:
                    await self._db.execute(
                        "DELETE FROM chunks_vec WHERE id IN "
                        "(SELECT id FROM chunks WHERE path = ?)",
                        (path,),
                    )
                except Exception:
                    pass
            await self._db.execute("DELETE FROM chunks_fts WHERE path = ?", (path,))
            await self._db.execute("DELETE FROM chunks WHERE path = ?", (path,))
            await self._db.execute("DELETE FROM files WHERE path = ?", (path,))
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise

    async def rebuild(self) -> None:
        """Clear rebuildable index rows.

        Sync managers are responsible for re-indexing canonical Markdown
        memory sources and derived session sources after this call.
        """
        assert self._db is not None
        if self._vec_available:
            try:
                await self._db.execute("DELETE FROM chunks_vec")
            except Exception:
                pass
        await self._db.execute("DELETE FROM chunks_fts")
        await self._db.execute("DELETE FROM chunks")
        await self._db.execute("DELETE FROM files")
        await self._db.commit()
        self._dirty = True

    async def health(self) -> dict[str, Any]:
        """Return backend status suitable for conformance checks and diagnostics."""
        if self._db is None:
            return {
                "backend": "sqlite",
                "initialized": False,
                "healthy": False,
                "vec_available": False,
                "fts_available": False,
                "source_counts": {},
            }
        healthy = True
        error: str | None = None
        try:
            await self._db.execute("SELECT 1")
            source_counts = await self.source_counts()
        except Exception as exc:  # noqa: BLE001
            healthy = False
            error = str(exc)
            source_counts = {}
        status: dict[str, Any] = {
            "backend": "sqlite",
            "initialized": True,
            "healthy": healthy,
            "vec_available": self._vec_available,
            "fts_available": self._fts_available,
            "source_counts": source_counts,
        }
        if error is not None:
            status["error"] = error
        return status

    async def get_chunk_hashes_for_path(self, path: str) -> list[str]:
        """Return the chunk hashes currently indexed under ``path``."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT hash FROM chunks WHERE path = ?", (path,)
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        max_results: int = DEFAULT_MEMORY_SEARCH_RESULTS,
        min_score: float = DEFAULT_MEMORY_SEARCH_MIN_SCORE,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
        source: MemorySource | None = None,
    ) -> tuple[list[MemorySearchResult], SearchMode]:
        """
        Hybrid search: vector + FTS5. Returns (results, mode_used).
        Falls back to FTS-only if vector not available.
        """
        assert self._db is not None

        use_vector = self._vec_available and not isinstance(self._provider, NullEmbeddingProvider)

        if use_vector:
            try:
                query_vec = await self._embed_query_cached(query)
                results = await self._hybrid_search(
                    query,
                    query_vec,
                    max_results,
                    min_score,
                    vector_weight,
                    text_weight,
                    source=source,
                )
                return results, SearchMode.hybrid
            except Exception as e:
                logger.warning("vector_search_failed_fallback", error=str(e))

        results = await self._fts_search(query, max_results, min_score, source=source)
        return results, SearchMode.fts_only

    async def _vector_search(
        self,
        query_vec: list[float],
        k: int,
        model: str,
        *,
        source: MemorySource | None = None,
    ) -> list[tuple[str, float]]:
        """Returns list of (chunk_id, score)."""
        assert self._db is not None
        blob = _float_list_to_blob(_l2_normalize_vector(query_vec))
        try:
            # sqlite-vec requires 'k = ?' in the virtual table WHERE clause
            # rather than only a LIMIT on the outer query.
            sql = """
            SELECT v.id, v.distance
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.id
            WHERE v.embedding MATCH ?
              AND k = ?
              AND c.model = ?
            """
            params: tuple[Any, ...] = (blob, k, model)
            if source is not None:
                sql += " AND c.source = ?"
                params = (*params, source.value)
            sql += " ORDER BY v.distance"
            async with self._db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [(row[0], _vector_distance_to_score(row[1])) for row in rows]
        except Exception as e:
            logger.warning("vector_search_error", error=str(e))
            return []

    async def _fts_search(
        self,
        query: str,
        k: int,
        min_score: float,
        *,
        source: MemorySource | None = None,
    ) -> list[MemorySearchResult]:
        """BM25-based FTS5 keyword search."""
        assert self._db is not None
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            sql = """
            SELECT chunks_fts.id, c.path, c.source, c.start_line, c.end_line, c.text,
                   bm25(chunks_fts) as rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.id
            WHERE chunks_fts MATCH ?
            """
            params: tuple[Any, ...] = (fts_query,)
            if source is not None:
                sql += " AND c.source = ?"
                params = (*params, source.value)
            sql += " ORDER BY rank LIMIT ?"
            params = (*params, k * 3)
            async with self._db.execute(sql, params) as cur:
                rows = await cur.fetchall()

            results = []
            relaxed_results = []
            for row in rows:
                cid, path, source, sl, el, text, rank = row
                score = _bm25_to_score(rank)
                result = MemorySearchResult(
                    chunk_id=cid,
                    path=path,
                    source=MemorySource(source),
                    start_line=sl,
                    end_line=el,
                    snippet=text[:700],
                    score=score,
                    text_score=score,
                    text=text,
                    citation=f"{path}#L{sl}-L{el}",
                )
                relaxed_results.append(result)
                if score >= min_score:
                    results.append(result)
            if not results and relaxed_results:
                for result in relaxed_results:
                    result.metadata[RELAXED_KEYWORD_MATCH_METADATA_KEY] = (
                        RELAXED_KEYWORD_MATCH_METADATA_VALUE
                    )
                results = relaxed_results
            results = results[:k]

            # Fetch content hashes for all results in a single follow-up query.
            # No JOIN inside the FTS path — FTS5 ranking semantics stay untouched.
            if results:
                ids = [r.chunk_id for r in results]
                placeholders = ",".join("?" * len(ids))
                async with self._db.execute(
                    f"SELECT id, hash FROM chunks WHERE id IN ({placeholders})",
                    ids,
                ) as cur:
                    hash_map = {row[0]: row[1] for row in await cur.fetchall()}
                for r in results:
                    r.chunk_hash = hash_map.get(r.chunk_id)

            return results
        except Exception as e:
            logger.warning("fts_search_error", error=str(e))
            return []

    async def _hybrid_search(
        self,
        query: str,
        query_vec: list[float],
        k: int,
        min_score: float,
        vector_weight: float,
        text_weight: float,
        *,
        source: MemorySource | None = None,
    ) -> list[MemorySearchResult]:
        """Merge vector and FTS5 results."""
        candidates = min(200, k * 10)
        vec_results = await self._vector_search(
            query_vec,
            candidates,
            self._provider.model,
            source=source,
        )
        fts_results = await self._fts_search(query, candidates, 0.0, source=source)

        # Map chunk_id -> scores
        scores: dict[str, dict[str, float]] = {}
        for cid, vscore in vec_results:
            scores.setdefault(cid, {})["vector"] = vscore
        for r in fts_results:
            scores.setdefault(r.chunk_id, {})["text"] = r.text_score or 0.0

        if not scores:
            return []

        # Fetch chunk data for all candidates
        all_ids = list(scores.keys())
        placeholders = ",".join("?" * len(all_ids))
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, path, source, start_line, end_line, text, hash"
            f" FROM chunks WHERE id IN ({placeholders})",
            all_ids,
        ) as cur:
            chunk_rows = {row[0]: row for row in await cur.fetchall()}

        results = []
        for cid, s in scores.items():
            if cid not in chunk_rows:
                continue
            row = chunk_rows[cid]
            vscore = s.get("vector", 0.0)
            tscore = s.get("text", 0.0)
            combined = vector_weight * vscore + text_weight * tscore
            if combined >= min_score:
                results.append(
                    MemorySearchResult(
                        chunk_id=cid,
                        path=row[1],
                        source=MemorySource(row[2]),
                        start_line=row[3],
                        end_line=row[4],
                        snippet=row[5][:700],
                        score=combined,
                        vector_score=vscore,
                        text_score=tscore,
                        text=row[5],
                        chunk_hash=row[6],
                        citation=f"{row[1]}#L{row[3]}-L{row[4]}",
                    )
                )

        strict_ids = {result.chunk_id for result in results}
        lexical_guarantees: list[MemorySearchResult] = []
        if results:
            for cid, s in scores.items():
                if cid in strict_ids or cid not in chunk_rows or "text" not in s:
                    continue
                tscore = s.get("text", 0.0)
                if tscore < min_score:
                    continue
                row = chunk_rows[cid]
                vscore = s.get("vector", 0.0)
                combined = vector_weight * vscore + text_weight * tscore
                lexical_guarantees.append(
                    MemorySearchResult(
                        chunk_id=cid,
                        path=row[1],
                        source=MemorySource(row[2]),
                        start_line=row[3],
                        end_line=row[4],
                        snippet=row[5][:700],
                        score=combined,
                        vector_score=vscore,
                        text_score=tscore,
                        text=row[5],
                        chunk_hash=row[6],
                        metadata={
                            LEXICAL_GUARANTEE_METADATA_KEY: (
                                LEXICAL_GUARANTEE_METADATA_VALUE
                            )
                        },
                        citation=f"{row[1]}#L{row[3]}-L{row[4]}",
                    )
                )

        if not results and fts_results:
            relaxed_min_score = min(min_score, text_weight)
            for cid, s in scores.items():
                if cid not in chunk_rows or "text" not in s:
                    continue
                row = chunk_rows[cid]
                vscore = s.get("vector", 0.0)
                tscore = s.get("text", 0.0)
                combined = vector_weight * vscore + text_weight * tscore
                if combined >= relaxed_min_score or tscore >= min_score:
                    results.append(
                        MemorySearchResult(
                            chunk_id=cid,
                            path=row[1],
                            source=MemorySource(row[2]),
                            start_line=row[3],
                            end_line=row[4],
                            snippet=row[5][:700],
                            score=combined,
                            vector_score=vscore,
                            text_score=tscore,
                            text=row[5],
                            chunk_hash=row[6],
                            metadata={
                                RELAXED_KEYWORD_MATCH_METADATA_KEY: (
                                    RELAXED_KEYWORD_MATCH_METADATA_VALUE
                                )
                            },
                            citation=f"{row[1]}#L{row[3]}-L{row[4]}",
                        )
                    )

        results.sort(key=lambda r: r.score, reverse=True)
        lexical_guarantees.sort(
            key=lambda r: (r.text_score or 0.0, r.score),
            reverse=True,
        )
        if lexical_guarantees:
            guarantee_cap = min(len(lexical_guarantees), max(1, min(2, (k + 2) // 3)))
            strict_keep = max(0, k - guarantee_cap)
            return (results[:strict_keep] + lexical_guarantees[:guarantee_cap])[:k]
        return results[:k]

    # ------------------------------------------------------------------
    # Status / helpers
    # ------------------------------------------------------------------

    async def get_file_mtimes(self, paths: list[str]) -> dict[str, float]:
        """Return {path: mtime} for given paths from the files table."""
        assert self._db is not None
        if not paths:
            return {}
        placeholders = ",".join("?" * len(paths))
        async with self._db.execute(
            f"SELECT path, mtime FROM files WHERE path IN ({placeholders})",
            paths,
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}

    async def file_count(self) -> int:
        assert self._db is not None
        async with self._db.execute("SELECT COUNT(*) FROM files") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def chunk_count(self) -> int:
        assert self._db is not None
        async with self._db.execute("SELECT COUNT(*) FROM chunks") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def total_size(self) -> int:
        """Return total size in bytes across all indexed files."""
        assert self._db is not None
        async with self._db.execute("SELECT COALESCE(SUM(size), 0) FROM files") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def source_counts(self) -> dict[str, dict[str, int]]:
        assert self._db is not None
        result: dict[str, dict[str, int]] = {}
        async with self._db.execute("SELECT source, COUNT(*) FROM files GROUP BY source") as cur:
            for row in await cur.fetchall():
                result.setdefault(row[0], {})["files"] = row[1]
        async with self._db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source") as cur:
            for row in await cur.fetchall():
                result.setdefault(row[0], {})["chunks"] = row[1]
        return result

    async def list_paths(self, source: MemorySource | None = None) -> list[str]:
        """Return indexed source paths, optionally restricted to one source."""
        assert self._db is not None
        if source is None:
            async with self._db.execute("SELECT path FROM files ORDER BY path") as cur:
                return [row[0] for row in await cur.fetchall()]
        async with self._db.execute(
            "SELECT path FROM files WHERE source = ? ORDER BY path",
            (source.value,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def vec_available(self) -> bool:
        return self._vec_available

    @property
    def fts_available(self) -> bool:
        return self._fts_available


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _load_jieba():
    global _JIEBA_WARNED
    try:
        return importlib.import_module("jieba")
    except ImportError as exc:
        if not _JIEBA_WARNED:
            logger.warning("jieba_unavailable_fallback", error=str(exc))
            _JIEBA_WARNED = True
        return None


def _dedupe_preserve_order(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _fallback_segment_run(text: str) -> str:
    tokens = [text]
    if len(text) > 1:
        tokens.extend(text[i : i + 2] for i in range(len(text) - 1))
    tokens.extend(text)
    return " ".join(_dedupe_preserve_order(tokens))


def _fallback_segment_for_fts(text: str) -> str:
    segmented = _CJK_RUN_RE.sub(lambda match: f" {_fallback_segment_run(match.group(0))} ", text)
    return re.sub(r"\s+", " ", segmented).strip()


def _segment_for_fts(text: str) -> str:
    """Segment ideographic/kana text with jieba for FTS5 indexing.

    Only Han ideographs and Japanese kana need segmentation (they are written
    without spaces). Hangul and every alphabetic script are space-delimited, so
    they pass through unchanged — jieba would otherwise shatter Korean words
    into single syllables. This runs on both the index and query sides, so the
    two stay symmetric.
    """
    if not any(_needs_jieba_segmentation(ch) for ch in text):
        return text
    jieba = _load_jieba()
    if jieba is None:
        return _fallback_segment_for_fts(text)
    return " ".join(jieba.cut(text))


# Han + Japanese kana ranges: the scripts written without word spaces that
# jieba is meant to segment. Excludes Hangul (alphabetic, space-delimited).
_JIEBA_SEGMENT_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xF900, 0xFAFF),  # CJK Compatibility
)


def _needs_jieba_segmentation(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _JIEBA_SEGMENT_RANGES)


def _build_fts_query(query: str) -> str | None:
    """Convert query to FTS5 OR query with jieba segmentation for CJK."""
    import re

    segmented = _segment_for_fts(query)
    # Unicode-aware token class ([^\W_] = any letter/digit but not underscore),
    # mirroring what the unicode61 FTS5 tokenizer treats as token characters.
    # The previous ASCII+Han+kana whitelist silently dropped every Cyrillic,
    # Hangul, Greek, Hebrew, Arabic, and accented-Latin query, making those
    # memories unretrievable. A double quote can never match [^\W_], so the
    # FTS5 query stays injection-safe.
    # Prefer multi-char tokens, which filters common single-character particles.
    tokens = re.findall(r"[^\W_]{2,}", segmented, flags=re.UNICODE)
    if not tokens:
        # Fallback: include single chars
        tokens = re.findall(r"[^\W_]+", segmented, flags=re.UNICODE)
    if not tokens:
        return None
    phrases = re.findall(r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)+", segmented)
    quoted = [f'"{p}"' for p in phrases] + [f'"{t}"' for t in tokens]
    return " OR ".join(quoted)


def _bm25_to_score(rank: float) -> float:
    """Convert BM25 rank (negative = better) to [0,1] score."""
    if rank < 0:
        relevance = -rank
        return relevance / (1.0 + relevance)
    return 1.0 / (1.0 + rank)
