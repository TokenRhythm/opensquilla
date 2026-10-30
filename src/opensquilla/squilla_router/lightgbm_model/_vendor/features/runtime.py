"""Frozen BGE and LightGBM feature inference definitions; see ../../PROVENANCE.md."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np

from .text import SegmentKind, truncate_content_tokens


PCA_DIM = 64
SVD_DIM = 100
BGE_MAX_CONTENT_TOKENS = 510
_BGE_CACHE_WRITE_BATCH = 256


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
    estimator: Any  # Restored from the frozen joblib state.

    def transform(self, values: object) -> np.ndarray:
        if self.estimator is None:
            raise RuntimeError("PCA projector is not fitted")
        matrix = _dense_2d(values, name="PCA input")
        return _pad_columns(self.estimator.transform(matrix), PCA_DIM, name="PCA output")

class SklearnCharTfidfVectorizer:
    """Frozen character ``char_wb`` TF-IDF configuration."""

    estimator: Any  # Restored from the frozen joblib state.

    def transform(self, texts: Sequence[str]) -> object:
        return self.estimator.transform(list(texts))

class SklearnSVD100Projector:
    """TruncatedSVD fitted on train TF-IDF, with a fixed 100-column output."""

    output_dim = SVD_DIM
    estimator: Any  # Restored from the frozen joblib state.

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
        providers: Sequence[str] = ("CPUExecutionProvider",),
        max_content_tokens: int = BGE_MAX_CONTENT_TOKENS,
        batch_size: int = 1,
        workers: int = 1,
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
        if session is None:
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - optional runtime dependency
                raise RuntimeError("onnxruntime is required to run the local BGE model") from exc
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
        self.session: Any = session
        self.cls_token_id = _special_token_id(tokenizer, "cls", "[CLS]")
        self.sep_token_id = _special_token_id(tokenizer, "sep", "[SEP]")
        self.pad_token_id = _special_token_id(tokenizer, "pad", "[PAD]")
        self._memory_cache: dict[str, np.ndarray] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

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
                for item_index, (cache_key, (_, indices)) in enumerate(items):
                    value = normalized[item_index]
                    self._memory_cache[cache_key] = value
                    for index in indices:
                        results[index] = value
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
