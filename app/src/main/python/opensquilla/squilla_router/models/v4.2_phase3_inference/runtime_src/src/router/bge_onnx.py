"""ONNX-INT8 backend for BGE encoder.

Public surface intentionally mirrors the small slice of the
`sentence_transformers.SentenceTransformer` API used by the router:
`encode(texts, batch_size=..., show_progress_bar=..., convert_to_numpy=True)`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_DEFAULT_MAX_LENGTH = 510


class OnnxBGE:
    """Lazy, pickle-safe ONNX BGE wrapper."""

    def __init__(self, model_dir: str | Path, max_length: int = _DEFAULT_MAX_LENGTH):
        self.model_dir = str(Path(model_dir))
        self.max_length = max_length
        self._tokenizer = None
        self._session = None

    def _ensure_loaded(self):
        if self._session is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(
                str(Path(self.model_dir) / "tokenizer.json")
            )
            tokenizer.enable_truncation(max_length=self.max_length)
            pad_token = "[PAD]"
            tokenizer.enable_padding(
                pad_id=tokenizer.token_to_id(pad_token) or 0,
                pad_token=pad_token,
            )
            self._tokenizer = tokenizer
            self._session = ort.InferenceSession(
                str(Path(self.model_dir) / "model.onnx"),
                providers=["CPUExecutionProvider"],
            )
        return self._tokenizer, self._session

    def encode(
        self,
        texts,
        *,
        batch_size: int = 64,
        show_progress_bar: bool = False,
        convert_to_numpy: bool = True,
        **_kwargs,
    ) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]

        tokenizer, session = self._ensure_loaded()
        outputs = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            encoded = tokenizer.encode_batch(batch)
            ort_inputs = {
                "input_ids": np.asarray([enc.ids for enc in encoded], dtype=np.int64),
                "attention_mask": np.asarray(
                    [enc.attention_mask for enc in encoded], dtype=np.int64
                ),
                "token_type_ids": np.asarray(
                    [enc.type_ids for enc in encoded], dtype=np.int64
                ),
            }
            last_hidden = session.run(None, ort_inputs)[0]
            cls = last_hidden[:, 0, :]
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            outputs.append((cls / np.maximum(norms, 1e-12)).astype(np.float32))

        return np.concatenate(outputs, axis=0)

    def __getstate__(self):
        return {"model_dir": self.model_dir, "max_length": self.max_length}

    def __setstate__(self, state):
        self.model_dir = state["model_dir"]
        self.max_length = state["max_length"]
        self._tokenizer = None
        self._session = None
