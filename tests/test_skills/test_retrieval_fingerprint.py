from __future__ import annotations

import numpy as np

from opensquilla.skills.retrieval import HybridRetriever
from opensquilla.skills.retrieval.semantic import SemanticIndex
from opensquilla.skills.types import SkillLayer, SkillSpec


def _skill(*, description: str, triggers: list[str]) -> SkillSpec:
    return SkillSpec(
        name="same-name",
        description=description,
        layer=SkillLayer.MANAGED,
        always=False,
        triggers=triggers,
        content="body",
    )


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.ones((len(texts), 2), dtype=np.float32)


def test_same_name_metadata_change_rebuilds_lexical_index() -> None:
    retriever = HybridRetriever(strategy="lexical")
    first = _skill(description="alpha", triggers=["one"])
    second = _skill(description="beta", triggers=["two"])

    retriever.retrieve([first], "alpha")
    first_index = retriever._lexical
    retriever.retrieve([second], "beta")

    assert retriever._lexical is not first_index


def test_same_name_metadata_change_rebuilds_semantic_index_but_keeps_skill_id() -> None:
    embedder = _RecordingEmbedder()
    index = SemanticIndex(embedder)
    first = _skill(description="alpha", triggers=["one"])
    second = _skill(description="beta", triggers=["two"])

    index.build([first])
    index.build([second])

    assert len(embedder.calls) == 2
    assert index._ids == ["same-name"]
