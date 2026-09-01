"""`config set` names one key surface, whichever way it is asked to persist.

Without `--config` the command used to accept any dotted string and answer with
an `export OPENSQUILLA_GATEWAY_...` line, so `definitely.invalid` and
`gateway.port` both came back looking configured while the gateway read
neither. With `--config` the same two spellings were refused. Issue #1383.

The other half of the same defect ran the other way: validity was read off the
values in the TOML document, and TOML has no null, so every field resting at its
`None` default was reported as "Key not found" and could not be set at all.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.gateway.config import GatewayConfig

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env form reads the operator's config; keep the runner off the host's."""

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)


def _empty_config(tmp_path: Path) -> Path:
    target = tmp_path / "opensquilla.toml"
    target.write_text("", encoding="utf-8")
    return target


# The two spellings from the report, plus a leaf typo under a real table.
REJECTED = ["definitely.invalid", "gateway.port", "auth.bogus", "memory.dream.bogus"]

# Fields at their `None` default: absent from the TOML document, real settings.
# The flag says whether `config get` redacts the value on the way back out —
# `auth.token` and `llm.api_key` are secrets, and that redaction is the point.
NULL_DEFAULTED = [("auth.token", True), ("compaction.model", False), ("llm.api_key", True)]
NULL_DEFAULTED_KEYS = [key for key, _ in NULL_DEFAULTED]


@pytest.mark.parametrize("key", REJECTED)
def test_a_key_that_does_not_exist_is_refused_without_config_too(key: str) -> None:
    result = runner.invoke(app, ["config", "set", key, "123"])

    assert result.exit_code == 1, result.stdout
    assert "Key not found" in result.stdout
    # The export line is the part that misleads: it reads as confirmation that
    # the setting was understood.
    assert "export" not in result.stdout


@pytest.mark.parametrize("key", REJECTED)
def test_the_two_forms_refuse_the_same_keys(key: str, tmp_path: Path) -> None:
    target = _empty_config(tmp_path)

    env_form = runner.invoke(app, ["config", "set", key, "123"])
    config_form = runner.invoke(app, ["config", "set", key, "123", "--config", str(target)])

    assert env_form.exit_code == config_form.exit_code == 1
    assert "Key not found" in env_form.stdout
    assert "Key not found" in config_form.stdout


@pytest.mark.parametrize(
    ("key", "env_var"),
    [
        ("port", "OPENSQUILLA_GATEWAY_PORT"),
        ("log_level", "OPENSQUILLA_GATEWAY_LOG_LEVEL"),
        ("auth.mode", "OPENSQUILLA_GATEWAY_AUTH__MODE"),
        ("memory.dream.preview_mode", "OPENSQUILLA_GATEWAY_MEMORY__DREAM__PREVIEW_MODE"),
    ],
)
def test_a_real_key_still_prints_its_export(key: str, env_var: str) -> None:
    result = runner.invoke(app, ["config", "set", key, "18823"])

    assert result.exit_code == 0, result.stdout
    assert f"export {env_var}=18823" in result.stdout


def test_invalid_port_is_not_persisted(tmp_path: Path) -> None:
    target = _empty_config(tmp_path)

    result = runner.invoke(
        app,
        ["config", "set", "port", "65536", "--config", str(target)],
    )

    assert result.exit_code == 2
    assert "Invalid value for port" in result.stdout
    assert tomllib.loads(target.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize(
    ("key", "env_var", "value", "expected"),
    [
        ("port", "OPENSQUILLA_GATEWAY_PORT", "18823", 18823),
        ("log_level", "OPENSQUILLA_GATEWAY_LOG_LEVEL", "INFO", "INFO"),
    ],
)
def test_the_printed_variable_is_the_one_the_gateway_reads(
    key: str,
    env_var: str,
    value: str,
    expected: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export is an instruction; following it has to change the config.

    This is what `gateway.port` failed: it produced
    OPENSQUILLA_GATEWAY_GATEWAY__PORT, a variable nothing reads, while the one
    that does — OPENSQUILLA_GATEWAY_PORT — stayed unset.
    """

    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == 0, result.stdout
    assert f"export {env_var}={value}" in result.stdout

    monkeypatch.setenv(env_var, value)
    reloaded = GatewayConfig.load(None)

    cursor: object = reloaded
    for part in key.split("."):
        cursor = getattr(cursor, part)
    assert cursor == expected


@pytest.mark.parametrize(("key", "redacted"), NULL_DEFAULTED)
def test_a_field_left_at_none_is_settable(key: str, redacted: bool, tmp_path: Path) -> None:
    """TOML cannot hold a null, which is not the same as the key not existing."""

    target = _empty_config(tmp_path)

    result = runner.invoke(app, ["config", "set", key, '"set-by-test"', "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    table, leaf = key.split(".")
    assert document[table][leaf] == "set-by-test"

    read_back = runner.invoke(app, ["config", "get", key, "--config", str(target)])
    assert read_back.exit_code == 0, read_back.stdout
    expected = "[redacted]" if redacted else "set-by-test"
    assert expected in read_back.stdout


@pytest.mark.parametrize("key", NULL_DEFAULTED_KEYS)
def test_a_field_left_at_none_is_accepted_by_the_env_form_too(key: str) -> None:
    result = runner.invoke(app, ["config", "set", key, '"set-by-test"'])

    assert result.exit_code == 0, result.stdout
    assert "export OPENSQUILLA_GATEWAY_" in result.stdout


def test_an_operator_named_mapping_entry_still_resolves(tmp_path: Path) -> None:
    """Router tier ids are data, not schema — the document is the only evidence.

    So the entry has to be there already: this must not invent a half-formed
    tier out of a typo, and must not refuse one the operator really has.
    """

    target = _empty_config(tmp_path)
    known = runner.invoke(
        app,
        [
            "config",
            "set",
            "squilla_router.tiers.c0.model",
            '"vendor/model"',
            "--config",
            str(target),
        ],
    )
    assert known.exit_code == 0, known.stdout

    typos = (
        "squilla_router.tiers.no_such_tier.model",
        "squilla_router.tiers.c0.no_such_field",
    )
    for typo in typos:
        result = runner.invoke(app, ["config", "set", typo, '"x"', "--config", str(target)])
        assert result.exit_code == 1, result.stdout
        assert "Key not found" in result.stdout


@pytest.mark.parametrize("key", ["", ".", "auth.", "auth..mode", "port.extra"])
def test_a_malformed_key_is_refused(key: str) -> None:
    result = runner.invoke(app, ["config", "set", key, "1"])

    assert result.exit_code == 1, result.stdout
    assert "export" not in result.stdout


def test_no_key_the_previous_check_accepted_is_now_refused(tmp_path: Path) -> None:
    """The new check is a superset of the old one, over the whole document.

    Validity moved from "this key is present in the TOML" to "this key is a
    field of `GatewayConfig`". Widening a check can narrow it somewhere else,
    so every leaf the old rule accepted is replayed against the new one.
    """

    from opensquilla.cli.config_cmd import _set_key

    document = GatewayConfig.load(None).to_toml_dict()

    def leaves(node: dict, prefix: str = "") -> list[str]:
        found: list[str] = []
        for name, value in node.items():
            full = f"{prefix}.{name}" if prefix else name
            found.extend(leaves(value, full) if isinstance(value, dict) else [full])
        return found

    keys = leaves(document)
    assert len(keys) > 300, "the default document should be a broad sample"

    import copy

    refused = [key for key in keys if not _set_key(copy.deepcopy(document), key, "X")]
    assert refused == []
