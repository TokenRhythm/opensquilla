from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryIndexMeta:
    model: str
    provider: str
    chunk_tokens: int
    chunk_overlap: int
    vector_dims: int | None
    fts_tokenizer: str
    sources: list[str]
    provider_fingerprint: str | None = None

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> MemoryIndexMeta | None:
        if raw is None:
            return None
        d: dict[str, Any] = json.loads(raw)
        valid_fields = {field.name for field in dataclasses.fields(cls)}
        d = {key: value for key, value in d.items() if key in valid_fields}
        return cls(**d)

    def requires_reindex(self, other: MemoryIndexMeta) -> bool:
        if self.model != other.model:
            return True
        if self.provider != other.provider:
            return True
        if self.provider_fingerprint != other.provider_fingerprint:
            return True
        if self.chunk_tokens != other.chunk_tokens:
            return True
        if self.chunk_overlap != other.chunk_overlap:
            return True
        if self.vector_dims != other.vector_dims:
            return True
        if self.fts_tokenizer != other.fts_tokenizer:
            return True
        if sorted(self.sources) != sorted(other.sources):
            return True
        return False
