from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.runtime import (
    _resolve_finalize_evidence_gate,
    _resolve_identity_prompt_mode,
    _resolve_legacy_prompt_style,
    _resolve_patch_evidence_protocol,
    _resolve_submit_review,
)
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _finalize_evidence_gate_from_env,
    _finalize_evidence_strict_from_env,
)
from opensquilla.gateway.config import GatewayConfig


def test_identity_prompt_mode_auto_preserves_full_default() -> None:
    assert _resolve_identity_prompt_mode(GatewayConfig()) == "full"


def test_identity_prompt_mode_auto_preserves_memory_only_minimal() -> None:
    cfg = GatewayConfig(tools={"profile": "memory_only"})

    assert _resolve_identity_prompt_mode(cfg) == "minimal"


def test_identity_prompt_mode_explicit_value_overrides_auto_tool_profile() -> None:
    cfg = GatewayConfig(
        prompt={"mode": "headless_source_edit"},
        tools={"profile": "memory_only"},
    )

    assert _resolve_identity_prompt_mode(cfg) == "headless_source_edit"


def test_identity_prompt_mode_accepts_headless_repo_coding_scaffold() -> None:
    cfg = GatewayConfig(prompt={"mode": "headless_repo_coding_scaffold"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_repo_coding_scaffold"


def test_identity_prompt_mode_short_env_alias_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROMPT_MODE", "headless_source_edit")
    cfg = GatewayConfig(prompt={"mode": "auto"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_source_edit"


def test_identity_prompt_mode_env_accepts_headless_repo_coding_scaffold(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROMPT_MODE", "headless_repo_coding_scaffold")
    cfg = GatewayConfig(prompt={"mode": "auto"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_repo_coding_scaffold"


def test_patch_evidence_protocol_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", raising=False)

    assert _resolve_patch_evidence_protocol(GatewayConfig()) is False


def test_patch_evidence_protocol_config_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", raising=False)
    cfg = GatewayConfig(prompt={"patch_evidence_protocol": True})

    assert _resolve_patch_evidence_protocol(cfg) is True


def test_patch_evidence_protocol_env_on_overrides_config_off(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", "on")

    assert _resolve_patch_evidence_protocol(GatewayConfig()) is True


def test_patch_evidence_protocol_env_off_overrides_config_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", "off")
    cfg = GatewayConfig(prompt={"patch_evidence_protocol": True})

    assert _resolve_patch_evidence_protocol(cfg) is False


def test_patch_evidence_protocol_env_blank_falls_through_to_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", "  ")
    cfg = GatewayConfig(prompt={"patch_evidence_protocol": True})

    assert _resolve_patch_evidence_protocol(cfg) is True


def test_patch_evidence_protocol_env_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL"):
        _resolve_patch_evidence_protocol(GatewayConfig())


def test_finalize_evidence_gate_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", raising=False)

    assert _resolve_finalize_evidence_gate(GatewayConfig()) is False


def test_finalize_evidence_gate_config_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", raising=False)
    cfg = GatewayConfig(prompt={"finalize_evidence_gate": True})

    assert _resolve_finalize_evidence_gate(cfg) is True


def test_finalize_evidence_gate_env_on_overrides_config_off(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "on")

    assert _resolve_finalize_evidence_gate(GatewayConfig()) is True


def test_finalize_evidence_gate_env_off_overrides_config_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "off")
    cfg = GatewayConfig(prompt={"finalize_evidence_gate": True})

    assert _resolve_finalize_evidence_gate(cfg) is False


def test_finalize_evidence_gate_env_blank_falls_through_to_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "  ")
    cfg = GatewayConfig(prompt={"finalize_evidence_gate": True})

    assert _resolve_finalize_evidence_gate(cfg) is True


def test_finalize_evidence_gate_env_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_FINALIZE_EVIDENCE_GATE"):
        _resolve_finalize_evidence_gate(GatewayConfig())


def test_bootstrap_finalize_evidence_gate_env_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", raising=False)

    assert _finalize_evidence_gate_from_env() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_bootstrap_finalize_evidence_gate_env_on(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", value)

    assert _finalize_evidence_gate_from_env() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "NO", "  "])
def test_bootstrap_finalize_evidence_gate_env_off_or_blank(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", value)

    assert _finalize_evidence_gate_from_env() is False


def test_bootstrap_finalize_evidence_gate_env_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_FINALIZE_EVIDENCE_GATE"):
        _finalize_evidence_gate_from_env()


def test_bootstrap_finalize_evidence_gate_uses_config_value_when_env_absent(
    monkeypatch,
) -> None:
    # The gateway ``prompt.finalize_evidence_gate`` value must reach the
    # loop-side gate through the same resolver the env override uses,
    # matching the runtime prompt-section resolution above.
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", raising=False)

    assert _finalize_evidence_gate_from_env(True) is True
    assert _finalize_evidence_gate_from_env(False) is False


def test_bootstrap_finalize_evidence_gate_env_blank_falls_through_to_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "  ")

    assert _finalize_evidence_gate_from_env(True) is True


def test_bootstrap_finalize_evidence_gate_env_off_overrides_config_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "off")

    assert _finalize_evidence_gate_from_env(True) is False


def test_bootstrap_finalize_evidence_strict_env_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", raising=False)

    assert _finalize_evidence_strict_from_env() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_bootstrap_finalize_evidence_strict_env_on(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", value)

    assert _finalize_evidence_strict_from_env() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "NO", "  "])
def test_bootstrap_finalize_evidence_strict_env_off_or_blank(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", value)

    assert _finalize_evidence_strict_from_env() is False


def test_bootstrap_finalize_evidence_strict_env_rejects_unrecognized_value(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_FINALIZE_EVIDENCE_STRICT"):
        _finalize_evidence_strict_from_env()


def test_bootstrap_finalize_evidence_strict_uses_config_value_when_env_absent(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", raising=False)

    assert _finalize_evidence_strict_from_env(True) is True
    assert _finalize_evidence_strict_from_env(False) is False


def test_bootstrap_finalize_evidence_strict_env_blank_falls_through_to_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", "  ")

    assert _finalize_evidence_strict_from_env(True) is True


def test_bootstrap_finalize_evidence_strict_env_off_overrides_config_on(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_STRICT", "off")

    assert _finalize_evidence_strict_from_env(True) is False


def test_legacy_prompt_style_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", raising=False)

    assert _resolve_legacy_prompt_style(GatewayConfig()) is False


def test_legacy_prompt_style_config_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", raising=False)
    cfg = GatewayConfig(prompt={"legacy_prompt_style": True})

    assert _resolve_legacy_prompt_style(cfg) is True


def test_legacy_prompt_style_env_on_overrides_config_off(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", "on")

    assert _resolve_legacy_prompt_style(GatewayConfig()) is True


def test_legacy_prompt_style_env_off_overrides_config_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", "off")
    cfg = GatewayConfig(prompt={"legacy_prompt_style": True})

    assert _resolve_legacy_prompt_style(cfg) is False


def test_legacy_prompt_style_env_blank_falls_through_to_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", "  ")
    cfg = GatewayConfig(prompt={"legacy_prompt_style": True})

    assert _resolve_legacy_prompt_style(cfg) is True


def test_legacy_prompt_style_env_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_LEGACY_PROMPT_STYLE"):
        _resolve_legacy_prompt_style(GatewayConfig())


# ---------------------------------------------------------------------------
# _resolve_submit_review — tool-surfacing decision (runtime side)
#
# Regression guard: the ``submit`` tool surfaces from the TurnRunner config
# (``self._config``), which is a GatewayConfig that carries no
# ``submit_review_enabled`` field. The loop-side flag lives on the separate
# AgentConfig built by agent_bootstrap_stage, so reading the config field alone
# left the tool unsurfaced even with OPENSQUILLA_SUBMIT_REVIEW=on. The resolver
# must read the env var directly so surfacing tracks the loop-side gate.
# ---------------------------------------------------------------------------


def test_submit_review_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)

    assert _resolve_submit_review(GatewayConfig()) is False


def test_submit_review_env_on_surfaces_despite_missing_config_field(monkeypatch) -> None:
    # GatewayConfig has no submit_review_enabled attribute; env must still win.
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "on")

    assert not hasattr(GatewayConfig(), "submit_review_enabled")
    assert _resolve_submit_review(GatewayConfig()) is True


def test_submit_review_config_opt_in_when_env_blank(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)

    assert _resolve_submit_review(SimpleNamespace(submit_review_enabled=True)) is True


def test_submit_review_env_off_overrides_config_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "off")

    assert _resolve_submit_review(SimpleNamespace(submit_review_enabled=True)) is False


def test_submit_review_env_blank_falls_through_to_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "  ")

    assert _resolve_submit_review(SimpleNamespace(submit_review_enabled=True)) is True


def test_submit_review_env_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "enabled")

    with pytest.raises(ValueError, match="OPENSQUILLA_SUBMIT_REVIEW"):
        _resolve_submit_review(GatewayConfig())
