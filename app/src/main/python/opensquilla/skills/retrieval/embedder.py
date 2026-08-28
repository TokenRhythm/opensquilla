"""Process-level singleton accessor for the local embedding provider.

The accessor lives outside memory/ because it is owned by the skill
retrieval subsystem; memory/embedding.py is the Protocol implementation.
Sharing a single embedder across skill filter and squilla_router avoids
loading the BGE weights twice.
"""

from __future__ import annotations

import threading

from opensquilla.memory.embedding import LocalEmbeddingProvider

_lock = threading.Lock()
_instances: dict[str, LocalEmbeddingProvider] = {}


def get_embedder(model_name: str | None = None) -> LocalEmbeddingProvider:
    """Return a process-wide LocalEmbeddingProvider keyed by model name.

    Lazy-constructs on first call per model. The underlying ONNX session
    is loaded by LocalEmbeddingProvider on first encode, not here.
    Raises nothing on its own; if onnxruntime / tokenizers / the
    bundled ONNX dir are missing, the corresponding ImportError or
    RuntimeError is surfaced when the caller invokes encode_sync /
    embed_query / embed_batch.
    """
    name = model_name or LocalEmbeddingProvider.DEFAULT_MODEL
    with _lock:
        inst = _instances.get(name)
        if inst is None:
            inst = LocalEmbeddingProvider(name)
            _instances[name] = inst
        return inst
