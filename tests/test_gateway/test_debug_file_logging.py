from __future__ import annotations

import io
import json
import logging
from logging.handlers import RotatingFileHandler

import structlog

from opensquilla.gateway.boot import _setup_file_logging
from opensquilla.gateway.config import GatewayConfig
from opensquilla.observability.cli_logging import configure_cli_structlog


def _remove_debug_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_opensquilla_debug_file_handler", False):
            root.removeHandler(handler)
            handler.close()


def _remove_console_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_opensquilla_console_log_handler", False):
            root.removeHandler(handler)
            handler.close()


def test_setup_file_logging_uses_rotation_without_forcing_root_debug(tmp_path, monkeypatch) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    root = logging.getLogger()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_root_level = root.level
    original_opensquilla_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "INFO")

    try:
        _setup_file_logging(
            GatewayConfig(
                log_level="DEBUG",
                log_file_max_bytes=4096,
                log_file_backup_count=2,
            )
        )

        handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, "_opensquilla_debug_file_handler", False)
        ]
        assert len(handlers) == 1
        handler = handlers[0]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.level == logging.INFO
        assert handler.maxBytes == 4096
        assert handler.backupCount == 2
        assert getattr(handler, "baseFilename").endswith("debug.log")
        assert root.level == original_root_level
        assert opensquilla_logger.level == logging.INFO
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        root.setLevel(original_root_level)
        opensquilla_logger.setLevel(original_opensquilla_level)


def test_setup_file_logging_can_be_disabled(tmp_path, monkeypatch) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_opensquilla_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_LOG_FILE_ENABLED", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LOG_LEVEL", raising=False)

    try:
        _setup_file_logging(GatewayConfig(log_file_enabled=False))
        assert not (tmp_path / "debug.log").exists()

        _setup_file_logging(GatewayConfig(log_level="INFO"))
        assert opensquilla_logger.level == logging.INFO
        assert (tmp_path / "debug.log").exists()
        _setup_file_logging(GatewayConfig(log_file_enabled=False))

        handlers = [
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, "_opensquilla_debug_file_handler", False)
        ]
        assert handlers == []
        # Disabling file logging must not silence the bridged console output:
        # the disabled path still pins the "opensquilla" logger to the
        # configured level (default DEBUG) rather than restoring the pre-setup
        # level, so INFO/DEBUG keep flowing to the console handler.
        assert opensquilla_logger.level == logging.DEBUG
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        opensquilla_logger.setLevel(original_opensquilla_level)


def test_disabled_file_logging_still_bridges_info_to_console(tmp_path, monkeypatch) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    old_config = structlog.get_config()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_LOG_FILE_ENABLED", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "INFO")
    try:
        structlog.reset_defaults()
        _setup_file_logging(GatewayConfig(log_file_enabled=False))
        assert not (tmp_path / "debug.log").exists()

        console_handlers = [
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, "_opensquilla_console_log_handler", False)
        ]
        assert len(console_handlers) == 1
        stream = io.StringIO()
        console_handlers[0].setStream(stream)

        structlog.get_logger("opensquilla.test_bridge").info("console_bridge_info_event")
        console_handlers[0].flush()
        assert "console_bridge_info_event" in stream.getvalue()
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        structlog.configure(**old_config)
        opensquilla_logger.setLevel(original_level)


def test_structlog_events_keep_exception_type_without_private_traceback(
    tmp_path, monkeypatch,
) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    old_config = structlog.get_config()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "DEBUG")
    try:
        structlog.reset_defaults()
        _setup_file_logging(GatewayConfig())
        slog = structlog.get_logger("opensquilla.test_bridge")
        try:
            raise ValueError("synthetic-bridge-error")
        except ValueError:
            slog.error("bridge_event", session_key="agent:test:bridge", exc_info=True)
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = (tmp_path / "debug.log").read_text(encoding="utf-8")
        event = json.loads(text.split(": ", 1)[1])
        assert event["event"] == "bridge_event"
        assert event["level"] == "ERROR"
        assert event["logger"] == "opensquilla.test_bridge"
        assert event["session_key"] == "agent:test:bridge"
        assert event["exception_type"] == "ValueError"
        assert "synthetic-bridge-error" not in text
        assert "Traceback" not in text
        assert text.count("bridge_event") == 1
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        structlog.configure(**old_config)
        opensquilla_logger.setLevel(original_level)


def test_structlog_bridge_respects_log_level(tmp_path, monkeypatch) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    old_config = structlog.get_config()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "INFO")
    try:
        structlog.reset_defaults()
        _setup_file_logging(GatewayConfig())
        structlog.get_logger("opensquilla.test_bridge").debug("bridge_debug_event")
        structlog.get_logger("opensquilla.test_bridge").info("bridge_info_event")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = (tmp_path / "debug.log").read_text(encoding="utf-8")
        assert "bridge_info_event" in text
        assert "bridge_debug_event" not in text
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        structlog.configure(**old_config)
        opensquilla_logger.setLevel(original_level)


def test_bridge_overrides_cli_structlog_default(tmp_path, monkeypatch) -> None:
    """`gateway run` enters via the CLI callback, which installs the CLI
    structlog default before boot; the bridge must treat that default as
    overridable so structlog events still reach debug.log."""
    _remove_debug_handlers()
    _remove_console_handlers()
    old_config = structlog.get_config()
    was_configured = structlog.is_configured()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "DEBUG")
    try:
        configure_cli_structlog()
        _setup_file_logging(GatewayConfig())
        structlog.get_logger("opensquilla.test_bridge").info("bridge_over_cli_default_event")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = (tmp_path / "debug.log").read_text(encoding="utf-8")
        assert "bridge_over_cli_default_event" in text
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        if was_configured:
            structlog.configure(**old_config)
        else:
            structlog.reset_defaults()
        opensquilla_logger.setLevel(original_level)


def test_bridge_respects_non_cli_explicit_configuration(tmp_path, monkeypatch) -> None:
    """An explicit non-CLI-default configuration (e.g. the interactive TUI's)
    must survive `_setup_file_logging` unchanged."""
    _remove_debug_handlers()
    _remove_console_handlers()
    old_config = structlog.get_config()
    was_configured = structlog.is_configured()
    opensquilla_logger = logging.getLogger("opensquilla")
    original_level = opensquilla_logger.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_LEVEL", "DEBUG")
    try:
        sentinel_factory = structlog.ReturnLoggerFactory()
        structlog.configure(logger_factory=sentinel_factory)
        _setup_file_logging(GatewayConfig())
        assert structlog.get_config()["logger_factory"] is sentinel_factory
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
        if was_configured:
            structlog.configure(**old_config)
        else:
            structlog.reset_defaults()
        opensquilla_logger.setLevel(original_level)


def test_bridge_failure_does_not_block_setup(tmp_path, monkeypatch) -> None:
    _remove_debug_handlers()
    _remove_console_handlers()
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    import opensquilla.gateway.boot as boot_module

    def _boom() -> None:
        raise RuntimeError("synthetic bridge failure")

    monkeypatch.setattr(boot_module, "_bridge_structlog_to_stdlib", _boom)
    try:
        _setup_file_logging(GatewayConfig())
        assert (tmp_path / "debug.log").exists()
    finally:
        _remove_debug_handlers()
        _remove_console_handlers()
