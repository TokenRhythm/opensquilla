"""Process-wide structlog default for CLI entry points.

The CLI process historically never configured structlog, so any
``structlog.get_logger`` call on a CLI code path fell back to structlog's
default ``PrintLogger`` and wrote debug/info events to **stdout**, polluting
command output (piped output, ``--json`` modes, and scripted callers all saw
the noise). :func:`configure_cli_structlog` installs a CLI-wide default
instead: events go to **stderr** and only WARNING and above pass the filter.

The default is deliberately recognizable via :func:`is_cli_default_active` so
surfaces that install a richer configuration later keep working:

* ``opensquilla gateway run`` enters through the same CLI callback; the
  gateway's structlog-to-stdlib bridge (``gateway/boot.py``) treats the CLI
  default as overridable so debug.log diagnostics stay intact, while any
  *other* explicit configuration is still respected.
* The interactive TUI (``cli/tui/adapters/launch_bridge.py``) simply
  configures later in the same process — last writer wins.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_CLI_DEFAULT: tuple[Any, Any] | None = None


class _StderrPrintLoggerFactory:
    """PrintLogger factory that resolves ``sys.stderr`` per emission.

    ``structlog.PrintLoggerFactory(file=sys.stderr)`` would pin the stream
    object captured at configure time; test harnesses (CliRunner, capsys) swap
    ``sys.stderr`` per invocation, so late-bind instead. With
    ``cache_logger_on_first_use`` False the factory runs on each emission and
    picks up the current stream.
    """

    def __call__(self, *args: Any) -> structlog.PrintLogger:
        return structlog.PrintLogger(file=sys.stderr)


def configure_cli_structlog() -> None:
    """Install the CLI-wide structlog default: stderr output, WARNING+ only."""
    global _CLI_DEFAULT
    wrapper_class = structlog.make_filtering_bound_logger(logging.WARNING)
    logger_factory = _StderrPrintLoggerFactory()
    structlog.configure(
        wrapper_class=wrapper_class,
        logger_factory=logger_factory,
        cache_logger_on_first_use=False,
    )
    _CLI_DEFAULT = (wrapper_class, logger_factory)


def is_cli_default_active() -> bool:
    """True while the configuration from :func:`configure_cli_structlog` is current.

    Self-invalidating: any other ``structlog.configure`` (or
    ``structlog.reset_defaults``) replaces the wrapper class or logger factory,
    so the identity check below fails and callers such as the gateway bridge
    treat the active configuration as explicit and leave it alone. Swapping
    processors only (e.g. ``structlog.testing.capture_logs``) keeps the
    default recognized.
    """
    if _CLI_DEFAULT is None or not structlog.is_configured():
        return False
    config = structlog.get_config()
    wrapper_class, logger_factory = _CLI_DEFAULT
    return (
        config.get("wrapper_class") is wrapper_class
        and config.get("logger_factory") is logger_factory
    )
