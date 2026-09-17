from __future__ import annotations

import importlib.util

import pytest

from opensquilla.sandbox.run_mode import RunMode


def test_legacy_codec_is_isolated_in_compatibility_module() -> None:
    assert importlib.util.find_spec("opensquilla.sandbox.legacy_codec") is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("standard", RunMode.SAFE),
        ("standard-sandbox", RunMode.SAFE),
        ("on", RunMode.SAFE),
        ("off", RunMode.SAFE),
        ("trusted", RunMode.SAFE),
        ("trusted-sandbox", RunMode.SAFE),
        ("trust", RunMode.SAFE),
        ("managed", RunMode.SAFE),
        ("full", RunMode.FULL),
        ("full-host-access", RunMode.FULL),
        ("bypass", RunMode.FULL),
    ],
)
def test_explicit_legacy_values_decode_one_way(value: str, expected: RunMode) -> None:
    from opensquilla.sandbox.legacy_codec import (
        LegacyModeContext,
        decode_legacy_run_mode,
    )

    assert decode_legacy_run_mode(value, context=LegacyModeContext.EXPLICIT) is expected


def test_unknown_legacy_value_fails_closed() -> None:
    from opensquilla.sandbox.legacy_codec import (
        LegacyModeContext,
        LegacyModeDecodeError,
        decode_legacy_run_mode,
    )

    with pytest.raises(LegacyModeDecodeError, match="unknown-mode"):
        decode_legacy_run_mode("unknown-mode", context=LegacyModeContext.CONFIG)


@pytest.mark.parametrize(
    ("protocol", "mode", "encoded"),
    [
        (1, RunMode.SAFE, "trusted"),
        (3, RunMode.SAFE, "trusted"),
        (4, RunMode.SAFE, "safe"),
        (1, RunMode.FULL, "full"),
        (4, RunMode.FULL, "full"),
    ],
)
def test_protocol_encoder_never_leaks_legacy_names_to_protocol_four(
    protocol: int,
    mode: RunMode,
    encoded: str,
) -> None:
    from opensquilla.sandbox.legacy_codec import encode_run_mode_for_protocol

    assert encode_run_mode_for_protocol(mode, protocol=protocol) == encoded


def test_missing_legacy_fields_preserve_previous_full_default() -> None:
    from opensquilla.sandbox.legacy_codec import decode_legacy_config_mode

    assert decode_legacy_config_mode() is RunMode.FULL


def test_explicit_legacy_sandbox_true_maps_to_safe() -> None:
    from opensquilla.sandbox.legacy_codec import decode_legacy_config_mode

    assert decode_legacy_config_mode(sandbox_enabled=True, grading_enabled=False) is RunMode.SAFE


def test_unknown_explicit_legacy_config_mode_does_not_fall_back_to_full() -> None:
    from opensquilla.sandbox.legacy_codec import LegacyModeDecodeError, decode_legacy_config_mode

    with pytest.raises(LegacyModeDecodeError, match="future-mode"):
        decode_legacy_config_mode(run_mode="future-mode")
