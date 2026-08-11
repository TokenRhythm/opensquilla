"""Contract tests for the comment-preserving TOML patcher.

``patch_import_config`` runs on every Gateway boot (``lossless_patch_sandbox_fields``
stamps ``sandbox.run_mode``), so a config it refuses to scan is a config the
Gateway refuses to start on. The scanner is line-oriented, which makes values
that span several physical lines — arrays of inline tables, nested arrays,
triple-quoted strings — the interesting cases.
"""

from __future__ import annotations

import tomllib

import pytest

from opensquilla.lossless_toml import LosslessTomlPatchError, patch_import_config


def _patched(raw: bytes, transform) -> str:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = tomllib.loads(raw.decode("utf-8"))
    transform(transformed)
    return patch_import_config(raw, original, transformed).decode("utf-8")


def _rejects(raw: bytes, transform) -> str:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = tomllib.loads(raw.decode("utf-8"))
    transform(transformed)
    with pytest.raises(LosslessTomlPatchError) as excinfo:
        patch_import_config(raw, original, transformed)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Single-line values: the patcher's core contract
# ---------------------------------------------------------------------------


def test_unchanged_payload_returns_the_source_bytes_verbatim() -> None:
    raw = b'port = 1\n# keep me\nname = "x"\n'
    original = tomllib.loads(raw.decode("utf-8"))
    assert patch_import_config(raw, original, dict(original)) is raw


def test_scalar_replacement_keeps_layout_and_trailing_comment() -> None:
    raw = b'# header\nport   =   1   # why\n\n[gateway]\nhost = "127.0.0.1"\n'
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == '# header\nport   =   2   # why\n\n[gateway]\nhost = "127.0.0.1"\n'


def test_insertion_lands_in_the_owning_table() -> None:
    raw = b'[gateway]\nhost = "127.0.0.1"\n'
    patched = _patched(raw, lambda payload: payload["gateway"].update(port=8080))
    assert patched == '[gateway]\nhost = "127.0.0.1"\nport = 8080\n'


def test_removal_preserves_a_trailing_comment_on_the_removed_line() -> None:
    raw = b'port = 1  # keep the note\nname = "x"\n'

    def drop_port(payload: dict) -> None:
        del payload["port"]

    assert _patched(raw, drop_port) == '# keep the note\nname = "x"\n'


def test_array_of_tables_headers_still_track_context() -> None:
    raw = b'[[server]]\nhost = "a"\n\n[[server]]\nhost = "b"\n'
    patched = _patched(raw, lambda payload: payload["server"][1].update(host="c"))
    assert patched == '[[server]]\nhost = "a"\n\n[[server]]\nhost = "c"\n'


# ---------------------------------------------------------------------------
# Values that span physical lines (issue #1106 and neighbours)
# ---------------------------------------------------------------------------


AGENTS_CONFIG = b"""# profile config
port = 18792

agents = [
    { id = "qa-agent", name = "QA Agent", enabled = true },
]

[gateway]
host = "127.0.0.1"
"""


def test_untouched_array_of_inline_tables_does_not_block_the_patch() -> None:
    """Regression for #1106.

    The Control UI writes ``agents`` as a multi-line array of inline tables. The
    boot migration only stamps ``sandbox.run_mode``, but the scan aborted on the
    array's rows before reaching that edit, so the Gateway never started again.
    """
    patched = _patched(AGENTS_CONFIG, lambda payload: payload.update(port=18793))
    assert patched == AGENTS_CONFIG.decode("utf-8").replace("18792", "18793")


def test_boot_migration_stamps_run_mode_next_to_an_agents_array() -> None:
    from opensquilla.sandbox.upgrade_migration import lossless_patch_sandbox_fields

    patched, mode = lossless_patch_sandbox_fields(AGENTS_CONFIG)
    payload = tomllib.loads(patched.decode("utf-8"))
    assert payload["sandbox"]["run_mode"] == mode
    assert payload["agents"] == [{"id": "qa-agent", "name": "QA Agent", "enabled": True}]
    assert b'{ id = "qa-agent", name = "QA Agent", enabled = true },' in patched


def test_nested_multi_line_array_rows_are_not_read_as_table_headers() -> None:
    raw = b"port = 1\nmatrix = [\n  [1, 2],\n  [3, 4],\n]\n"
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == "port = 2\nmatrix = [\n  [1, 2],\n  [3, 4],\n]\n"


def test_multi_line_array_may_close_with_a_trailing_comment() -> None:
    raw = b'agents = [\n  { id = "a" },\n]  # the roster\nport = 1\n'
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == 'agents = [\n  { id = "a" },\n]  # the roster\nport = 2\n'


def test_tables_after_a_multi_line_array_keep_their_own_context() -> None:
    patched = _patched(
        AGENTS_CONFIG,
        lambda payload: payload["gateway"].update(host="0.0.0.0"),
    )
    assert patched.endswith('[gateway]\nhost = "0.0.0.0"\n')
    assert '{ id = "qa-agent"' in patched


def test_insertion_after_a_trailing_multi_line_array_lands_below_it() -> None:
    raw = b'agents = [\n  { id = "a" },\n]\n'
    patched = _patched(raw, lambda payload: payload.update(port=1))
    assert patched == 'agents = [\n  { id = "a" },\n]\nport = 1\n'


def test_crlf_config_with_a_multi_line_array_keeps_its_line_endings() -> None:
    raw = AGENTS_CONFIG.replace(b"\n", b"\r\n")
    patched = _patched(raw, lambda payload: payload.update(port=18793))
    assert patched == raw.decode("utf-8").replace("18792", "18793")
    assert "\r\n" in patched


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("terminal_quotes", [3, 4, 5])
def test_terminal_quote_runs_inside_an_array_do_not_hide_the_next_table(
    quote: str,
    terminal_quotes: int,
) -> None:
    delimiter = quote * 3
    raw = (
        "[cors]\n"
        f"allowed_origins = [{delimiter}https://example.com{quote * terminal_quotes} ]\n"
        "\n"
        "[sandbox]\n"
        "sandbox = true\n"
    ).encode()

    from opensquilla.sandbox.upgrade_migration import lossless_patch_sandbox_fields

    patched, mode = lossless_patch_sandbox_fields(raw)
    payload = tomllib.loads(patched.decode("utf-8"))
    assert mode == "safe"
    assert payload["sandbox"]["run_mode"] == "safe"
    assert payload["cors"]["allowed_origins"] == [
        "https://example.com" + quote * (terminal_quotes - 3)
    ]


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("opening_quotes", [3, 4, 5])
def test_opening_quote_runs_inside_an_array_keep_the_collection_close_visible(
    quote: str,
    opening_quotes: int,
) -> None:
    delimiter = quote * 3
    raw = (
        "[cors]\n"
        f"allowed_origins = [{quote * opening_quotes}example{delimiter} ]\n"
        "\n"
        "[sandbox]\n"
        "sandbox = true\n"
    ).encode()

    from opensquilla.sandbox.upgrade_migration import lossless_patch_sandbox_fields

    patched, mode = lossless_patch_sandbox_fields(raw)
    payload = tomllib.loads(patched.decode("utf-8"))
    assert mode == "safe"
    assert payload["sandbox"]["run_mode"] == "safe"
    assert payload["cors"]["allowed_origins"] == [
        quote * (opening_quotes - 3) + "example"
    ]


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_string_separators_are_not_toml_physical_lines(separator: str) -> None:
    raw = f'name = "before{separator}after"\n'.encode()
    patched = _patched(
        raw,
        lambda payload: payload.setdefault("sandbox", {}).update(run_mode="full"),
    )
    assert f'before{separator}after' in patched
    assert tomllib.loads(patched)["sandbox"]["run_mode"] == "full"


def test_insertion_after_an_eof_assignment_adds_a_physical_newline() -> None:
    raw = b"port = 1"
    patched = _patched(
        raw,
        lambda payload: payload.setdefault("sandbox", {}).update(run_mode="full"),
    )
    assert patched == 'port = 1\nsandbox.run_mode = "full"'


def test_boot_stamp_after_an_eof_table_assignment_adds_a_physical_newline() -> None:
    from opensquilla.sandbox.upgrade_migration import lossless_patch_sandbox_fields

    raw = b"[sandbox]\nsandbox = true"
    patched, mode = lossless_patch_sandbox_fields(raw)
    assert mode == "safe"
    assert patched == b'[sandbox]\nsandbox = true\nrun_mode = "safe"'


def test_multiple_eof_insertions_have_separators_without_adding_a_final_newline() -> None:
    raw = b"port = 1"
    patched = _patched(raw, lambda payload: payload.update(alpha=1, beta=2))
    assert patched == "port = 1\nalpha = 1\nbeta = 2"


def test_crlf_eof_insertion_uses_crlf_without_adding_a_final_newline() -> None:
    from opensquilla.sandbox.upgrade_migration import lossless_patch_sandbox_fields

    raw = b"port = 1\r\n[sandbox]\r\nsandbox = true"
    patched, mode = lossless_patch_sandbox_fields(raw)
    assert mode == "safe"
    assert patched == b'port = 1\r\n[sandbox]\r\nsandbox = true\r\nrun_mode = "safe"'


# ---------------------------------------------------------------------------
# Configs with no multi-line value must come through untouched
# ---------------------------------------------------------------------------


# The shape the Desktop profile-import E2E writes, including the Windows spelling
# of its paths: `json.dumps(str(path))` produces doubled backslashes, so the value
# scanner has to walk string escapes correctly on a line it must not consume.
DESKTOP_PROFILE_CONFIG = (
    'workspace_dir = "C:\\\\Users\\\\RUNNER~1\\\\AppData\\\\Local\\\\Temp\\\\p1\\\\workspace"\n'
    'state_dir = "C:\\\\Users\\\\RUNNER~1\\\\AppData\\\\Local\\\\Temp\\\\p1\\\\state"\n'
    'search_provider = "duckduckgo"\n'
    'search_api_key_env = ""\n'
    "\n"
    "[llm]\n"
    'provider = "ollama"\n'
    'model = "synthetic-source-model"\n'
    'base_url = "http://127.0.0.1:11434/v1"  # local only\n'
    'api_key_env = ""\n'
    "\n"
    "[squilla_router]\n"
    "enabled = false\n"
    'default_tier = "c2"\n'
    "confidence_threshold = 0.77\n"
    "\n"
    "[squilla_router.tiers.c0]\n"
    'provider = "ollama"\n'
    'model = "synthetic-source-tier-model"\n'
    "\n"
    "[control_ui]\n"
    "enabled = true\n"
    'base_path = "/control"\n'
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_boot_stamp_rewrites_one_line_and_no_other_byte(newline: str) -> None:
    raw = DESKTOP_PROFILE_CONFIG.replace("\n", newline).encode("utf-8")
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = tomllib.loads(raw.decode("utf-8"))
    transformed["squilla_router"]["default_tier"] = "c1"

    patched = patch_import_config(raw, original, transformed).decode("utf-8")

    before = raw.decode("utf-8").split(newline)
    after = patched.split(newline)
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert differing == [before.index('default_tier = "c2"')]
    assert after[differing[0]] == 'default_tier = "c1"'


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_escaped_windows_paths_survive_an_insertion(newline: str) -> None:
    raw = DESKTOP_PROFILE_CONFIG.replace("\n", newline).encode("utf-8")
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = tomllib.loads(raw.decode("utf-8"))
    transformed.setdefault("sandbox", {})["run_mode"] = "full"

    patched = patch_import_config(raw, original, transformed).decode("utf-8")

    for line in raw.decode("utf-8").split(newline):
        assert line in patched.split(newline)
    assert tomllib.loads(patched) == transformed


# ---------------------------------------------------------------------------
# Triple-quoted strings: their bodies are not TOML
# ---------------------------------------------------------------------------


def test_assignment_inside_a_multi_line_string_is_not_a_second_assignment() -> None:
    raw = b'key = "real"\nprompt = """\nkey = "phantom"\n"""\n'
    patched = _patched(raw, lambda payload: payload.update(key="changed"))
    assert patched == 'key = "changed"\nprompt = """\nkey = "phantom"\n"""\n'


def test_bracketed_line_inside_a_literal_string_is_not_a_table_header() -> None:
    raw = b"notes = '''\n[not a table]\n'''\nport = 1\n"
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == "notes = '''\n[not a table]\n'''\nport = 2\n"


def test_hash_inside_a_multi_line_string_is_not_a_comment() -> None:
    raw = b'notes = """\n# not a comment\n"""\nport = 1\n'
    assert tomllib.loads(raw.decode("utf-8"))["notes"] == "# not a comment\n"
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == 'notes = """\n# not a comment\n"""\nport = 2\n'


def test_escaped_quotes_do_not_close_a_multi_line_string_early() -> None:
    raw = b'notes = """\na \\""" b\n"""\nport = 1\n'
    assert tomllib.loads(raw.decode("utf-8"))["notes"] == 'a """ b\n'
    patched = _patched(raw, lambda payload: payload.update(port=2))
    assert patched == 'notes = """\na \\""" b\n"""\nport = 2\n'


# ---------------------------------------------------------------------------
# Edits that reach *into* a multi-line value stay refused — and say why
# ---------------------------------------------------------------------------


def test_replacing_a_leaf_inside_a_multi_line_array_is_refused_by_path() -> None:
    message = _rejects(
        AGENTS_CONFIG,
        lambda payload: payload["agents"][0].update(name="Renamed"),
    )
    assert "cannot replace ('agents', 0, 'name')" in message
    assert "multi-line TOML value at ('agents',)" in message


def test_removing_a_leaf_inside_a_multi_line_array_is_refused_by_path() -> None:
    def drop_name(payload: dict) -> None:
        del payload["agents"][0]["name"]

    message = _rejects(AGENTS_CONFIG, drop_name)
    assert "cannot remove ('agents', 0, 'name')" in message
    assert "multi-line TOML value at ('agents',)" in message


def test_adding_a_leaf_inside_a_multi_line_array_is_refused_by_path() -> None:
    message = _rejects(
        AGENTS_CONFIG,
        lambda payload: payload["agents"][0].update(role="reviewer"),
    )
    assert "cannot add ('agents', 0, 'role')" in message
    assert "multi-line TOML value at ('agents',)" in message


def test_rewriting_a_multi_line_string_is_refused_rather_than_spliced() -> None:
    raw = b'prompt = """\nhello\n"""\n'
    message = _rejects(raw, lambda payload: payload.update(prompt="goodbye"))
    assert "('prompt',)" in message


# ---------------------------------------------------------------------------
# Guards the patcher already owned
# ---------------------------------------------------------------------------


def test_source_bytes_must_match_the_validated_payload() -> None:
    with pytest.raises(LosslessTomlPatchError, match="no longer match"):
        patch_import_config(b"port = 1\n", {"port": 2}, {"port": 3})


def test_invalid_source_toml_is_rejected() -> None:
    with pytest.raises(LosslessTomlPatchError, match="not valid UTF-8 TOML"):
        patch_import_config(b"port = \n", {}, {"port": 1})
