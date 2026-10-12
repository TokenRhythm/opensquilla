"""Helpers for normalizing user-supplied secrets."""

from __future__ import annotations

_PASTE_BOUNDARY_CHARACTERS = "、，。；;：:,. \t\r\n"


def clean_header_secret(value: str | None, *, label: str = "API key") -> str:
    """Return a secret safe to place in an HTTP header value.

    API keys are often pasted from chat or docs. On Windows terminals it is easy
    to carry a boundary full-width punctuation mark into the password prompt;
    strip those boundary characters while rejecting non-ASCII text that remains.
    """

    cleaned = str(value or "").strip(_PASTE_BOUNDARY_CHARACTERS)
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{label} contains non-ASCII characters; remove copied punctuation "
            "and paste the key again."
        ) from exc
    return cleaned
