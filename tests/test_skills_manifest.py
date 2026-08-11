from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from opensquilla.skills.hub.lockfile import LockEntry, Lockfile
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.manifest import (
    MAX_STANDARD_SKILL_DESCRIPTION_LENGTH,
    SkillCompileProfile,
    compile_skill_manifest,
    validate_hub_candidate,
)
from opensquilla.skills.types import SkillLayer


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "Portable test skill.",
    extra: str = "",
    body: str = "Use the test workflow.",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name if name is not None else directory}\n"
        f"description: {json.dumps(description)}\n"
        f"{extra}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_shared_compiler_and_strict_candidate_accept_portable_manifest(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "portable-skill",
        extra=(
            "user-invocable: true\n"
            "disable-model-invocation: false\n"
            "vendor-extension: retained\n"
            "metadata:\n"
            "  openclaw:\n"
            "    requires:\n"
            "      bins: [python3]\n"
        ),
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="portable-skill",
    )

    assert validation.ok is True
    assert validation.diagnostics == ()
    assert validation.compatibility_diagnostics == ()
    assert validation.spec is not None
    assert validation.spec.name == "portable-skill"
    assert validation.spec.layer is SkillLayer.MANAGED
    assert validation.spec.instance_id.startswith("managed:")
    assert validation.spec.metadata is not None
    assert validation.spec.metadata.requires is not None
    assert validation.spec.metadata.requires.bins == ["python3"]

    compiled = compile_skill_manifest(skill_dir, SkillLayer.MANAGED)
    assert compiled == validation.spec


@pytest.mark.parametrize(
    "name",
    [
        "Uppercase",
        "leading-",
        "two--hyphens",
        "contains_underscore",
        "a" * 65,
    ],
)
def test_community_candidate_accepts_noncanonical_runtime_names_without_degradation(
    tmp_path: Path,
    name: str,
) -> None:
    skill_dir = _write_skill(tmp_path, name, name=name)

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.name == name
    assert validation.diagnostics == ()
    assert validation.compatibility_diagnostics == ()


def test_community_candidate_keeps_exact_runtime_name_without_special_case(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(tmp_path, "metro_home", name="metro_home")

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="metro_home",
    )

    assert validation.ok is True
    assert validation.diagnostics == ()
    assert validation.spec is not None
    assert validation.spec.name == "metro_home"


def test_community_candidate_trims_invisible_runtime_name_whitespace(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "source-slug"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: "  runtime_name  "\ndescription: Portable.\n---\nBody.\n',
        encoding="utf-8",
    )

    validation = validate_hub_candidate(skill_dir, expected_name="runtime_name")

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.name == "runtime_name"
    assert validation.compatibility_diagnostics == ()


def test_runtime_name_storage_name_and_visibility_are_independent(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "different-directory",
        name="metro_home",
        description="Metro home instructions.",
        extra='hooks: {}\nuser-invocable: "false"\n',
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="metro_home",
    )

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.name == "metro_home"
    assert validation.spec.user_invocable is False
    assert validation.spec.entrypoint is None
    assert validation.spec.composition_raw is None
    codes = {item["code"] for item in validation.compatibility_diagnostics}
    assert "DIALECT_FIELD_UNSUPPORTED" in codes


@pytest.mark.parametrize("name", ["Upper_Name", "name with spaces", "metro__home"])
def test_legacy_name_parameter_is_no_longer_needed_or_restrictive(
    tmp_path: Path,
    name: str,
) -> None:
    skill_dir = _write_skill(tmp_path, name, name=name)

    validation = validate_hub_candidate(
        skill_dir,
        expected_name=name,
        allowed_legacy_name=name,
    )

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.name == name


def test_candidate_accepts_name_mismatches_and_parses_boolean_strings(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "directory-name",
        name="manifest-name",
        extra='user-invocable: "false"\n',
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="source-name",
    )

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.user_invocable is False
    assert validation.compatibility_diagnostics == ()


def test_strict_candidate_rejects_missing_or_ambiguous_frontmatter(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "SKILL.md").write_text("No frontmatter", encoding="utf-8")
    assert validate_hub_candidate(missing).diagnostics[0]["code"] == "FRONTMATTER_INVALID"

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text(
        "---\n"
        "name: duplicate\n"
        "name: other\n"
        "description: Duplicate key must not be ambiguous.\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    result = validate_hub_candidate(duplicate)
    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "duplicate key" in result.diagnostics[0]["message"]


def test_strict_candidate_quickly_rejects_yaml_alias_expansion(
    tmp_path: Path,
) -> None:
    width = 10
    depth = 8
    aliases = ["alias0: &alias0 [value]"]
    aliases.extend(
        f"alias{level}: &alias{level} [" + ", ".join([f"*alias{level - 1}"] * width) + "]"
        for level in range(1, depth + 1)
    )
    aliases.append(f"context: *alias{depth}")
    skill_dir = _write_skill(
        tmp_path,
        "alias-expansion",
        extra="\n".join(aliases) + "\n",
    )

    started = time.monotonic()
    result = validate_hub_candidate(skill_dir)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "aliases are not allowed" in result.diagnostics[0]["message"]


def test_strict_candidate_rejects_excessive_yaml_nesting(tmp_path: Path) -> None:
    nested = "[" * 80 + "value" + "]" * 80
    skill_dir = _write_skill(
        tmp_path,
        "deep-frontmatter",
        extra=f"vendor-extension: {nested}\n",
    )

    result = validate_hub_candidate(skill_dir)

    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "maximum YAML nesting depth" in result.diagnostics[0]["message"]


def test_tolerant_compiler_keeps_legacy_yaml_alias_support(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "legacy-alias",
        extra="vendor-defaults: &defaults [one, two]\nvendor-copy: *defaults\n",
    )

    compiled = compile_skill_manifest(skill_dir, SkillLayer.BUNDLED)

    assert compiled.name == "legacy-alias"


def test_community_candidate_accepts_long_description_without_degradation(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "long-description",
        description="x" * (MAX_STANDARD_SKILL_DESCRIPTION_LENGTH + 1),
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.compatibility_diagnostics == ()


@pytest.mark.parametrize(
    ("metadata", "field"),
    [
        ("requires: [python3]\n", "metadata.openclaw.requires"),
        ("requires:\n        bins: python3\n", "metadata.openclaw.requires.bins"),
        ("requires:\n        commands: python3\n", "metadata.openclaw.requires.commands"),
        ('always: "yes"\n', "metadata.openclaw.always"),
        ("os: linux\n", "metadata.openclaw.os"),
        ("install: {kind: uv}\n", "metadata.openclaw.install"),
        (
            "install:\n        - kind: uv\n          bins: python3\n",
            "metadata.openclaw.install[0].bins",
        ),
    ],
)
def test_community_candidate_ignores_known_metadata_type_pollution(
    tmp_path: Path,
    metadata: str,
    field: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "typed-metadata",
        extra="metadata:\n  openclaw:\n    " + metadata,
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert any(
        item["code"] == "FIELD_TYPE_INVALID" and item["field"] == field
        for item in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hooks", "{}"),
        ("context", "fork"),
        ("agent", "general-purpose"),
        ("plugin", "vendor/plugin"),
        ("mcpServers", "{}"),
        ("command-dispatch", "tool"),
        ("entrypoint", "{command: 'python run.py'}"),
        ("kind", "meta"),
        ("composition", "{}"),
    ],
)
def test_community_candidate_ignores_unsupported_execution_dialect_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dialect-skill",
        extra=f"{field}: {value}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.spec.kind == "skill"
    assert validation.spec.entrypoint is None
    assert validation.spec.composition_raw is None
    assert any(
        diagnostic["code"] == "DIALECT_FIELD_UNSUPPORTED" and diagnostic["field"] == field
        for diagnostic in validation.compatibility_diagnostics
    )


def test_strict_candidate_accepts_allowed_tools_as_degraded_compatibility(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "tool-preapproval-skill",
        extra="allowed-tools: Bash(npx example@latest *)\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert validation.compatibility_diagnostics == (
        {
            "code": "TOOL_PREAPPROVAL_IGNORED",
            "message": (
                "allowed-tools requests dialect-specific tool preapproval; "
                "OpenSquilla will keep its normal tool approval policy"
            ),
            "path": str(skill_dir / "SKILL.md"),
            "field": "allowed-tools",
        },
    )


@pytest.mark.parametrize(
    ("extra", "expected_field"),
    [
        ("allowed_tools: Read\n", "allowed_tools"),
        (
            "metadata:\n  openclaw:\n    allowed-tools: Bash(npx example@latest *)\n",
            "metadata.openclaw.allowed-tools",
        ),
    ],
)
def test_tool_preapproval_aliases_remain_nonblocking_degradations(
    tmp_path: Path,
    extra: str,
    expected_field: str,
) -> None:
    skill_dir = _write_skill(tmp_path, "tool-preapproval-alias", extra=extra)

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert any(
        item["code"] == "TOOL_PREAPPROVAL_IGNORED" and item["field"] == expected_field
        for item in validation.compatibility_diagnostics
    )


def test_tool_preapproval_and_execution_fields_are_nonblocking_degradations(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "mixed-dialect-skill",
        extra="allowed-tools: Bash\nhooks: {}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert any(
        item["code"] == "DIALECT_FIELD_UNSUPPORTED" and item["field"] == "hooks"
        for item in validation.compatibility_diagnostics
    )
    assert any(
        item["code"] == "TOOL_PREAPPROVAL_IGNORED" for item in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize(
    "body",
    [
        "Project context: !`npx example@latest info --json`",
        "```!\nnode --version\nnpm --version\n```",
    ],
)
def test_dynamic_shell_context_is_a_nonblocking_degradation(
    tmp_path: Path,
    body: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-context-skill",
        body=body,
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert any(
        item["code"] == "DYNAMIC_CONTEXT_UNSUPPORTED" and item["field"] == "body.dynamic-context"
        for item in validation.compatibility_diagnostics
    )


def test_fenced_dynamic_shell_context_is_detected_with_crlf(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-context-skill",
        body="```!\nnode --version\nnpm --version\n```",
    )
    manifest = skill_dir / "SKILL.md"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("\n", "\r\n"),
        encoding="utf-8",
        newline="",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert any(
        item["code"] == "DYNAMIC_CONTEXT_UNSUPPORTED"
        for item in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize(
    ("namespace", "field", "value"),
    [
        ("", "command", "python run.py"),
        ("platform", "plugin", "vendor/plugin"),
        ("openclaw", "mcpServers", "{}"),
        ("clawdbot", "hooks", "{}"),
        ("opensquilla", "context", "fork"),
    ],
)
def test_community_candidate_ignores_nested_platform_execution_fields(
    tmp_path: Path,
    namespace: str,
    field: str,
    value: str,
) -> None:
    prefix = f"  {namespace}:\n    " if namespace else "  "
    skill_dir = _write_skill(
        tmp_path,
        "dialect-skill",
        extra=f"metadata:\n{prefix}{field}: {value}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    expected_field = f"metadata.{namespace}.{field}" if namespace else f"metadata.{field}"
    assert validation.ok is True
    assert validation.spec is not None
    assert any(
        diagnostic["code"] == "DIALECT_FIELD_UNSUPPORTED" and diagnostic["field"] == expected_field
        for diagnostic in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize("namespace", ["", "openclaw", "opensquilla"])
def test_community_candidate_ignores_dynamic_command_inside_install_item(
    tmp_path: Path,
    namespace: str,
) -> None:
    metadata = (
        f"metadata:\n  {namespace}:\n    install:\n"
        "      - kind: uv\n"
        "        command: python installer.py\n"
        if namespace
        else ("metadata:\n  install:\n    - kind: uv\n      command: python installer.py\n")
    )
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-installer",
        extra=metadata,
    )

    validation = validate_hub_candidate(skill_dir)

    field_prefix = f"metadata.{namespace}" if namespace else "metadata"
    assert validation.ok is True
    assert validation.spec is not None
    assert any(
        item["code"] == "DIALECT_FIELD_UNSUPPORTED"
        and item["field"] == f"{field_prefix}.install[0].command"
        for item in validation.compatibility_diagnostics
    )


def test_instruction_projection_retains_instructions_visibility_and_safe_requirements(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "source-slug",
        name="Runtime_Name",
        extra=(
            'user-invocable: "false"\n'
            'disable-model-invocation: "yes"\n'
            "always: true\n"
            "triggers: [automatic]\n"
            "kind: meta\n"
            "entrypoint: {command: 'python run.py'}\n"
            "composition: {steps: []}\n"
            "meta_priority: 99\n"
            "request_template: {title: unsafe}\n"
            "output_contract: {required: [artifact]}\n"
            "eval_prompts: [{prompt: unsafe}]\n"
            "preference_keys: [secret]\n"
            "policy_tags: [elevated]\n"
            "requires_tools: [shell]\n"
            "vendor-extension: retained-upstream-only\n"
            "metadata:\n"
            "  openclaw:\n"
            "    requires:\n"
            "      bins: [python3, 7]\n"
            "      env: [EXAMPLE_TOKEN, null]\n"
            "    install:\n"
            "      - kind: npm\n"
            "        package: safe-package\n"
            "        bins: [safe-bin]\n"
            "        command: ignored\n"
            "    always: true\n"
        ),
        body="Follow these Community instructions exactly as ordinary prompt text.",
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="source-slug",
    )

    assert validation.ok is True
    spec = validation.spec
    assert spec is not None
    assert spec.name == "Runtime_Name"
    assert spec.content.startswith("Follow these Community instructions")
    assert spec.user_invocable is False
    assert spec.disable_model_invocation is True
    assert spec.always is False
    assert spec.triggers == []
    assert spec.kind == "skill"
    assert spec.entrypoint is None
    assert spec.composition_raw is None
    assert spec.meta_priority == 0
    assert spec.request_template == {}
    assert spec.output_contract == {}
    assert spec.eval_prompts == []
    assert spec.preference_keys == []
    assert spec.policy_tags == []
    assert spec.requires_tools == []
    assert spec.metadata is not None
    assert spec.metadata.always is None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.bins == ["python3"]
    assert spec.metadata.requires.env == ["EXAMPLE_TOKEN"]
    assert len(spec.metadata.install) == 1
    assert spec.metadata.install[0].kind == "node"
    assert spec.metadata.install[0].package == "safe-package"
    codes = {item["code"] for item in validation.compatibility_diagnostics}
    assert "DIALECT_FIELD_UNSUPPORTED" in codes
    assert all(item["field"] != "vendor-extension" for item in validation.compatibility_diagnostics)


@pytest.mark.parametrize(
    "parser_version",
    [
        "",
        "community-strict-v1",
        "community-instruction-v1",
        "future-community-v99",
    ],
)
def test_loader_projects_only_exact_lock_tracked_community_directories(
    tmp_path: Path,
    parser_version: str,
) -> None:
    managed = tmp_path / "managed"
    tracked = _write_skill(
        managed,
        "source-slug",
        name="runtime_name",
        extra="always: true\nkind: meta\nentrypoint: {command: unsafe}\n",
    )
    _write_skill(
        managed,
        "accepted-local-meta",
        extra="always: true\nkind: meta\nentrypoint: {command: trusted}\n",
    )
    lock_path = tmp_path / "skills-lock.json"
    lockfile = Lockfile()
    lockfile.add(
        "source-slug",
        LockEntry(
            source="clawhub",
            identifier="source-slug",
            parser_version=parser_version,
            manifest_name="runtime_name",
            directory_name="source-slug",
            relative_path="source-slug",
            path=str(tracked),
        ),
    )
    lockfile.save(lock_path)
    loader = SkillLoader(
        managed_dir=managed,
        lockfile_path=lock_path,
        snapshot_path=tmp_path / "snapshot.json",
    )

    projected = loader.get_by_name("runtime_name")
    local_meta = loader.get_by_name("accepted-local-meta")

    assert projected is not None
    assert projected.always is False
    assert projected.kind == "skill"
    assert projected.entrypoint is None
    assert local_meta is not None
    assert local_meta.always is True
    assert local_meta.kind == "meta"
    assert local_meta.entrypoint == {"command": "trusted"}


@pytest.mark.parametrize(
    "lock_payload",
    [
        "{not-json",
        json.dumps({"version": 999, "installed": {}}),
    ],
)
def test_loader_quarantines_managed_layer_when_lock_profile_is_untrusted(
    tmp_path: Path,
    lock_payload: str,
) -> None:
    managed = tmp_path / "managed"
    _write_skill(
        managed,
        "looks-local",
        extra="always: true\nkind: meta\nentrypoint: {command: unsafe}\n",
    )
    lock_path = tmp_path / "skills-lock.json"
    lock_path.write_text(lock_payload, encoding="utf-8")
    loader = SkillLoader(
        managed_dir=managed,
        lockfile_path=lock_path,
        snapshot_path=tmp_path / "snapshot.json",
    )

    result = loader.reload(reason="test")

    assert result.success is True
    assert result.partial is True
    assert loader.load_all() == []
    assert len(result.errors) == 1
    assert result.errors[0].name == "managed"
    assert result.errors[0].kept_previous is False


@pytest.mark.parametrize("invalid_lock_path", [False, True])
def test_loader_serves_projected_lkg_when_lock_or_tracked_path_becomes_untrusted(
    tmp_path: Path,
    invalid_lock_path: bool,
) -> None:
    managed = tmp_path / "managed"
    tracked = _write_skill(
        managed,
        "source-slug",
        name="runtime_name",
        extra="kind: meta\nentrypoint: {command: first}\n",
        body="Original instructions.",
    )
    lock_path = tmp_path / "skills-lock.json"
    lockfile = Lockfile()
    lockfile.add(
        "source-slug",
        LockEntry(
            source="clawhub",
            parser_version="community-instruction-v1",
            manifest_name="runtime_name",
            relative_path="source-slug",
            path=str(tracked),
        ),
    )
    lockfile.save(lock_path)
    loader = SkillLoader(
        managed_dir=managed,
        lockfile_path=lock_path,
        snapshot_path=tmp_path / "snapshot.json",
    )
    baseline = loader.get_by_name("runtime_name")
    assert baseline is not None
    assert baseline.kind == "skill"
    assert baseline.entrypoint is None

    (tracked / "SKILL.md").write_text(
        "---\nname: runtime_name\ndescription: Changed.\n"
        "kind: meta\nentrypoint: {command: second}\n---\nChanged instructions.\n",
        encoding="utf-8",
    )
    if invalid_lock_path:
        tracked_entry = lockfile.get("source-slug")
        assert tracked_entry is not None
        tracked_entry.relative_path = "../escape"
        lockfile.save(lock_path)
    else:
        lock_path.write_text("{corrupt", encoding="utf-8")
    result = loader.reload(reason="test.untrusted-lock")
    retained = loader.get_by_name("runtime_name")

    assert result.success is True
    assert result.partial is True
    assert result.errors[0].kept_previous is True
    assert retained is not None
    assert retained.content == "Original instructions."
    assert retained.kind == "skill"
    assert retained.entrypoint is None


def test_loader_lock_fingerprint_is_content_stable_across_identical_restore(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    tracked = _write_skill(managed, "source-slug", name="runtime_name")
    lock_path = tmp_path / "skills-lock.json"
    lockfile = Lockfile()
    lockfile.add(
        "source-slug",
        LockEntry(
            source="clawhub",
            parser_version="community-instruction-v1",
            manifest_name="runtime_name",
            relative_path="source-slug",
            path=str(tracked),
        ),
    )
    lockfile.save(lock_path)
    loader = SkillLoader(
        managed_dir=managed,
        lockfile_path=lock_path,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()
    baseline_generation = loader.snapshot().generation
    original_bytes = lock_path.read_bytes()

    # Recovery writes the same durable state through a new filesystem entry.
    loader.freeze_catalog_for_recovery(reason="test.synthetic-recovery")
    lock_path.write_bytes(original_bytes)
    unchanged = loader.refresh_if_changed("test.identical-lock-restore")

    assert unchanged.changed is False
    assert unchanged.generation == baseline_generation

    loader.clear_catalog_recovery_freeze()
    changed_payload = json.loads(original_bytes)
    changed_payload["installed"]["source-slug"]["parser_version"] = "future-v99"
    lock_path.write_text(json.dumps(changed_payload), encoding="utf-8")
    changed = loader.refresh_if_changed("test.lock-content-change")

    assert changed.changed is True
    assert changed.generation == baseline_generation + 1


def test_loader_relocates_v1_profile_by_storage_key_not_stale_absolute_path(
    tmp_path: Path,
) -> None:
    old_managed = tmp_path / "old-profile" / "skills"
    stale = _write_skill(
        old_managed,
        "source-slug",
        name="runtime_name",
        description="stale copy",
        extra="kind: meta\nentrypoint: {command: stale}\n",
    )
    managed = tmp_path / "moved-profile" / "skills"
    _write_skill(
        managed,
        "source-slug",
        name="runtime_name",
        description="moved copy",
        extra="kind: meta\nentrypoint: {command: moved}\n",
    )
    lock_path = tmp_path / "moved-profile" / "skills-lock.json"
    lockfile = Lockfile()
    lockfile.add(
        "source-slug",
        LockEntry(
            source="clawhub",
            # Historical rows may have neither relative_path nor parser_version.
            path=str(stale),
        ),
    )
    lockfile.save(lock_path)

    loader = SkillLoader(
        managed_dir=managed,
        lockfile_path=lock_path,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loaded = loader.get_by_name("runtime_name")

    assert loaded is not None
    assert loaded.description == "moved copy"
    assert loaded.kind == "skill"
    assert loaded.entrypoint is None


def test_compile_profile_default_preserves_trusted_execution_semantics(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "trusted-meta",
        extra="always: true\nkind: meta\nentrypoint: {command: trusted}\n",
    )

    trusted = compile_skill_manifest(skill_dir, SkillLayer.MANAGED)
    community = compile_skill_manifest(
        skill_dir,
        SkillLayer.MANAGED,
        profile=SkillCompileProfile.COMMUNITY_INSTRUCTION,
    )

    assert trusted.always is True
    assert trusted.kind == "meta"
    assert trusted.entrypoint == {"command": "trusted"}
    assert community.always is False
    assert community.kind == "skill"
    assert community.entrypoint is None


def test_existing_loader_layers_remain_tolerant_of_legacy_uppercase_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundled"
    skill_dir = _write_skill(
        root,
        "AwesomeLegacySkill",
        name="AwesomeLegacySkill",
        extra='user-invocable: "false"\n',
    )
    loader = SkillLoader(
        bundled_dir=root,
        snapshot_path=tmp_path / "snapshot.json",
    )

    loaded = loader.get_by_name("AwesomeLegacySkill")

    assert loaded is not None
    # Historical trusted conversion remains unchanged: a non-empty string is
    # truthy to existing consumers. Community projection parses it explicitly.
    assert loaded.user_invocable == "false"
    assert loaded.instance_id.startswith("bundled:")
    projected = validate_hub_candidate(skill_dir)
    assert projected.ok is True
    assert projected.spec is not None
    assert projected.spec.user_invocable is False


def test_catalog_exposes_candidates_shadowed_instances_and_diagnostics(
    tmp_path: Path,
) -> None:
    extra = tmp_path / "extra"
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    _write_skill(extra, "alpha", description="extra")
    _write_skill(managed, "alpha", description="managed")
    _write_skill(workspace, "alpha", description="workspace")
    broken = workspace / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    loader = SkillLoader(
        extra_dirs=[extra],
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    result = loader.reload(reason="test")
    snapshot = loader.snapshot()

    assert result.partial is True
    assert [skill.description for skill in snapshot.skills] == ["workspace"]
    assert [skill.description for skill in snapshot.candidates] == [
        "extra",
        "managed",
        "workspace",
    ]
    assert [skill.description for skill in snapshot.shadowed] == ["extra", "managed"]
    assert len({skill.instance_id for skill in snapshot.candidates}) == 3
    assert snapshot.diagnostics == snapshot.errors
    assert snapshot.get_candidate_by_instance_id(snapshot.shadowed[0].instance_id) is not None


def test_v15_snapshot_round_trips_candidate_view_and_invalidates_v14(
    tmp_path: Path,
) -> None:
    low = tmp_path / "low"
    high = tmp_path / "high"
    _write_skill(low, "alpha", description="low")
    _write_skill(high, "alpha", description="high")
    snapshot_path = tmp_path / "snapshot.json"

    loader = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    loader.load_all()
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["version"] == 15
    assert len(data["candidates"]) == 2
    assert len(data["shadowed"]) == 1

    restored = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    restored.load_all()
    assert [skill.description for skill in restored.snapshot().candidates] == [
        "low",
        "high",
    ]
    assert [skill.description for skill in restored.snapshot().shadowed] == ["low"]

    # v14 has no managed compile-profile dependency. It must miss so Community
    # candidates cannot be restored with trusted execution fields.
    data["version"] = 14
    data.pop("candidates", None)
    data.pop("shadowed", None)
    data.pop("diagnostics", None)
    for row in data["skills"]:
        row.pop("instance_id", None)
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    legacy = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    assert legacy.load_snapshot() is None
    assert [skill.description for skill in legacy.load_all()] == ["high"]
    assert [skill.description for skill in legacy.snapshot().candidates] == [
        "low",
        "high",
    ]
    assert [skill.description for skill in legacy.snapshot().shadowed] == ["low"]
