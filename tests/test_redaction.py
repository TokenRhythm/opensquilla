"""Tests for the dependency-free free-text redaction primitive."""

from __future__ import annotations

from opensquilla.redaction import redact_error_text


def test_empty_text_stays_empty() -> None:
    assert redact_error_text("") == ""


def test_plain_error_text_passes_through() -> None:
    text = "connection refused while contacting host"
    assert redact_error_text(text) == text


def test_bearer_token_is_masked() -> None:
    out = redact_error_text("401 unauthorized: Bearer abc123def456 rejected")
    assert "abc123def456" not in out
    assert "Bearer ***" in out
    assert "rejected" in out


def test_bearer_token_masked_case_insensitively() -> None:
    out = redact_error_text("header was BEARER XYZTOKEN123")
    assert "XYZTOKEN123" not in out


def test_sk_style_key_is_masked() -> None:
    out = redact_error_text("invalid api key sk-test-000 provided")
    assert "sk-test-000" not in out
    assert "provided" in out


def test_api_key_query_value_is_masked() -> None:
    out = redact_error_text("GET https://api.example.test/v1?api_key=tok0123456&scope=chat failed")
    assert "tok0123456" not in out
    assert "scope=chat" in out


def test_token_and_key_body_values_are_masked() -> None:
    out = redact_error_text('payload had token: tok-abc-1 and "key=val0987"')
    assert "tok-abc-1" not in out
    assert "val0987" not in out


def test_token_prose_plural_is_not_masked() -> None:
    text = "too many tokens: 4096 requested"
    assert redact_error_text(text) == text


def test_url_userinfo_is_masked() -> None:
    out = redact_error_text("proxy http://alice:hunter0@proxy.local:8080 unreachable")
    assert "alice" not in out
    assert "hunter0" not in out
    assert "***@proxy.local:8080" in out


def test_long_unbroken_base64_run_is_masked() -> None:
    secret = "QUJDREVGR0hJSktMTU5PUFFSU1RVVg=="
    out = redact_error_text(f"upstream echoed {secret} in the body")
    assert secret not in out
    assert "upstream echoed" in out


def test_long_hex_run_is_masked() -> None:
    out = redact_error_text("trace 0badc0ffee0badc0ffee0badc0ffee end")
    assert "0badc0ffee0badc0ffee0badc0ffee" not in out
    assert "end" in out


def test_short_runs_are_not_masked() -> None:
    text = "request id abc123 failed after 30s"
    assert redact_error_text(text) == text


def test_exact_known_secret_is_masked_without_relying_on_its_shape() -> None:
    secret = "AIza"
    out = redact_error_text(
        f"upstream echoed opaque credential {secret}",
        known_secrets=(secret,),
    )
    assert secret not in out
    assert out == "upstream echoed opaque credential ***"


def test_truncates_to_max_len() -> None:
    out = redact_error_text("x " * 400)
    assert len(out) <= 200
    assert out.endswith("…")


def test_custom_max_len_is_honored() -> None:
    out = redact_error_text("connection reset by peer during handshake", max_len=16)
    assert len(out) <= 16
    assert out.endswith("…")


def test_secret_beyond_truncation_window_never_survives() -> None:
    text = ("padding " * 30) + "sk-test-000-aaaaaaaaaaaaaaaaaaaaaa"
    out = redact_error_text(text, max_len=250)
    assert "sk-test-000" not in out


def test_onboarding_module_reexports_the_primitive() -> None:
    from opensquilla.onboarding import redaction as onboarding_redaction

    assert onboarding_redaction.redact_error_text is redact_error_text
