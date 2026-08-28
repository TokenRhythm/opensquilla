"""CLI stdout/stderr encoding guards."""

from __future__ import annotations

import sys
from typing import Any


def _reconfigure_text_stream(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return

    try:
        reconfigure(encoding="utf-8", errors="replace")
    except TypeError:
        try:
            reconfigure(errors="replace")
        except (OSError, TypeError, ValueError):
            return
    except (OSError, ValueError):
        return


def configure_stdio_for_unicode() -> None:
    """Make CLI output tolerant of emoji/non-GBK text on legacy consoles."""

    _reconfigure_text_stream(sys.stdout)
    _reconfigure_text_stream(sys.stderr)
