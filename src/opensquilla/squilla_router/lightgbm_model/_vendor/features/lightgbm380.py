"""Injectable, train-split-safe LightGBM 380-dimensional feature pipeline.

This module creates features only. It intentionally does not import LightGBM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

import numpy as np

from .numeric import extract_numeric88
from .registry import NUMERIC88_REGISTRY
from .text import SEGMENT_KINDS, SegmentKind, TextSegments, extract_text_segments


SEMANTIC_DIM = 64
CHARACTER_DIM = 100
FEATURE_DIM = 380


class EmbeddingEncoder(Protocol):
    def encode(
        self,
        texts: Sequence[str],
        *,
        segment_kinds: Sequence[SegmentKind],
    ) -> object: ...


class Projector(Protocol):
    def fit(self, values: object) -> object: ...

    def transform(self, values: object) -> object: ...


class TextVectorizer(Protocol):
    def fit_transform(self, texts: Sequence[str]) -> object: ...

    def transform(self, texts: Sequence[str]) -> object: ...


def _numeric_names(start: int, stop: int) -> tuple[str, ...]:
    return tuple(feature.name for feature in NUMERIC88_REGISTRY[start:stop])


FEATURE_NAMES_380 = (
    tuple(f"current.semantic.{index:03d}" for index in range(SEMANTIC_DIM))
    + tuple(f"current.character.{index:03d}" for index in range(CHARACTER_DIM))
    + _numeric_names(0, 48)
    + tuple(f"history.semantic.{index:03d}" for index in range(SEMANTIC_DIM))
    + _numeric_names(48, 64)
    + tuple(f"previous.semantic.{index:03d}" for index in range(SEMANTIC_DIM))
    + _numeric_names(64, 83)
    + _numeric_names(83, 88)
)

if len(FEATURE_NAMES_380) != FEATURE_DIM:
    raise AssertionError(f"feature registry has {len(FEATURE_NAMES_380)} entries")


def _matrix(values: object, *, rows: int | None = None, columns: int | None = None) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"transformer output must be 2-D, got shape {matrix.shape}")
    if rows is not None and matrix.shape[0] != rows:
        raise ValueError(f"transformer output has {matrix.shape[0]} rows, expected {rows}")
    if columns is not None and matrix.shape[1] != columns:
        raise ValueError(f"transformer output has {matrix.shape[1]} columns, expected {columns}")
    if not np.isfinite(matrix).all():
        raise ValueError("transformer output contains non-finite values")
    return matrix


class LightGBM380Featurizer:
    """Fit PCA/TF-IDF/SVD on explicit train rows and transform canonical rows."""

    def __init__(
        self,
        *,
        embedder: EmbeddingEncoder,
        semantic_projector: Projector,
        text_vectorizer: TextVectorizer,
        character_projector: Projector,
    ) -> None:
        self._embedder = embedder
        self._semantic_projector = semantic_projector
        self._text_vectorizer = text_vectorizer
        self._character_projector = character_projector
        self._fitted = False

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(self, train_rows: Sequence[Mapping[str, object]]) -> "LightGBM380Featurizer":
        if self._fitted:
            raise RuntimeError("featurizer is immutable after fit; create a new version")
        rows = list(train_rows)
        if not rows:
            raise ValueError("train_rows must not be empty")
        invalid = [row.get("sample_id", "<unknown>") for row in rows if row.get("split") != "train"]
        if invalid:
            raise ValueError(f"fit accepts only rows explicitly marked split='train': {invalid[:5]}")

        segments = [extract_text_segments(row) for row in rows]
        nonempty = [
            (kind, text)
            for item in segments
            for kind, text in zip(SEGMENT_KINDS, item.as_tuple(), strict=True)
            if text
        ]
        if not nonempty:
            raise ValueError("train rows contain no non-empty text segments")
        embeddings = _matrix(
            self._embedder.encode(
                [text for _, text in nonempty],
                segment_kinds=[kind for kind, _ in nonempty],
            ),
            rows=len(nonempty),
        )
        self._semantic_projector.fit(embeddings)

        current_texts = [item.current_request for item in segments]
        tfidf_matrix = self._text_vectorizer.fit_transform(current_texts)
        self._character_projector.fit(tfidf_matrix)
        # Fail during fit if injected components cannot satisfy the frozen widths.
        projected = _matrix(
            self._semantic_projector.transform(embeddings[:1]), rows=1, columns=SEMANTIC_DIM
        )
        del projected
        reduced = _matrix(
            self._character_projector.transform(self._text_vectorizer.transform(current_texts[:1])),
            rows=1,
            columns=CHARACTER_DIM,
        )
        del reduced
        self._fitted = True
        return self

    def _semantic_blocks(self, segments: TextSegments) -> np.ndarray:
        texts = segments.as_tuple()
        result = np.zeros((3, SEMANTIC_DIM), dtype=np.float32)
        positions = [index for index, text in enumerate(texts) if text]
        if positions:
            embeddings = _matrix(
                self._embedder.encode(
                    [texts[index] for index in positions],
                    segment_kinds=[SEGMENT_KINDS[index] for index in positions],
                ),
                rows=len(positions),
            )
            projected = _matrix(
                self._semantic_projector.transform(embeddings),
                rows=len(positions),
                columns=SEMANTIC_DIM,
            )
            result[positions] = projected
        return result

    def transform_row(self, row: Mapping[str, object]) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("call fit(train_rows) before transform_row")
        segments = extract_text_segments(row)
        semantics = self._semantic_blocks(segments)
        character = _matrix(
            self._character_projector.transform(
                self._text_vectorizer.transform([segments.current_request])
            ),
            rows=1,
            columns=CHARACTER_DIM,
        )[0]
        numeric = extract_numeric88(row)
        result = np.concatenate(
            (
                semantics[0],
                character,
                numeric[0:48],
                semantics[1],
                numeric[48:64],
                semantics[2],
                numeric[64:83],
                numeric[83:88],
            )
        ).astype(np.float32)
        if result.shape != (FEATURE_DIM,):
            raise AssertionError(f"feature vector shape drifted to {result.shape}")
        if not np.isfinite(result).all():
            raise ValueError("feature vector contains non-finite values")
        return result

    def transform_rows(self, rows: Sequence[Mapping[str, object]]) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("call fit(train_rows) before transform_rows")
        values = list(rows)
        if not values:
            return np.empty((0, FEATURE_DIM), dtype=np.float32)
        segments = [extract_text_segments(row) for row in values]
        semantic = np.zeros((len(values), 3, SEMANTIC_DIM), dtype=np.float32)
        positions = [
            (row_index, segment_index, kind, text)
            for row_index, item in enumerate(segments)
            for segment_index, (kind, text) in enumerate(
                zip(SEGMENT_KINDS, item.as_tuple(), strict=True)
            )
            if text
        ]
        if positions:
            embeddings = _matrix(
                self._embedder.encode(
                    [item[3] for item in positions],
                    segment_kinds=[item[2] for item in positions],
                ),
                rows=len(positions),
            )
            projected = _matrix(
                self._semantic_projector.transform(embeddings),
                rows=len(positions),
                columns=SEMANTIC_DIM,
            )
            for index, (row_index, segment_index, _, _) in enumerate(positions):
                semantic[row_index, segment_index] = projected[index]

        current_texts = [item.current_request for item in segments]
        character = _matrix(
            self._character_projector.transform(
                self._text_vectorizer.transform(current_texts)
            ),
            rows=len(values),
            columns=CHARACTER_DIM,
        )
        numeric = np.stack([extract_numeric88(row) for row in values]).astype(
            np.float32, copy=False
        )
        result = np.concatenate(
            (
                semantic[:, 0, :],
                character,
                numeric[:, 0:48],
                semantic[:, 1, :],
                numeric[:, 48:64],
                semantic[:, 2, :],
                numeric[:, 64:83],
                numeric[:, 83:88],
            ),
            axis=1,
        ).astype(np.float32, copy=False)
        if result.shape != (len(values), FEATURE_DIM):
            raise AssertionError(f"feature matrix shape drifted to {result.shape}")
        if not np.isfinite(result).all():
            raise ValueError("feature matrix contains non-finite values")
        return result
