"""Embedding provider abstraction and implementations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.app_attribution import provider_app_headers

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    import numpy as np

EMBEDDING_BATCH_MAX_BYTES = 8000
BATCH_FAILURE_LIMIT = 2
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_MS = 500
DEFAULT_RETRY_MAX_MS = 8000

RETRYABLE_ERROR_PATTERNS = [
    "rate_limit",
    "429",
    "too many requests",
    "500",
    "502",
    "503",
    "504",
    "cloudflare",
]


def _is_retryable(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(p in msg for p in RETRYABLE_ERROR_PATTERNS)


async def _retry_with_backoff(
    fn,
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    base_ms: int = DEFAULT_RETRY_BASE_MS,
    max_ms: int = DEFAULT_RETRY_MAX_MS,
):
    import asyncio

    last_error = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1 and _is_retryable(str(e)):
                delay = min(base_ms * (2**attempt), max_ms) / 1000.0
                logger.warning("embedding_retry", attempt=attempt + 1, delay=delay, error=str(e))
                await asyncio.sleep(delay)
            else:
                raise
    raise last_error  # type: ignore[misc]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    @property
    def provider_id(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query text."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, returning one vector per text."""
        ...

    async def probe(self) -> tuple[bool, str | None]:
        """Check if this provider is available. Returns (ok, error)."""
        ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embedding provider (also works with OpenRouter)."""

    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        extra_headers: dict[str, str] | None = None,
        dimensions: int | None = None,
        timeout_query_ms: int = 60_000,
        timeout_batch_ms: int = 120_000,
    ) -> None:
        self._api_key = api_key
        self._model = model or self.DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._extra_headers = dict(extra_headers or {})
        self._dimensions = dimensions
        self._timeout_query = timeout_query_ms / 1000.0
        self._timeout_batch = timeout_batch_ms / 1000.0

    @property
    def provider_id(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        headers.update(provider_app_headers(self._base_url))
        headers.update(self._extra_headers)
        return headers

    def _payload(self, input_value: str | list[str]) -> dict[str, object]:
        payload: dict[str, object] = {"input": input_value, "model": self._model}
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions
        return payload

    async def embed_query(self, text: str) -> list[float]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json=self._payload(text),
                    timeout=self._timeout_query,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json=self._payload(texts),
                    timeout=self._timeout_batch,
                )
                resp.raise_for_status()
                data = resp.json()
                # API returns results in order
                results = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in results]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:
            return False, str(e)


class OllamaEmbeddingProvider:
    """Ollama local embedding provider."""

    DEFAULT_MODEL = "nomic-embed-text"

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "http://localhost:11434",
        timeout_query_ms: int = 300_000,
        timeout_batch_ms: int = 600_000,
    ) -> None:
        self._model = model or self.DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._timeout_query = timeout_query_ms / 1000.0
        self._timeout_batch = timeout_batch_ms / 1000.0

    @property
    def provider_id(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    async def _embed(self, inputs: list[str], timeout: float) -> list[list[float]]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self._model, "input": inputs},
                    timeout=timeout,
                )
                resp.raise_for_status()
                return resp.json()["embeddings"]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text], self._timeout_query)
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # /api/embed embeds the whole batch in one request (the legacy
        # /api/embeddings endpoint only took a single prompt).
        if not texts:
            return []
        return await self._embed(texts, self._timeout_batch)

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:
            return False, str(e)


class NullEmbeddingProvider:
    """Placeholder when no real embedding provider is configured. FTS-only mode."""

    @property
    def provider_id(self) -> str:
        return "none"

    @property
    def model(self) -> str:
        return "fts-only"

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("NullEmbeddingProvider cannot produce embeddings")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("NullEmbeddingProvider cannot produce embeddings")

    async def probe(self) -> tuple[bool, str | None]:
        return False, "no embedding provider configured"


class LocalEmbeddingProvider:
    """Local embedding provider — ONNX-only backend.

    Loads the bundled v4 router BGE ONNX export
    (``squilla_router/models/v4.2_phase3_inference/bge_onnx/``) via
    ``onnxruntime`` + the Hugging Face ``tokenizers`` runtime. There is no
    sentence-transformers / torch path: the project ships an INT8 BGE
    ONNX so the FP32 sentence-transformers download is unnecessary
    weight, and earlier dual-backend coexistence created a real
    train/inference distribution mismatch when callers silently
    swapped between FP32 and INT8.

    Produces raw (un-normalised) float32 vectors of shape ``(N, dim)``;
    downstream callers (e.g. ``SemanticIndex``) normalise or reduce
    themselves.

    Failure modes:
      * ``onnxruntime`` or ``tokenizers`` not installed → ``ImportError``
      * bundled ONNX dir / weights missing → ``RuntimeError``
      * runtime error during model load → ``RuntimeError``

    All raise on first ``encode_sync`` (lazy load); construction itself
    is side-effect-free so importers can probe ``loaded`` first.
    """

    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

    _BUNDLED_V4_ONNX_DIR = (
        Path(__file__).resolve().parents[1]
        / "squilla_router"
        / "models"
        / "v4.2_phase3_inference"
        / "bge_onnx"
    )

    @classmethod
    def _bundled_onnx_dir(cls, model_name: str) -> Path | None:
        """Resolve the bundled ONNX dir for the given model. Returns None
        when no bundled export is available."""
        if model_name == cls.DEFAULT_MODEL and cls._BUNDLED_V4_ONNX_DIR.is_dir():
            return cls._BUNDLED_V4_ONNX_DIR
        short = model_name.split("/")[-1]
        candidate = cls._BUNDLED_V4_ONNX_DIR.parent / "embeddings" / f"{short}-int8"
        return candidate if candidate.is_dir() else None

    def __init__(
        self,
        model_name: str | None = None,
        *,
        onnx_dir: Path | str | None = "auto",
    ) -> None:
        import threading

        self._model_name = model_name or self.DEFAULT_MODEL
        # ONNX dir resolution. Two contract values:
        #   "auto" (default) → convention-based bundled discovery
        #   <path>           → use exactly this dir
        # ``None`` is rejected explicitly: the previous semantics
        # (force sentence-transformers fallback) no longer exists.
        if onnx_dir == "auto":
            self._onnx_dir: Path | None = self._bundled_onnx_dir(self._model_name)
        elif onnx_dir is None:
            raise ValueError(
                "onnx_dir=None is no longer supported; the sentence-transformers "
                "fallback was removed. Pass an explicit ONNX directory or omit "
                'the argument to use the bundled "auto" discovery.'
            )
        else:
            self._onnx_dir = Path(onnx_dir).expanduser().resolve()
        self._dim: int | None = None
        self._loaded: bool = False
        # ONNX backend state — populated on first encode.
        self._onnx_session: Any | None = None
        self._onnx_tokenizer: Any | None = None
        self._onnx_input_names: list[str] = []
        # Guards _ensure_loaded so concurrent first-time encode_sync calls
        # don't each construct their own session. Without this,
        # get_embedder()'s singleton guarantee only covers the provider
        # object — the underlying weights would still load twice.
        self._load_lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def backend(self) -> str | None:
        """Always ``"onnx"`` after the first successful encode, ``None``
        before. Kept as an attribute (rather than a constant) so existing
        callers/tests can probe whether the provider has actually loaded."""
        return "onnx" if self._loaded else None

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("LocalEmbeddingProvider not loaded yet")
        return self._dim

    def _ensure_loaded(self) -> None:
        # Double-checked locking: the fast path is unlocked so subsequent
        # calls don't pay a lock acquisition; only the first one (or
        # several concurrent firsts) pays the lock and the load.
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._load_onnx()
            self._loaded = True

    def _load_onnx(self) -> None:
        """Load the ONNX backend. Raises with an actionable message on
        any failure — there is no fallback to retry against."""
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ImportError(
                "LocalEmbeddingProvider requires `onnxruntime` and "
                "`tokenizers`. Install via "
                "`uv sync --extra recommended`."
            ) from exc
        if self._onnx_dir is None or not self._onnx_dir.is_dir():
            raise RuntimeError(
                f"LocalEmbeddingProvider could not locate a bundled ONNX dir "
                f"for model {self._model_name!r}; got onnx_dir={self._onnx_dir!r}. "
                "The bundled v4 router export should ship at "
                "src/opensquilla/squilla_router/models/v4.2_phase3_inference/bge_onnx/."
            )
        onnx_files = sorted(self._onnx_dir.glob("*.onnx"))
        if not onnx_files:
            raise RuntimeError(
                f"LocalEmbeddingProvider found onnx_dir={self._onnx_dir} but "
                "it contains no *.onnx files."
            )
        try:
            session = ort.InferenceSession(str(onnx_files[0]), providers=["CPUExecutionProvider"])
            tokenizer_path = self._onnx_dir / "tokenizer.json"
            if not tokenizer_path.is_file():
                raise RuntimeError(f"tokenizer.json not found in {self._onnx_dir}")
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            tokenizer.enable_truncation(max_length=512)
            tokenizer.enable_padding()
            input_names = [inp.name for inp in session.get_inputs()]
            # Warm-up + dim discovery via a dummy encode.
            self._onnx_tokenizer = tokenizer
            feed = self._tokenize_onnx(["warmup"], input_names)
            out = session.run(None, feed)[0]
            if out.ndim == 3:
                out = out[:, 0, :]  # CLS pooling for BGE-style models
        except Exception as exc:
            logger.warning("local_embedding.onnx_load_failed", error=str(exc))
            raise RuntimeError(
                f"LocalEmbeddingProvider failed to initialise ONNX session "
                f"from {onnx_files[0]}: {exc}"
            ) from exc
        self._onnx_session = session
        self._onnx_tokenizer = tokenizer
        self._onnx_input_names = input_names
        self._dim = int(out.shape[-1])

    def _tokenize_onnx(self, texts: list[str], input_names: list[str]) -> dict[str, Any]:
        import numpy as np

        assert self._onnx_tokenizer is not None
        encodings = self._onnx_tokenizer.encode_batch(texts)  # type: ignore[union-attr]
        arrays: dict[str, Any] = {
            "input_ids": np.asarray([enc.ids for enc in encodings], dtype=np.int64),
            "attention_mask": np.asarray(
                [enc.attention_mask for enc in encodings],
                dtype=np.int64,
            ),
            "token_type_ids": np.asarray([enc.type_ids for enc in encodings], dtype=np.int64),
        }
        return {name: value for name, value in arrays.items() if name in input_names}

    def encode_sync(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
        show_progress_bar: bool = False,  # accepted for API compat; unused
    ) -> np.ndarray:  # noqa: F821
        self._ensure_loaded()
        return self._encode_onnx(list(texts), batch_size=batch_size)

    def _encode_onnx(self, texts: list[str], *, batch_size: int) -> np.ndarray:  # noqa: F821
        import numpy as np

        assert self._onnx_session is not None
        assert self._onnx_tokenizer is not None
        if not texts:
            dim = self._dim or 0
            return np.zeros((0, dim), dtype=np.float32)
        chunks: list[np.ndarray] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            feed = self._tokenize_onnx(batch, self._onnx_input_names)
            outputs = self._onnx_session.run(None, feed)[0]  # type: ignore[union-attr]
            if outputs.ndim == 3:
                outputs = outputs[:, 0, :]  # CLS pooling
            chunks.append(np.asarray(outputs, dtype=np.float32))
        return np.concatenate(chunks, axis=0)

    async def embed_query(self, text: str) -> list[float]:
        import asyncio

        arr = await asyncio.to_thread(self.encode_sync, [text])
        return cast(list[float], arr[0].tolist())

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        arr = await asyncio.to_thread(self.encode_sync, list(texts))
        return [row.tolist() for row in arr]

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:  # ImportError or model load failure
            return False, str(e)


_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xF900, 0xFAFF),  # CJK Compatibility
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _estimate_tokens(text: str) -> int:
    """CJK chars ≈ 1 token each; ASCII ≈ 4 chars per token."""
    if not text:
        return 0
    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    ascii_count = len(text) - cjk_count
    return max(1, cjk_count + ascii_count // 4)


def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def chunk_text(
    text: str, chunk_tokens: int = 400, chunk_overlap: int = 50
) -> list[tuple[int, int, str]]:
    """
    Split text into overlapping chunks by approximate token count.
    Returns list of (start_line, end_line, chunk_text).
    CJK chars count as 1 token each; ASCII uses 4 chars per token.
    """
    lines = text.splitlines(keepends=True)
    chunks: list[tuple[int, int, str]] = []

    current_tokens = 0
    chunk_start_line = 0

    i = 0
    while i < len(lines):
        line_tokens = _estimate_tokens(lines[i])
        if current_tokens + line_tokens > chunk_tokens and current_tokens > 0:
            # emit chunk
            chunk_text_val = "".join(lines[chunk_start_line:i]).strip()
            if chunk_text_val:
                chunks.append((chunk_start_line + 1, i, chunk_text_val))

            # move back by overlap
            overlap_tokens_back = 0
            new_start = i
            for j in range(i - 1, chunk_start_line - 1, -1):
                overlap_tokens_back += _estimate_tokens(lines[j])
                if overlap_tokens_back >= chunk_overlap:
                    new_start = j
                    break
            chunk_start_line = new_start
            current_tokens = sum(_estimate_tokens(lines[k]) for k in range(chunk_start_line, i))
        current_tokens += line_tokens
        i += 1

    # final chunk
    if chunk_start_line < len(lines):
        chunk_text_val = "".join(lines[chunk_start_line:]).strip()
        if chunk_text_val:
            chunks.append((chunk_start_line + 1, len(lines), chunk_text_val))

    return chunks if chunks else [(1, len(lines), text.strip())]
