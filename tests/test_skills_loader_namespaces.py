"""Loader namespace fallback + ClawHub field-alias contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.skills.hub.lockfile import LockEntry, Lockfile
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"


def _write_skill(dir_path: Path, name: str, body: str) -> None:
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _rewrite_v2_with_v1_field_filter(path: Path) -> None:
    v1_fields = {
        "source",
        "identifier",
        "version",
        "installed_at",
        "path",
        "sha256",
        "license",
        "upstream_url",
        "source_trust",
        "scan_verdict",
        "scan_strategy",
        "scan_findings",
    }
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(
            {
                "version": payload["version"],
                "installed": {
                    storage_key: {
                        key: value for key, value in entry.items() if key in v1_fields
                    }
                    for storage_key, entry in payload["installed"].items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_clawdbot_namespace_resolves(tmp_path: Path) -> None:
    """`metadata.clawdbot.requires.bins` must populate SkillSpec.metadata.requires.bins."""
    _write_skill(
        tmp_path,
        "clawdbot-skill",
        """---
name: clawdbot-skill
description: Synthetic skill exercising the clawdbot namespace fallback.
metadata:
  clawdbot:
    requires:
      bins: [foo]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path)
    spec = loader.get_by_name("clawdbot-skill")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.bins == ["foo"]


def test_commands_alias_for_bins(tmp_path: Path) -> None:
    """`requires.commands` should map onto `requires.bins` when bins is absent."""
    _write_skill(
        tmp_path,
        "commands-alias",
        """---
name: commands-alias
description: Synthetic skill exercising the requires.commands alias.
metadata:
  platform:
    requires:
      commands: [bar]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path)
    spec = loader.get_by_name("commands-alias")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.bins == ["bar"]


def test_explicit_bins_wins_over_commands(tmp_path: Path) -> None:
    """When both `bins` and `commands` are present, `bins` is authoritative."""
    _write_skill(
        tmp_path,
        "bins-wins",
        """---
name: bins-wins
description: bins wins over commands when both present.
metadata:
  platform:
    requires:
      bins: [keep]
      commands: [drop]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path)
    spec = loader.get_by_name("bins-wins")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.bins == ["keep"]


def test_opensquilla_capabilities_and_risk_resolve(tmp_path: Path) -> None:
    """Auto-enable risk evaluation reads manifest capabilities from metadata."""
    _write_skill(
        tmp_path,
        "capability-risk",
        """---
name: capability-risk
description: Synthetic skill declaring auto-enable risk metadata.
metadata:
  opensquilla:
    capabilities: [filesystem-write, network]
    risk: medium
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snap.json")
    spec = loader.get_by_name("capability-risk")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.capabilities == ["filesystem-write", "network"]
    assert spec.metadata.risk_level == "medium"


def test_opensquilla_risk_metadata_preserves_platform_requires(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "capability-risk-with-requires",
        """---
name: capability-risk-with-requires
description: Synthetic skill declaring platform deps and risk metadata.
metadata:
  requires:
    anyBins: [python]
  opensquilla:
    risk: low
    capabilities: []
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snap.json")
    spec = loader.get_by_name("capability-risk-with-requires")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.any_bins == ["python"]
    assert spec.metadata.risk_level == "low"
    assert spec.metadata.capabilities == []


def test_env_any_requires_parse(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "env-any-parse",
        """---
name: env-any-parse
description: Synthetic skill exercising requires.envAny parsing.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path)
    spec = loader.get_by_name("env-any-parse")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.env_any == ["OPENROUTER_API_KEY", "ARK_API_KEY"]


def test_env_any_snapshot_roundtrip(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "env-any-snapshot",
        """---
name: env-any-snapshot
description: Synthetic skill exercising requires.envAny snapshot restore.
metadata:
  opensquilla:
    requires:
      envAny: [BRAVE_SEARCH_API_KEY, BRAVE_API_KEY]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    loader.load_all()
    loader.save_snapshot()
    restored = loader.load_snapshot()

    assert restored is not None
    spec = next(item for item in restored if item.name == "env-any-snapshot")
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.env_any == [
        "BRAVE_SEARCH_API_KEY",
        "BRAVE_API_KEY",
    ]


def test_existing_bundled_skills_still_parse() -> None:
    """Regression guard: every bundled SKILL.md must still parse after the patch."""
    loader = SkillLoader(bundled_dir=BUNDLED)
    skills = loader.load_all()
    parsed_names = {spec.name for spec in skills}

    on_disk = {
        path.name
        for path in BUNDLED.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert on_disk.issubset(parsed_names), (
        f"loader dropped bundled skill(s): {on_disk - parsed_names}"
    )


def test_loader_keeps_degraded_v2_entries_in_instruction_only_projection(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "skills"
    storage_key = "package-storage"
    runtime_name = "runtime-projection"
    _write_skill(
        managed,
        storage_key,
        "---\n"
        f"name: {runtime_name}\n"
        "description: Synthetic rollback loader fixture\n"
        "always: true\n"
        "entrypoint:\n"
        "  command: unsafe-upstream-command\n"
        "composition:\n"
        "  steps: []\n"
        "---\n"
        "Portable instruction body.\n",
    )
    skill_dir = managed / storage_key
    lock_path = tmp_path / "skills-lock.json"
    Lockfile(
        installed={
            storage_key: LockEntry(
                source="github",
                identifier="owner/repo:skills/runtime-projection",
                path=str(skill_dir),
                install_id="install-demo",
                manifest_name=runtime_name,
                relative_path=storage_key,
                requested_identifier="owner/repo:skills/runtime-projection@main",
                resolved_identifier=(
                    "owner/repo@" + "a" * 40 + ":skills/runtime-projection/SKILL.md"
                ),
                resolved_revision="a" * 40,
                source_package_id="github:owner/repo:skills/runtime-projection",
                parser_version="community-instruction-v1",
                dialect="instruction-first",
            )
        }
    ).save(lock_path)
    _rewrite_v2_with_v1_field_filter(lock_path)

    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        workspace_dir=tmp_path / "workspace",
        managed_dir=managed,
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "snapshot.json",
        lockfile_path=lock_path,
    )

    loaded = loader.load_all()

    spec = next(item for item in loaded if item.name == runtime_name)
    assert spec.layer is SkillLayer.MANAGED
    assert spec.content == "Portable instruction body."
    assert spec.always is False
    assert spec.entrypoint is None
    assert spec.composition_raw is None
