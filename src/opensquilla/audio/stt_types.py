"""Provider-neutral speech-to-text transcription types.

The gateway route (and future tools) hand an :class:`SttAudio` to any
:class:`SttProvider`; each adapter maps it onto its wire protocol and
normalizes the outcome into :class:`SttResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SttAudio:
    """One recorded audio payload ready for transcription."""

    data: bytes
    filename: str
    mime_type: str


@dataclass(frozen=True)
class SttResult:
    """Normalized transcription outcome across providers."""

    text: str
    provider: str
    model: str
    language_code: str | None = None
    language_probability: float | None = None


class SttProviderError(RuntimeError):
    """Provider or transport failure surfaced to callers as one type."""


@runtime_checkable
class SttProvider(Protocol):
    """One configurable transcription backend.

    ``accepts_request_model`` marks providers that trust a caller-selected
    model id (WebUI history: ElevenLabs Scribe variants). Providers whose
    model is fixed by configuration must ignore it and keep the flag False
    so a stale client-side model id cannot leak across backends.
    """

    provider_id: str
    accepts_request_model: bool

    async def transcribe(
        self,
        audio: SttAudio,
        *,
        model_id: str | None = None,
        language: str | None = None,
    ) -> SttResult: ...
