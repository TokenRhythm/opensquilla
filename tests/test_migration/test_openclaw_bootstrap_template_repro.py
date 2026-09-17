"""Repro: opensquilla bootstrap-template MEMORY.md blocks openclaw migration.

When opensquilla initializes a workspace (via ``ensure_agent_workspace``) it
seeds bootstrap templates for ``SOUL.md`` / ``USER.md`` / ``AGENTS.md`` /
``MEMORY.md``. These are placeholder docs (5-line comments). Before the fix
the openclaw migrator's ``_write_text_target`` hit the conflict gate on
every one of them, silently dropping every workspace file the user actually
wanted migrated — including the real reason the user invoked the migration,
their daily memory.

The fix detects "destination still holds the pristine bootstrap template"
and treats it as overwrite-safe (item-level backup + replace), with an
explicit ``details.replaced_bootstrap_template`` flag in the report.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.identity.bootstrap import ensure_agent_workspace
from opensquilla.migration.openclaw import MigrationOptions, OpenClawMigrator


def _make_openclaw_source(root: Path) -> Path:
    source = root / ".openclaw"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("openclaw real soul\n", encoding="utf-8")
    (workspace / "USER.md").write_text("openclaw real user\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("openclaw agents guide\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("openclaw long-term memory\n", encoding="utf-8")
    (workspace / "memory").mkdir()
    (workspace / "memory" / "2026-05-04.md").write_text(
        "real daily entry that needs to survive migration\n",
        encoding="utf-8",
    )
    (source / "openclaw.json").write_text("{}", encoding="utf-8")
    return source


@pytest.mark.parametrize("old_defaults", [False, True])
def test_pristine_bootstrap_templates_do_not_block_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_defaults: bool,
) -> None:
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    ensure_agent_workspace(home / "workspace")
    if old_defaults:
        fixtures = Path(__file__).parents[1] / "fixtures" / "workspace_md_retirement"
        for name in ("AGENTS", "SOUL"):
            (home / "workspace" / f"{name}.md").write_bytes((fixtures / f"{name}.txt").read_bytes())
    # Confirm the templates were seeded (precondition for the bug).
    for filename in ("SOUL.md", "USER.md", "AGENTS.md", "MEMORY.md"):
        assert (home / "workspace" / filename).is_file()

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    statuses = {item["kind"]: item["status"] for item in report["items"]}
    for kind in ("soul", "user-profile", "workspace-agents", "memory"):
        assert statuses.get(kind) == "migrated", (
            f"kind={kind} expected migrated, got {statuses.get(kind)!r}. "
            f"items: {[i for i in report['items'] if i['kind'] == kind]}"
        )

    # Bootstrap-template replacement is announced via the details flag so the
    # report makes the special case visible rather than silent.
    for kind in ("soul", "user-profile", "workspace-agents", "memory"):
        item = next(i for i in report["items"] if i["kind"] == kind)
        assert item["details"].get("replaced_bootstrap_template") is True, (
            f"kind={kind} did not record replaced_bootstrap_template: {item}"
        )

    # The migrated content actually landed and the daily memory is in MEMORY.md.
    memory_text = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert "real daily entry that needs to survive migration" in memory_text

    soul_text = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    # rebrand: openclaw -> opensquilla in workspace prose.
    assert "opensquilla real soul" in soul_text
    assert "openclaw" not in soul_text.lower()

    # Item-level backups of the pristine templates exist for rollback.
    backups = sorted((home / "workspace").glob("*.backup.*"))
    backup_basenames = {b.name.split(".backup.")[0] for b in backups}
    assert {"SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md"} <= backup_basenames


def test_user_edited_memory_is_preserved_and_openclaw_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # User who has truly edited their MEMORY.md should NOT have their
    # content silently overwritten AND the openclaw memory should NOT be
    # silently dropped. The migrator merges instead: user content stays
    # at the top, openclaw blocks that aren't already there are appended.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    ensure_agent_workspace(home / "workspace")
    (home / "workspace" / "MEMORY.md").write_text(
        "# My real, edited memory\n\nThis is not the template.\n",
        encoding="utf-8",
    )

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    memory_item = next(i for i in report["items"] if i["kind"] == "memory")
    assert memory_item["status"] == "migrated"
    assert memory_item["details"]["appended_to_existing"] is True
    assert memory_item["details"]["new_blocks_appended"] >= 1
    # User content verbatim at the top.
    text = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert text.startswith("# My real, edited memory")
    assert "This is not the template." in text
    # openclaw content appended below.
    assert "real daily entry that needs to survive migration" in text
    # The pre-existing edited file was backed up before being modified.
    backups = list((home / "workspace").glob("MEMORY.md.backup.*"))
    assert len(backups) == 1
    assert (
        "This is not the template."
        in backups[0].read_text(encoding="utf-8")
    )


def test_re_migration_against_existing_destination_dedupes_to_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Second pass over an already-migrated destination should record
    # `skipped: all openclaw memory blocks already present in destination`
    # and leave the file untouched. No duplicate growth, no conflict.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    ensure_agent_workspace(home / "workspace")

    OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()
    after_first = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")

    second = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()
    memory_item = next(i for i in second["items"] if i["kind"] == "memory")
    assert memory_item["status"] == "skipped"
    assert "already present" in memory_item["reason"]
    assert memory_item["details"]["deduplicated_against_existing"] is True
    after_second = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert after_first == after_second  # byte-identical, no second backup either


def test_partial_overlap_appends_only_new_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Destination already contains one of the openclaw daily blocks (e.g. a
    # prior migration's output). Only the genuinely new blocks should be
    # appended; duplicates should be counted in details.
    source = tmp_path / ".openclaw"
    ws = source / "workspace"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "memory" / "day-one.md").write_text("shared fact\n", encoding="utf-8")
    (ws / "memory" / "day-two.md").write_text("brand new fact\n", encoding="utf-8")
    (source / "openclaw.json").write_text("{}", encoding="utf-8")

    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    # Pre-existing destination already has the day-one block.
    (home / "workspace" / "MEMORY.md").write_text(
        "Unique opensquilla note.\n\n"
        "## Imported daily memory: day-one.md\n\n"
        "shared fact\n",
        encoding="utf-8",
    )

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    memory_item = next(i for i in report["items"] if i["kind"] == "memory")
    assert memory_item["status"] == "migrated"
    assert memory_item["details"]["new_blocks_appended"] == 1
    assert memory_item["details"]["deduplicated_blocks_vs_existing"] >= 1

    text = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert "Unique opensquilla note" in text
    assert text.count("shared fact") == 1
    assert "brand new fact" in text


def test_persona_conflict_default_in_non_tty_keeps_opensquilla_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In a non-TTY context (pytest, CI, --json) the default `prompt` mode
    # cannot actually prompt, so it falls back to the safe choice: keep
    # the user's opensquilla content and archive the openclaw original
    # under archive/files/openclaw-orphaned/ so nothing is silently lost.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "SOUL.md").write_text(
        "# My real opensquilla persona\nI am precise and terse.\n",
        encoding="utf-8",
    )

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    soul = next(i for i in report["items"] if i["kind"] == "soul")
    assert soul["status"] == "skipped"
    assert "kept existing opensquilla content" in soul["reason"]
    assert soul["details"]["persona_conflict_resolution"] == "use-opensquilla"
    # Existing opensquilla content untouched.
    assert (
        "I am precise and terse."
        in (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    )
    # Openclaw original archived, not silently dropped.
    orphan = Path(report["output_dir"]) / "archive" / "files" / "openclaw-orphaned" / "SOUL.md"
    assert orphan.is_file()
    assert "openclaw real soul" in orphan.read_text(encoding="utf-8")


def test_persona_conflict_use_openclaw_replaces_with_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "SOUL.md").write_text(
        "OPENSQUILLA ORIGINAL\n", encoding="utf-8"
    )

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "cfg.toml",
            apply=True,
            persona_conflict="use-openclaw",
        )
    ).migrate()

    text = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert "OPENSQUILLA ORIGINAL" not in text
    # rebrand turns "openclaw real soul" -> "opensquilla real soul"
    assert "opensquilla real soul" in text
    backups = list((home / "workspace").glob("SOUL.md.backup.*"))
    assert len(backups) == 1
    assert "OPENSQUILLA ORIGINAL" in backups[0].read_text(encoding="utf-8")


def test_persona_conflict_merge_appends_with_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "USER.md").write_text(
        "I am Alice, working on RPC layer.\n", encoding="utf-8"
    )

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "cfg.toml",
            apply=True,
            persona_conflict="merge",
        )
    ).migrate()

    user_item = next(i for i in report["items"] if i["kind"] == "user-profile")
    assert user_item["status"] == "migrated"
    assert user_item["details"]["persona_conflict_resolution"] == "merge"
    text = (home / "workspace" / "USER.md").read_text(encoding="utf-8")
    # Existing opensquilla content preserved at the top.
    assert text.startswith("I am Alice")
    # OpenClaw content appended after a clear separator.
    assert "## Imported from OpenClaw" in text
    assert "opensquilla real user" in text  # rebrand applied
    # Pre-existing file was backed up.
    backups = list((home / "workspace").glob("USER.md.backup.*"))
    assert len(backups) == 1


def test_persona_conflict_skip_drops_both_with_explicit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "AGENTS.md").write_text(
        "WORKSPACE OPERATING RULES\n", encoding="utf-8"
    )

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "cfg.toml",
            apply=True,
            persona_conflict="skip",
        )
    ).migrate()

    agents_item = next(i for i in report["items"] if i["kind"] == "workspace-agents")
    assert agents_item["status"] == "skipped"
    assert agents_item["reason"] == "user chose to skip this file"
    assert agents_item["details"]["persona_conflict_resolution"] == "skip"
    # Existing content unchanged.
    assert (
        "WORKSPACE OPERATING RULES"
        in (home / "workspace" / "AGENTS.md").read_text(encoding="utf-8")
    )
    # When user explicitly says "skip" we do NOT archive openclaw — they
    # asked to drop it.
    orphan = Path(report["output_dir"]) / "archive" / "files" / "openclaw-orphaned" / "AGENTS.md"
    assert not orphan.exists()


def test_persona_conflict_use_opensquilla_keeps_dest_and_archives_openclaw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Explicit (non-prompt) use-opensquilla. Same behavior as the non-TTY
    # default but reached via an explicit CLI flag instead of fallback.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "SOUL.md").write_text(
        "EXISTING CONTENT TO PRESERVE\n", encoding="utf-8"
    )

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "cfg.toml",
            apply=True,
            persona_conflict="use-opensquilla",
        )
    ).migrate()

    soul = next(i for i in report["items"] if i["kind"] == "soul")
    assert soul["status"] == "skipped"
    assert soul["details"]["persona_conflict_resolution"] == "use-opensquilla"
    assert (
        "EXISTING CONTENT TO PRESERVE"
        in (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    )
    orphan = Path(report["output_dir"]) / "archive" / "files" / "openclaw-orphaned" / "SOUL.md"
    assert orphan.is_file()


def test_overwrite_flag_still_replaces_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --overwrite is the explicit "replace, do not merge" escape hatch.
    # Confirm its semantics are unchanged by the new merge-by-default flow.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "MEMORY.md").write_text(
        "REAL USER MEMORY THAT GETS REPLACED\n", encoding="utf-8"
    )

    OpenClawMigrator(
        MigrationOptions(
            source=source, config_path=tmp_path / "cfg.toml", apply=True, overwrite=True
        )
    ).migrate()

    text = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert "REAL USER MEMORY THAT GETS REPLACED" not in text
    assert "real daily entry that needs to survive migration" in text
    # Backup of the previous content is still kept on --overwrite.
    backups = list((home / "workspace").glob("MEMORY.md.backup.*"))
    assert len(backups) == 1
    assert "REAL USER MEMORY THAT GETS REPLACED" in backups[0].read_text(encoding="utf-8")


def test_template_detection_is_robust_to_trailing_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stray trailing newline or platform EOL artifact should not disqualify
    # the destination from being treated as the pristine template.
    source = _make_openclaw_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    ensure_agent_workspace(home / "workspace")
    memory_path = home / "workspace" / "MEMORY.md"
    original = memory_path.read_text(encoding="utf-8")
    memory_path.write_text(original.rstrip() + "\n\n\n", encoding="utf-8")

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    memory_item = next(i for i in report["items"] if i["kind"] == "memory")
    assert memory_item["status"] == "migrated"
    assert memory_item["details"]["replaced_bootstrap_template"] is True


def test_openclaw_workspace_with_opensquilla_mention_is_kept_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same mixed-subject prose bug as hermes, on the openclaw side.
    # Source SOUL.md mentions OpenClaw AND OpenSquilla as distinct
    # entities — mechanical rebrand would corrupt every sentence. The
    # migrator must write the text verbatim and surface
    # ``details.rebrand_skipped``.
    source = _make_openclaw_source(tmp_path)
    (source / "workspace" / "SOUL.md").write_text(
        "OpenClaw v1.2 is installed at ~/.openclaw.\n"
        "OpenSquilla is also installed at ~/.opensquilla and exposes "
        "`migrate openclaw` for importing OpenClaw state.\n",
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    migrated = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    # OpenClaw mention preserved verbatim (no rebrand).
    assert "OpenClaw v1.2 is installed at ~/.openclaw" in migrated
    # The "migrate openclaw" command name is kept verbatim too.
    assert "`migrate openclaw`" in migrated
    soul_item = next(i for i in report["items"] if i["kind"] == "soul")
    assert soul_item["details"]["rebrand_skipped"] == "mentions-opensquilla"
    assert "semantic_conversions" not in soul_item["details"]


def test_openclaw_rebrand_does_not_mangle_path_substrings() -> None:
    # Plain string replace turned ``.openclawrc`` into ``.opensquillarc``,
    # ``OpenClawFlavored`` into ``OpenSquillaFlavored``, and
    # ``openclaw_pid`` into ``opensquilla_pid``. None of these were
    # intentional rebrands. Word-boundary aware regex replacement
    # leaves prefix-substring tokens alone.
    from opensquilla.migration.openclaw import _rebrand_text

    cases = [
        ("Config ~/.openclawrc keeps working", "Config ~/.openclawrc keeps working"),
        ("var openclaw_pid", "var openclaw_pid"),
        ("Has OpenClawFlavored name", "Has OpenClawFlavored name"),
        # Legitimate rebrands:
        ("Use ~/.openclaw home", "Use ~/.opensquilla home"),
        ("I am OpenClaw", "I am OpenSquilla"),
        ("Run: openclaw migrate", "Run: opensquilla migrate"),
    ]
    for source_text, expected in cases:
        out, _ = _rebrand_text(source_text)
        assert out == expected, f"{source_text!r} -> {out!r}, expected {expected!r}"


def test_openclaw_mcp_enabled_false_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same fix as hermes side: respect an explicit ``mcp.enabled=false``
    # when the user already has configured servers.
    import json as _json

    source = tmp_path / ".openclaw"
    (source / "workspace").mkdir(parents=True)
    (source / "openclaw.json").write_text(
        _json.dumps({"mcp": {"servers": {"new": {"command": "/usr/bin/x"}}}}),
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    home.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "cfg.toml"
    config_path.write_text(
        "[mcp]\nenabled = false\n\n"
        "[[mcp.servers]]\n"
        "name = \"existing\"\ncommand = \"/usr/bin/y\"\ntransport = \"stdio\"\n",
        encoding="utf-8",
    )

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    import tomllib
    data = tomllib.loads(config_path.read_text())
    assert data["mcp"]["enabled"] is False
    mc = next(i for i in report["items"] if i["kind"] == "mcp-servers")
    assert mc["details"]["mcp_enabled_left_disabled"] is True


def test_openclaw_memory_blocks_track_skipped_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two daily-memory entries: one is pure OpenClaw prose (gets rebranded),
    # the other is mixed-subject (must be kept verbatim). The memory
    # report should record that exactly one block was skipped while the
    # other was rebranded.
    #
    # Note: the openclaw rebrand protects "OpenClaw home/skills/..." as
    # source-reference markers, so we use plain "I am OpenClaw" (which
    # IS rebranded to "I am OpenSquilla") to drive the rebranded branch.
    source = tmp_path / ".openclaw"
    ws = source / "workspace"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "memory" / "single.md").write_text(
        "I am OpenClaw and I remember everything.\n",
        encoding="utf-8",
    )
    (ws / "memory" / "mixed.md").write_text(
        "OpenClaw v1.2 lives at ~/.openclaw; OpenSquilla is at ~/.opensquilla.\n",
        encoding="utf-8",
    )
    (source / "openclaw.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    text = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    # Rebranded block: "I am OpenClaw" -> "I am OpenSquilla".
    assert "I am OpenSquilla" in text
    # Mixed-subject block: kept verbatim, OpenClaw mention survives.
    assert "OpenClaw v1.2 lives at ~/.openclaw" in text
    memory_item = next(i for i in report["items"] if i["kind"] == "memory")
    assert memory_item["details"]["rebrand_skipped"] == "mentions-opensquilla"
    assert memory_item["details"]["rebrand_skipped_block_count"] == 1
    # The other block was rebranded, so semantic_conversions still appears.
    assert memory_item["details"].get("semantic_conversions") == ["openclaw-branding"]
