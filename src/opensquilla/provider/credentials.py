"""Credential pool: round-robin + least-used + 429 cooldown."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlparse

_TOKENRHYTHM_KEY_RE = re.compile(r"^sk_tr_[A-Za-z0-9_-]{16,}$")
_OPENROUTER_KEY_RE = re.compile(r"^sk-or-v1-[A-Za-z0-9_-]{16,}$")


def credential_provider_hint(
    api_key: object = "",
    *,
    api_key_env: object = "",
) -> str:
    """Return a provider id only for globally distinctive credential evidence."""

    key = str(api_key or "").strip()
    if _TOKENRHYTHM_KEY_RE.fullmatch(key):
        return "tokenrhythm"
    if _OPENROUTER_KEY_RE.fullmatch(key):
        return "openrouter"
    env_name = str(api_key_env or "").strip().upper()
    if env_name == "TOKENRHYTHM_API_KEY":
        return "tokenrhythm"
    if env_name == "OPENROUTER_API_KEY":
        return "openrouter"
    return ""


def endpoint_provider_hint(base_url: object = "") -> str:
    """Return a provider id only for another provider's distinctive official host."""

    value = str(base_url or "").strip()
    if not value:
        return ""
    try:
        hostname = str(urlparse(value).hostname or "").strip().lower()
    except ValueError:
        return ""
    if hostname == "tokenrhythm.studio":
        return "tokenrhythm"
    if hostname == "openrouter.ai":
        return "openrouter"
    return ""


class NoCredentialsAvailable(RuntimeError):  # noqa: N818 - public compatibility name
    """Raised by ``CredentialPool.acquire`` when every credential is parked."""


@dataclass
class Credential:
    """A single opaque credential tracked by ``CredentialPool``.

    ``secret`` is excluded from the auto-generated ``repr`` so that logging
    or interpolating a ``Credential`` (``log.info(..., cred=cred)``,
    f-strings, tracebacks) can never leak the live key value. Identify
    credentials by ``cred_id`` (an env-var name or another non-secret id).
    """

    cred_id: str
    secret: str = field(default="", repr=False)
    acquisitions: int = 0
    _parked_until: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


class CredentialPool:
    """Round-robin pool with least-used tie-break and 429 cooldowns.

    Round-robin cycles ``1 → 2 → … → N → 1``; the tie-break only kicks
    in when multiple candidates sit at the same (lowest) acquisition
    count. Credentials that ``report_429`` are parked using
    ``time.monotonic`` so that callers cannot drift state by mutating
    the wall clock, and are re-eligible once the cooldown expires.
    """

    def __init__(
        self,
        credentials: Iterable[Credential],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        creds = list(credentials)
        if not creds:
            raise ValueError("CredentialPool requires at least one credential")
        self._creds: list[Credential] = creds
        self._cursor = 0
        self._clock = clock
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._creds)

    def available(self, cred_id: str) -> bool:
        """Return True when ``cred_id`` exists and is not currently parked.

        Wiring helper for callers that pin a credential (e.g. per-session
        pinning for prompt-cache warmth) and must check eligibility without
        advancing the round-robin cursor or the acquisition counters.
        """
        with self._lock:
            now = self._clock()
            for cred in self._creds:
                if cred.cred_id == cred_id:
                    return cred._parked_until <= now
            return False

    def acquire(self) -> Credential:
        """Return the next eligible credential.

        Starts from the current round-robin cursor, skips parked
        credentials, and breaks ties by lowest acquisition count. Raises
        ``NoCredentialsAvailable`` when everyone is in cooldown.
        """
        with self._lock:
            now = self._clock()
            n = len(self._creds)
            # Collect eligible candidates in round-robin order starting at
            # cursor; this preserves the documented 1→2→…→N→1 rotation
            # while letting the tie-break prefer the least-used.
            ordered: list[tuple[int, Credential]] = []
            for offset in range(n):
                idx = (self._cursor + offset) % n
                cred = self._creds[idx]
                if cred._parked_until <= now:
                    ordered.append((offset, cred))

            if not ordered:
                raise NoCredentialsAvailable("all credentials in cooldown")

            min_hits = min(cred.acquisitions for _, cred in ordered)
            chosen_offset, chosen = next(
                (off, cred) for off, cred in ordered if cred.acquisitions == min_hits
            )

            chosen.acquisitions += 1
            self._cursor = (self._cursor + chosen_offset + 1) % n
            return chosen

    def report_429(self, cred_id: str, cooldown_seconds: float = 60.0) -> None:
        """Park the credential identified by ``cred_id`` for ``cooldown_seconds``."""
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        with self._lock:
            for cred in self._creds:
                if cred.cred_id == cred_id:
                    cred._parked_until = self._clock() + cooldown_seconds
                    return
        raise KeyError(f"unknown credential id: {cred_id!r}")
