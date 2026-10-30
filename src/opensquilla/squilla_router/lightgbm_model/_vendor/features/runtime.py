"""Frozen BGE and LightGBM feature inference definitions; see ../../PROVENANCE.md."""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np

from ..ids import canonical_json_bytes, sha256_file
from .lightgbm380 import LightGBM380Featurizer
from .text import SegmentKind, truncate_content_tokens


PCA_DIM = 64
TFIDF_MAX_FEATURES = 10_000
SVD_DIM = 100
BGE_MAX_CONTENT_TOKENS = 510
_BGE_CACHE_SCHEMA = "bge_embedding_cache.v1"
_BGE_CACHE_WRITE_BATCH = 256


def _sklearn() -> tuple[Any, Any, Any, Any]:
    try:
        from sklearn.decomposition import PCA, TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:  # pragma: no cover - exercised only in minimal installs
        raise RuntimeError("scikit-learn is required for concrete feature runtimes") from exc
    return PCA, TruncatedSVD, TfidfVectorizer, StandardScaler


def _row_mapping(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
        if isinstance(value, Mapping):
            return value
    raise TypeError("feature rows must be mappings or Pydantic models")


def _dense_2d(values: object, *, name: str) -> np.ndarray:
    result = np.asarray(values, dtype=np.float32)
    if result.ndim != 2:
        raise ValueError(f"{name} must be a 2-D matrix, got {result.shape}")
    if result.shape[0] == 0 or result.shape[1] == 0:
        raise ValueError(f"{name} must not be empty")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} contains non-finite values")
    return result


def _pad_columns(values: object, width: int, *, name: str) -> np.ndarray:
    result = np.asarray(values, dtype=np.float32)
    if result.ndim != 2 or result.shape[1] > width:
        raise ValueError(f"{name} has invalid shape {result.shape}; expected at most {width} columns")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} contains non-finite values")
    if result.shape[1] == width:
        return result
    padded = np.zeros((result.shape[0], width), dtype=np.float32)
    padded[:, : result.shape[1]] = result
    return padded


class SklearnPCA64Projector:
    """PCA fitted on train embeddings, with a fixed 64-column output."""

    output_dim = PCA_DIM

    def __init__(self) -> None:
        self.estimator: object | None = None
        self.fitted_components = 0

    def transform(self, values: object) -> np.ndarray:
        if self.estimator is None:
            raise RuntimeError("PCA projector is not fitted")
        matrix = _dense_2d(values, name="PCA input")
        return _pad_columns(self.estimator.transform(matrix), PCA_DIM, name="PCA output")

class SklearnCharTfidfVectorizer:
    """Frozen character ``char_wb`` TF-IDF configuration."""

    def __init__(self) -> None:
        _, _, TfidfVectorizer, _ = _sklearn()
        self.estimator = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            max_features=TFIDF_MAX_FEATURES,
            dtype=np.float32,
        )

    def transform(self, texts: Sequence[str]) -> object:
        return self.estimator.transform(list(texts))

class SklearnSVD100Projector:
    """TruncatedSVD fitted on train TF-IDF, with a fixed 100-column output."""

    output_dim = SVD_DIM

    def __init__(self, *, random_state: int = 17) -> None:
        self.random_state = random_state
        self.estimator: object | None = None
        self.fitted_components = 0

    def transform(self, values: object) -> np.ndarray:
        if self.estimator is None:
            raise RuntimeError("SVD projector is not fitted")
        return _pad_columns(self.estimator.transform(values), SVD_DIM, name="SVD output")

def _tokenizer_content_ids(tokenizer: object, text: str) -> list[int]:
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise TypeError("tokenizer must provide encode(text, add_special_tokens=False)")
    try:
        encoded = encode(text, add_special_tokens=False)
    except TypeError:
        encoded = encode(text)
    values = getattr(encoded, "ids", encoded)
    if isinstance(values, np.ndarray):
        values = values.tolist()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("tokenizer encode result must contain a sequence of token IDs")
    return [int(value) for value in values]


def _special_token_id(tokenizer: object, name: str, token: str) -> int:
    attribute = getattr(tokenizer, f"{name}_token_id", None)
    if attribute is not None:
        return int(attribute)
    token_to_id = getattr(tokenizer, "token_to_id", None)
    if callable(token_to_id):
        value = token_to_id(token)
        if value is not None:
            return int(value)
    convert = getattr(tokenizer, "convert_tokens_to_ids", None)
    if callable(convert):
        value = convert(token)
        if value is not None and int(value) >= 0:
            return int(value)
    raise ValueError(f"tokenizer does not define {token!r}")


def _local_model_file(model_dir: Path, explicit: str | Path | None, candidates: Sequence[str]) -> Path:
    if explicit is not None:
        path = Path(explicit)
        path = path if path.is_absolute() else model_dir / path
        if not path.is_file():
            raise FileNotFoundError(path)
        resolved = path.resolve()
        if resolved != model_dir and model_dir not in resolved.parents:
            raise ValueError("BGE files must stay inside the local model directory")
        return resolved
    for relative in candidates:
        path = model_dir / relative
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"none of {list(candidates)} exists under {model_dir}")


def _stable_runtime_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value)


def _session_options_identity(options: object) -> dict[str, object]:
    names = (
        "intra_op_num_threads",
        "inter_op_num_threads",
        "execution_mode",
        "execution_order",
        "graph_optimization_level",
        "enable_cpu_mem_arena",
        "enable_mem_pattern",
        "enable_mem_reuse",
        "use_deterministic_compute",
    )
    return {
        name: _stable_runtime_value(getattr(options, name))
        for name in names
        if hasattr(options, name)
    }


def _session_runtime_identity(
    session: object,
    *,
    explicit: Mapping[str, object] | None,
    ort_module: object | None = None,
) -> dict[str, object]:
    if explicit is not None:
        required = {"onnxruntime_version", "providers", "session_options"}
        missing = required - set(explicit)
        providers = explicit.get("providers")
        version = explicit.get("onnxruntime_version")
        session_options = explicit.get("session_options")
        if (
            missing
            or not isinstance(version, str)
            or not version.strip()
            or not isinstance(providers, Sequence)
            or isinstance(providers, (str, bytes))
            or not providers
            or any(not isinstance(provider, str) or not provider for provider in providers)
            or not isinstance(session_options, Mapping)
        ):
            raise ValueError(
                "explicit ONNX session_identity requires onnxruntime_version, "
                "non-empty providers, and session_options"
            )
        return json.loads(
            canonical_json_bytes(
                {
                    "schema_version": "bge_onnx_runtime_identity.v1",
                    "source": "explicit",
                    "identity": dict(explicit),
                }
            ).decode("utf-8")
        )

    session_type = type(session)
    if not session_type.__module__.startswith("onnxruntime."):
        raise ValueError(
            "custom ONNX sessions require an explicit stable session_identity"
        )
    if ort_module is None:
        try:
            import onnxruntime as ort_module
        except ImportError as exc:  # pragma: no cover - optional runtime dependency
            raise RuntimeError("onnxruntime is required to identify an ONNX session") from exc
    get_providers = getattr(session, "get_providers", None)
    get_session_options = getattr(session, "get_session_options", None)
    if not callable(get_providers) or not callable(get_session_options):
        raise ValueError(
            "ONNX sessions must expose get_providers() and get_session_options()"
        )
    providers = [str(provider) for provider in get_providers()]
    if not providers:
        raise ValueError("ONNX session must expose at least one active provider")
    get_provider_options = getattr(session, "get_provider_options", None)
    raw_provider_options = get_provider_options() if callable(get_provider_options) else {}
    provider_options: dict[str, object] = {}
    if isinstance(raw_provider_options, Mapping):
        for provider, options in raw_provider_options.items():
            if isinstance(options, Mapping):
                provider_options[str(provider)] = {
                    str(name): _stable_runtime_value(value)
                    for name, value in sorted(options.items(), key=lambda item: str(item[0]))
                }
            else:
                provider_options[str(provider)] = _stable_runtime_value(options)
    get_build_info = getattr(ort_module, "get_build_info", None)
    build_info = get_build_info() if callable(get_build_info) else None
    identity = {
        "schema_version": "bge_onnx_runtime_identity.v1",
        "source": "onnxruntime",
        "onnxruntime_version": str(getattr(ort_module, "__version__", "unknown")),
        "onnxruntime_build_info": str(build_info) if build_info is not None else None,
        "session_type": f"{session_type.__module__}.{session_type.__qualname__}",
        "providers": providers,
        "provider_options": provider_options,
        "session_options": _session_options_identity(get_session_options()),
    }
    return json.loads(canonical_json_bytes(identity).decode("utf-8"))


class _BGEEmbeddingCache:
    """Integrity-checked, content-addressed cache for normalized BGE rows."""

    def __init__(self, path: str | Path, *, namespace: str) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        if self.path.exists() and not self.path.is_file():
            raise ValueError("BGE embedding cache path must be a SQLite file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.connection = sqlite3.connect(self.path, timeout=60.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=60000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bge_embeddings (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                embedding_sha256 TEXT NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            ) WITHOUT ROWID
            """
        )
        self.connection.commit()

    @staticmethod
    def _decode(
        dimension: object,
        payload: object,
        payload_digest: object,
    ) -> np.ndarray | None:
        if (
            not isinstance(dimension, int)
            or dimension < 1
            or not isinstance(payload, bytes)
            or len(payload) != dimension * np.dtype("<f4").itemsize
            or not isinstance(payload_digest, str)
            or sha256(payload).hexdigest() != payload_digest
        ):
            return None
        value = np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=True)
        if value.shape != (dimension,) or not np.isfinite(value).all():
            return None
        return value

    def get_many(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        unique = tuple(dict.fromkeys(keys))
        result: dict[str, np.ndarray] = {}
        invalid: list[str] = []
        for start in range(0, len(unique), 500):
            batch = unique[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT cache_key, dimension, embedding, embedding_sha256
                FROM bge_embeddings
                WHERE namespace = ? AND cache_key IN ({placeholders})
                """,
                (self.namespace, *batch),
            )
            for cache_key, dimension, payload, payload_digest in rows:
                value = self._decode(dimension, payload, payload_digest)
                if value is None:
                    invalid.append(str(cache_key))
                else:
                    result[str(cache_key)] = value
        if invalid:
            with self.connection:
                self.connection.executemany(
                    "DELETE FROM bge_embeddings WHERE namespace = ? AND cache_key = ?",
                    ((self.namespace, cache_key) for cache_key in invalid),
                )
        return result

    def put_many(self, values: Mapping[str, np.ndarray]) -> None:
        records = []
        for cache_key, raw in values.items():
            value = np.asarray(raw, dtype="<f4")
            if value.ndim != 1 or not np.isfinite(value).all():
                raise ValueError("BGE cache accepts only finite embedding rows")
            payload = value.tobytes(order="C")
            records.append(
                (
                    self.namespace,
                    cache_key,
                    int(value.shape[0]),
                    payload,
                    sha256(payload).hexdigest(),
                )
            )
        if not records:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO bge_embeddings (
                    namespace, cache_key, dimension, embedding, embedding_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                records,
            )

    def close(self) -> None:
        self.connection.close()


class BGEOnnxEncoder:
    """Local-only BGE ONNX encoder using CLS pooling and L2 normalization."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        tokenizer_path: str | Path | None = None,
        model_path: str | Path | None = None,
        tokenizer: object | None = None,
        session: object | None = None,
        session_identity: Mapping[str, object] | None = None,
        providers: Sequence[str] = ("CPUExecutionProvider",),
        max_content_tokens: int = BGE_MAX_CONTENT_TOKENS,
        batch_size: int = 1,
        workers: int = 1,
        embedding_cache: str | Path | None = None,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        if not self.model_dir.is_dir():
            raise FileNotFoundError(self.model_dir)
        self.tokenizer_path = _local_model_file(
            self.model_dir, tokenizer_path, ("tokenizer.json",)
        )
        self.model_path = _local_model_file(
            self.model_dir, model_path, ("model.onnx", "onnx/model.onnx")
        )
        if max_content_tokens != BGE_MAX_CONTENT_TOKENS:
            raise ValueError("BGE feature contract requires exactly 510 content tokens")
        if batch_size != 1:
            raise ValueError(
                "BGE feature contract fixes batch_size=1 for offline/online parity"
            )
        if workers < 1:
            raise ValueError("BGE workers must be a positive integer")
        self.max_content_tokens = max_content_tokens
        self.batch_size = int(batch_size)
        self.workers = int(workers)

        if tokenizer is None:
            try:
                from tokenizers import Tokenizer
            except ImportError as exc:  # pragma: no cover - optional runtime dependency
                raise RuntimeError("tokenizers is required to load local tokenizer.json") from exc
            tokenizer = Tokenizer.from_file(str(self.tokenizer_path))
        ort_module: object | None = None
        if session is None:
            if session_identity is not None:
                raise ValueError(
                    "session_identity is only valid when injecting a custom session"
                )
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - optional runtime dependency
                raise RuntimeError("onnxruntime is required to run the local BGE model") from exc
            ort_module = ort
            try:
                intra_threads = int(os.environ.get("ROUTER_ORT_INTRA_OP_THREADS", "1"))
                inter_threads = int(os.environ.get("ROUTER_ORT_INTER_OP_THREADS", "1"))
            except ValueError as exc:
                raise RuntimeError("ORT thread settings must be positive integers") from exc
            if intra_threads < 1 or inter_threads < 1:
                raise RuntimeError("ORT thread settings must be positive integers")
            options = ort.SessionOptions()
            options.intra_op_num_threads = intra_threads
            options.inter_op_num_threads = inter_threads
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(self.model_path),
                sess_options=options,
                providers=list(providers),
            )
        self.tokenizer = tokenizer
        self.session = session
        self.inference_runtime_identity = _session_runtime_identity(
            session,
            explicit=session_identity,
            ort_module=ort_module,
        )
        self.cls_token_id = _special_token_id(tokenizer, "cls", "[CLS]")
        self.sep_token_id = _special_token_id(tokenizer, "sep", "[SEP]")
        self.pad_token_id = _special_token_id(tokenizer, "pad", "[PAD]")
        self.postprocess_identity = {
            "schema_version": "bge_postprocess_identity.v1",
            "numpy_version": np.__version__,
            "hidden_cast": "float32",
            "pooling": "cls",
            "normalization": "numpy.linalg.norm_l2_float32",
        }
        self._memory_cache: dict[str, np.ndarray] = {}
        cache_namespace = sha256(
            canonical_json_bytes(
                {
                    "schema_version": _BGE_CACHE_SCHEMA,
                    "model_sha256": sha256_file(self.model_path),
                    "input_width": BGE_MAX_CONTENT_TOKENS + 2,
                    "input_dtype": "int64",
                    "pad_token_id": self.pad_token_id,
                    "inference_runtime": self.inference_runtime_identity,
                    "postprocess_runtime": self.postprocess_identity,
                }
            )
        ).hexdigest()
        self._embedding_cache = (
            _BGEEmbeddingCache(embedding_cache, namespace=cache_namespace)
            if embedding_cache is not None
            else None
        )
        self._cache_hits = 0
        self._cache_misses = 0
        self._closed = False

    def metadata(self) -> dict[str, object]:
        return {
            "type": "bge_onnx",
            "model_file": self.model_path.relative_to(self.model_dir).as_posix(),
            "model_sha256": sha256_file(self.model_path),
            "tokenizer_file": self.tokenizer_path.relative_to(self.model_dir).as_posix(),
            "tokenizer_sha256": sha256_file(self.tokenizer_path),
            "pooling": "cls",
            "normalization": "l2",
            "max_content_tokens": self.max_content_tokens,
            "batch_size": self.batch_size,
            "inference_runtime": self.inference_runtime_identity,
            "postprocess_runtime": self.postprocess_identity,
            "segment_truncation": {
                "current_request": "head_tail_3_to_1",
                "recent_history": "tail",
                "previous_answer": "head_tail_1_to_1",
            },
        }

    def serving_asset_files(self) -> dict[str, Path]:
        """Return the exact local files needed to reproduce BGE inference."""

        return {
            "onnx_model": self.model_path,
            "tokenizer": self.tokenizer_path,
        }

    @property
    def cache_stats(self) -> dict[str, int]:
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "memory_entries": len(self._memory_cache),
        }

    def close(self) -> None:
        if self._closed:
            return
        if self._embedding_cache is not None:
            self._embedding_cache.close()
            self._embedding_cache = None
        self._closed = True

    def __enter__(self) -> "BGEOnnxEncoder":
        if self._closed:
            raise RuntimeError("BGE encoder is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def _input_specs(self) -> dict[str, object]:
        get_inputs = getattr(self.session, "get_inputs", None)
        if not callable(get_inputs):
            raise TypeError("ONNX session must provide get_inputs()")
        return {item.name: item for item in get_inputs()}

    @staticmethod
    def _sequence_cache_key(sequence: Sequence[int]) -> str:
        values = np.asarray(sequence, dtype="<i8")
        digest = sha256()
        digest.update(len(sequence).to_bytes(4, byteorder="little", signed=False))
        digest.update(values.tobytes(order="C"))
        return digest.hexdigest()

    def _run_sequence(
        self,
        sequence: Sequence[int],
        *,
        specs: Mapping[str, object],
        width: int,
    ) -> np.ndarray:
        if width < len(sequence):
            raise ValueError(
                "ONNX input sequence width is smaller than the 512-token contract"
            )
        input_ids = np.full((1, width), self.pad_token_id, dtype=np.int64)
        attention_mask = np.zeros_like(input_ids)
        input_ids[0, : len(sequence)] = sequence
        attention_mask[0, : len(sequence)] = 1
        available = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": np.zeros_like(input_ids),
        }
        feed = {name: available[name] for name in specs if name in available}
        outputs = self.session.run(None, feed)
        if not outputs:
            raise ValueError("ONNX BGE model returned no outputs")
        hidden = np.asarray(outputs[0], dtype=np.float32)
        if hidden.ndim == 3:
            pooled = hidden[0, 0, :]
        elif hidden.ndim == 2:
            pooled = hidden[0, :]
        else:
            raise ValueError(f"unexpected BGE output shape: {hidden.shape}")
        if pooled.ndim != 1 or not np.isfinite(pooled).all():
            raise ValueError("ONNX BGE model returned an invalid pooled row")
        return pooled

    def encode(
        self,
        texts: Sequence[str],
        *,
        segment_kinds: Sequence[SegmentKind],
    ) -> np.ndarray:
        batch = list(texts)
        kinds = list(segment_kinds)
        if self._closed:
            raise RuntimeError("BGE encoder is closed")
        if not batch or len(batch) != len(kinds):
            raise ValueError("texts and segment_kinds must be non-empty and have equal length")
        if any(not isinstance(text, str) or not text for text in batch):
            raise ValueError("empty semantic segments must bypass the BGE encoder")

        specs = self._input_specs()
        if "input_ids" not in specs:
            raise ValueError("ONNX model must expose an input_ids input")
        fixed_length: int | None = None
        shape = getattr(specs["input_ids"], "shape", None)
        if isinstance(shape, Sequence) and len(shape) > 1 and isinstance(shape[1], int):
            fixed_length = shape[1]
        # Every ONNX call retains the frozen single-row, 512-token shape.  Work
        # may run concurrently, but rows are collected in input order.
        width = fixed_length or (BGE_MAX_CONTENT_TOKENS + 2)
        results: list[np.ndarray | None] = [None] * len(batch)
        pending: dict[str, tuple[list[int], list[int]]] = {}
        for index, (text, kind) in enumerate(zip(batch, kinds, strict=True)):
            content = truncate_content_tokens(
                kind,
                _tokenizer_content_ids(self.tokenizer, text),
                max_content_tokens=self.max_content_tokens,
            )
            sequence = [self.cls_token_id, *content, self.sep_token_id]
            cache_key = self._sequence_cache_key(sequence)
            cached = self._memory_cache.get(cache_key)
            if cached is not None:
                results[index] = cached
                self._cache_hits += 1
                continue
            existing = pending.get(cache_key)
            if existing is None:
                pending[cache_key] = (sequence, [index])
            else:
                existing[1].append(index)

        if self._embedding_cache is not None and pending:
            persisted = self._embedding_cache.get_many(tuple(pending))
            for cache_key, cached in persisted.items():
                sequence, indices = pending.pop(cache_key)
                del sequence
                self._memory_cache[cache_key] = cached
                for index in indices:
                    results[index] = cached
                self._cache_hits += len(indices)

        misses = list(pending.items())
        executor = (
            ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="bge-onnx")
            if self.workers > 1
            else None
        )
        try:
            for start in range(0, len(misses), _BGE_CACHE_WRITE_BATCH):
                items = misses[start : start + _BGE_CACHE_WRITE_BATCH]
                if executor is None:
                    raw_rows = [
                        self._run_sequence(sequence, specs=specs, width=width)
                        for _, (sequence, _) in items
                    ]
                else:
                    futures = [
                        executor.submit(
                            self._run_sequence,
                            sequence,
                            specs=specs,
                            width=width,
                        )
                        for _, (sequence, _) in items
                    ]
                    raw_rows = [future.result() for future in futures]
                pooled = np.stack(raw_rows).astype(np.float32, copy=False)
                if not np.isfinite(pooled).all():
                    raise ValueError("BGE output has invalid rows or non-finite values")
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                normalized = np.divide(
                    pooled,
                    norms,
                    out=np.zeros_like(pooled),
                    where=norms > 0,
                ).astype(np.float32, copy=False)
                cache_batch: dict[str, np.ndarray] = {}
                for item_index, (cache_key, (_, indices)) in enumerate(items):
                    value = normalized[item_index]
                    cache_batch[cache_key] = value
                    self._memory_cache[cache_key] = value
                    for index in indices:
                        results[index] = value
                if self._embedding_cache is not None:
                    self._embedding_cache.put_many(cache_batch)
                self._cache_hits += sum(len(indices) - 1 for _, (_, indices) in items)
                self._cache_misses += len(items)
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)

        if any(value is None for value in results):
            raise AssertionError("BGE cache failed to materialize every input row")
        normalized = np.stack([value for value in results if value is not None])
        if normalized.shape[0] != len(batch) or not np.isfinite(normalized).all():
            raise ValueError("BGE output has invalid rows or non-finite values")
        return normalized.astype(np.float32, copy=False)


class LightGBMFeatureRuntime:
    """Concrete BGE/PCA + TF-IDF/SVD implementation of the 380 contract."""

    schema_version = "lightgbm380_runtime.v1"

    def __init__(
        self,
        *,
        embedder: object,
        semantic_projector: object | None = None,
        text_vectorizer: object | None = None,
        character_projector: object | None = None,
    ) -> None:
        self.embedder = embedder
        self.semantic_projector = semantic_projector or SklearnPCA64Projector()
        self.text_vectorizer = text_vectorizer or SklearnCharTfidfVectorizer()
        self.character_projector = character_projector or SklearnSVD100Projector()
        self._featurizer = LightGBM380Featurizer(
            embedder=self.embedder,
            semantic_projector=self.semantic_projector,
            text_vectorizer=self.text_vectorizer,
            character_projector=self.character_projector,
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LightGBM feature runtime is closed")

    def close(self) -> None:
        if self._closed:
            return
        close = getattr(self.embedder, "close", None)
        if callable(close):
            close()
        self._closed = True

    def transform_row(self, row: object) -> np.ndarray:
        self._ensure_open()
        return self._featurizer.transform_row(_row_mapping(row))
