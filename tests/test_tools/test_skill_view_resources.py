from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillCompatibilityState,
    SkillDiagnostic,
    SkillInstallState,
    SkillLifecycle,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.installer import InstallResult
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter, SourceSearchReport
from opensquilla.skills.hub.scanner import ScanFinding, ScanResult
from opensquilla.skills.hub.source import SkillMeta
from opensquilla.skills.injector import SkillInjector
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.resources import SkillResources
from opensquilla.tools.builtin import skill_tools as skill_tools_module
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context


async def _skill_view(name: str, file_path: str | None = None) -> str:
    registered = get_default_registry().get("skill_view")
    assert registered is not None
    return await registered.handler(name=name, file_path=file_path)


async def _skill_search_community(query: str, source: str = "clawhub", limit: int = 10) -> str:
    registered = get_default_registry().get("skill_search_community")
    assert registered is not None
    return await registered.handler(query=query, source=source, limit=limit)


async def _skill_list() -> str:
    registered = get_default_registry().get("skill_list")
    assert registered is not None
    return await registered.handler()


async def _skill_install_community(
    identifier: str,
    source: str = "clawhub",
    force: bool = False,
    risk_confirmation: str = "",
) -> str:
    registered = get_default_registry().get("skill_install_community")
    assert registered is not None
    return await registered.handler(
        identifier=identifier,
        source=source,
        force=force,
        risk_confirmation=risk_confirmation,
    )


@pytest.fixture()
def skill_loader(tmp_path: Path) -> Iterator[SkillLoader]:
    bundled_root = tmp_path / "bundled"
    skill_dir = bundled_root / "deck"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "assets").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deck\ndescription: Deck helper\n---\n"
        "See [guide](references/guide.md).\n"
        "Run {baseDir}/scripts/inspect.py or {base_dir}/scripts/inspect.py.\n",
        encoding="utf-8",
    )
    env_any_dir = bundled_root / "env-any"
    env_any_dir.mkdir(parents=True)
    (env_any_dir / "SKILL.md").write_text(
        "---\n"
        "name: env-any\n"
        "description: Needs one of two API keys\n"
        "metadata:\n"
        "  opensquilla:\n"
        "    requires:\n"
        "      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]\n"
        "---\n"
        "Needs an API key.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text(
        "reference body keeps {baseDir}\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "inspect.py").write_text("print('script body')\n", encoding="utf-8")
    (skill_dir / "assets" / "palette.txt").write_text("blue\n", encoding="utf-8")
    (skill_dir / "secret.txt").write_text("do not expose\n", encoding="utf-8")

    loader = SkillLoader(
        bundled_dir=bundled_root,
        workspace_dir=tmp_path / "workspace",
        managed_dir=tmp_path / "managed",
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "skills.snapshot.json",
    )
    previous_loader = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    try:
        yield loader
    finally:
        skill_tools_module._loader = previous_loader


@pytest.mark.asyncio
async def test_skill_view_reads_registered_skill_resources_by_relative_path(
    skill_loader: SkillLoader,
) -> None:
    assert "reference body" in await _skill_view("deck", "references/guide.md")
    assert "script body" in await _skill_view("deck", "scripts/inspect.py")
    assert "blue" in await _skill_view("deck", "assets/palette.txt")


@pytest.mark.asyncio
async def test_skill_view_expands_base_dir_only_in_skill_body(
    skill_loader: SkillLoader,
) -> None:
    spec = skill_loader.get_by_name("deck")
    assert spec is not None

    body = await _skill_view("deck")
    explicit_body = await _skill_view("deck", "SKILL.md")
    resource = await _skill_view("deck", "references/guide.md")

    expected_script = f"{spec.base_dir}/scripts/inspect.py"
    assert body.count(expected_script) == 2
    assert explicit_body == body
    assert "{baseDir}" not in body
    assert "{base_dir}" not in body
    # Existing fixed-resource reads stay literal and do not gain a templating
    # surface merely because the main body expands its runtime location.
    assert "{baseDir}" in resource


def test_full_catalog_prompt_does_not_expose_skill_location(
    skill_loader: SkillLoader,
) -> None:
    spec = skill_loader.get_by_name("deck")
    assert spec is not None

    prompt = SkillInjector().inject_skills("", [spec], max_chars=10_000)

    assert "<location>" not in prompt
    assert spec.base_dir not in prompt


@pytest.mark.asyncio
async def test_skill_view_rejects_resource_paths_that_escape_skill_directory(
    skill_loader: SkillLoader,
) -> None:
    result = await _skill_view("deck", "../secret.txt")

    assert "File not found in skill 'deck': ../secret.txt" == result
    assert "do not expose" not in result


def test_managed_v2_resources_allow_only_recorded_plain_text(tmp_path: Path) -> None:
    skill_dir = tmp_path / "managed-skill"
    skill_dir.mkdir()
    (skill_dir / "notes.txt").write_text("recorded text\n", encoding="utf-8")
    (skill_dir / "unrecorded.txt").write_text("private\n", encoding="utf-8")
    references = skill_dir / "references"
    references.mkdir()
    (references / "recorded.md").write_text("recorded reference\n", encoding="utf-8")
    (references / "unrecorded.md").write_text("unrecorded reference\n", encoding="utf-8")
    (skill_dir / "binary.bin").write_bytes(b"\x00\xff")
    (skill_dir / "utf8-with-nul.txt").write_bytes(b"prefix\x00suffix")
    (skill_dir / ".provenance.json").write_text("secret\n", encoding="utf-8")
    resources = SkillResources(
        skill_dir,
        managed_manifest_files={
            "notes.txt",
            "references/recorded.md",
            "binary.bin",
            "utf8-with-nul.txt",
            ".provenance.json",
        },
    )

    assert resources.read_resource("notes.txt") == "recorded text\n"
    assert resources.read_resource("unrecorded.txt") is None
    assert resources.read_resource("references/recorded.md") == "recorded reference\n"
    assert resources.read_resource("recorded.md") == "recorded reference\n"
    assert resources.read_resource("references/unrecorded.md") is None
    assert resources.read_resource("binary.bin") is None
    assert resources.read_resource("utf8-with-nul.txt") is None
    assert resources.read_resource(".provenance.json") is None


@pytest.mark.asyncio
async def test_skill_view_uses_injected_management_lockfile_for_v2_resources(
    skill_loader: SkillLoader,
    tmp_path: Path,
) -> None:
    assert skill_loader.managed_dir is not None
    skill_dir = skill_loader.managed_dir / "managed-resource"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: managed-resource\ndescription: custom lock fixture\n---\nBody.\n",
        encoding="utf-8",
    )
    (skill_dir / "notes.txt").write_text("custom lock resource\n", encoding="utf-8")
    custom_lock = tmp_path / "custom-skills-lock.json"
    digest = compute_tree_sha256(skill_dir)
    Lockfile(
        installed={
            "managed-resource": LockEntry(
                source="github",
                identifier="owner/repo:managed-resource",
                relative_path="managed-resource",
                manifest_name="managed-resource",
                parser_version="community-strict-v1",
                tree_sha256=digest,
                sha256=digest,
                extra={"files": ["SKILL.md", "notes.txt"]},
            )
        }
    ).save(custom_lock)
    skill_loader.reload(force=True, reason="test.custom-resource-lock")
    service = SkillManagementService(
        router=SourceRouter([]),
        managed_dir=skill_loader.managed_dir,
        lockfile_path=custom_lock,
        loader=skill_loader,
        journal_path=tmp_path / "transaction.json",
    )
    skill_tools_module.create_skill_tools(
        skill_loader,
        management_service=service,
    )

    assert await _skill_view("managed-resource", "notes.txt") == "custom lock resource\n"


@pytest.mark.asyncio
async def test_skill_view_missing_skill_uses_catalog_guidance(
    skill_loader: SkillLoader,
) -> None:
    result = await _skill_view("missing-skill")

    assert "Skill not found: missing-skill" in result
    assert "current skill catalog" in result
    assert "Do not search host filesystem paths" in result
    assert "skill_list" in result


@pytest.mark.asyncio
async def test_skill_view_uses_catalog_pinned_to_current_turn(
    skill_loader: SkillLoader,
) -> None:
    skill_loader.load_all()
    pinned = skill_loader.snapshot()
    skill_file = Path(pinned.get_by_name("deck").file_path)  # type: ignore[union-attr]
    skill_file.write_text(
        "---\nname: deck\ndescription: Updated\n---\nnew body\n",
        encoding="utf-8",
    )
    skill_loader.reload(reason="test")

    token = current_tool_context.set(ToolContext(skill_catalog=pinned))
    try:
        assert "See [guide]" in await _skill_view("deck")
    finally:
        current_tool_context.reset(token)

    assert await _skill_view("deck") == "new body"


@pytest.mark.asyncio
async def test_skill_view_pinned_turn_refuses_resources_from_new_generation(
    skill_loader: SkillLoader,
) -> None:
    skill_loader.load_all()
    pinned = skill_loader.snapshot()
    pinned_spec = pinned.get_by_name("deck")
    assert pinned_spec is not None
    assert pinned_spec.tree_digest
    resource = Path(pinned_spec.base_dir) / "references" / "guide.md"
    resource.write_text("new generation resource\n", encoding="utf-8")

    reloaded = skill_loader.refresh_if_changed("resource update")
    assert reloaded.modified == ("deck",)
    assert skill_loader.snapshot().generation == pinned.generation + 1

    token = current_tool_context.set(ToolContext(skill_catalog=pinned))
    try:
        result = await _skill_view("deck", "references/guide.md")
    finally:
        current_tool_context.reset(token)

    assert "current catalog was pinned" in result
    assert "Retry skill_view in the next turn" in result
    assert "new generation resource" not in result
    assert await _skill_view("deck", "references/guide.md") == "new generation resource\n"


@pytest.mark.asyncio
async def test_skill_view_offloads_pinned_tree_checks_and_resource_read(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_loader.load_all()
    pinned = skill_loader.snapshot()
    calls: list[object] = []
    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(skill_tools_module.asyncio, "to_thread", tracking_to_thread)
    token = current_tool_context.set(ToolContext(skill_catalog=pinned))
    try:
        result = await _skill_view("deck", "references/guide.md")
    finally:
        current_tool_context.reset(token)

    assert "reference body" in result
    assert [getattr(func, "__name__", "") for func in calls] == [
        "compute_tree_sha256",
        "read_resource",
        "compute_tree_sha256",
    ]


@pytest.mark.asyncio
async def test_skill_list_reports_missing_env_any_groups(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)

    result = await _skill_list()

    assert "OPENROUTER_API_KEY or ARK_API_KEY (env var group)" in result


@pytest.mark.asyncio
async def test_skill_search_community_returns_hub_results_with_installed_flag(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str | None]] = []

        async def search(
            self,
            query: str,
            limit: int = 20,
            source_id: str | None = None,
        ) -> list[SkillMeta]:
            self.calls.append((query, limit, source_id))
            return [
                SkillMeta(
                    name="plotter",
                    description="Plot charts",
                    version="1.0.0",
                    author="Alice",
                    source_id="clawhub",
                    trust_level="community",
                    identifier="@alice/plotter",
                    canonical_identifier="@alice/plotter",
                ),
                SkillMeta(
                    name="plotter",
                    description="A different publisher",
                    version="1.0.0",
                    author="Bob",
                    source_id="clawhub",
                    trust_level="community",
                    identifier="@bob/plotter",
                    canonical_identifier="@bob/plotter",
                ),
            ]

    router = FakeRouter()
    monkeypatch.setattr(skill_tools_module, "get_default_skill_router", lambda: router)
    lockfile = Lockfile(
        installed={
            "plotter": LockEntry(
                source="clawhub",
                identifier="@alice/plotter",
                source_package_id="clawhub:@alice/plotter",
            )
        }
    )
    monkeypatch.setattr(skill_tools_module, "installed_skill_lockfile", lambda: lockfile)

    payload = json.loads(await _skill_search_community("plot", source="clawhub", limit=5))

    assert payload["status"] == "ok"
    assert router.calls == [("plot", 5, "clawhub")]
    assert payload["results"] == [
        {
            "name": "plotter",
            "description": "Plot charts",
            "version": "1.0.0",
            "author": "Alice",
            "source": "clawhub",
            "trust_level": "community",
            "identifier": "@alice/plotter",
            "installReference": "@alice/plotter",
            "installed": True,
        },
        {
            "name": "plotter",
            "description": "A different publisher",
            "version": "1.0.0",
            "author": "Bob",
            "source": "clawhub",
            "trust_level": "community",
            "identifier": "@bob/plotter",
            "installReference": "@bob/plotter",
            "installed": False,
        },
    ]


@pytest.mark.asyncio
async def test_skill_search_community_returns_partial_results_with_diagnostics(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = SkillDiagnostic(
        code="SOURCE_RATE_LIMITED",
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.SOURCE,
        message="The source rate-limited this search.",
        blocking=True,
        details={"source": "limited", "retryAfter": "60"},
    )

    class PartialRouter:
        async def search_with_diagnostics(
            self,
            query: str,
            limit: int = 20,
            source_id: str | None = None,
        ) -> SourceSearchReport:
            assert (query, limit, source_id) == ("plot", 5, None)
            return SourceSearchReport(
                results=(
                    SkillMeta(
                        name="plotter",
                        source_id="healthy",
                        identifier="plotter",
                    ),
                ),
                diagnostics=(diagnostic,),
                searched_sources=("healthy", "limited"),
                successful_sources=("healthy",),
            )

    monkeypatch.setattr(
        skill_tools_module,
        "get_default_skill_router",
        lambda: PartialRouter(),
    )
    monkeypatch.setattr(
        skill_tools_module,
        "installed_skill_lockfile",
        lambda: Lockfile(),
    )

    payload = json.loads(await _skill_search_community("plot", source="all", limit=5))

    assert payload["status"] == "partial"
    assert [row["name"] for row in payload["results"]] == ["plotter"]
    assert payload["diagnostics"] == [diagnostic.to_dict()]
    assert payload["partial"] is True
    assert payload["allSourcesUnavailable"] is False


@pytest.mark.asyncio
async def test_agent_search_uses_injected_management_router_and_lockfile(
    skill_loader: SkillLoader,
    tmp_path: Path,
) -> None:
    class FakeRouter:
        async def search(
            self,
            query: str,
            limit: int = 20,
            source_id: str | None = None,
        ) -> list[SkillMeta]:
            assert (query, limit, source_id) == ("plot", 3, "clawhub")
            return [
                SkillMeta(
                    name="plotter",
                    source_id="clawhub",
                    identifier="@alice/plotter",
                    canonical_identifier="@alice/plotter",
                )
            ]

    custom_lock = tmp_path / "agent-search-lock.json"
    Lockfile(
        installed={
            "plotter": LockEntry(
                source="clawhub",
                identifier="@alice/plotter",
                source_package_id="clawhub:@alice/plotter",
            )
        }
    ).save(custom_lock)
    assert skill_loader.managed_dir is not None
    service = SkillManagementService(
        router=FakeRouter(),  # type: ignore[arg-type]
        managed_dir=skill_loader.managed_dir,
        lockfile_path=custom_lock,
        loader=skill_loader,
        journal_path=tmp_path / "agent-search-transaction.json",
    )
    skill_tools_module.create_skill_tools(
        skill_loader,
        management_service=service,
    )

    payload = json.loads(await _skill_search_community("plot", limit=3))

    assert payload["results"][0]["installed"] is True


@pytest.mark.asyncio
async def test_skill_install_community_uses_loader_managed_dir_and_marks_catalog_dirty(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInstaller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, bool]] = []

        async def install(
            self,
            identifier: str,
            source_id: str,
            force: bool = False,
        ) -> InstallResult:
            self.calls.append((identifier, source_id, force))
            return InstallResult(
                success=True,
                name="plotter",
                message="installed",
                path=str(skill_loader.managed_dir / "plotter"),
            )

    installer = FakeInstaller()
    captured: dict[str, Path | None] = {}

    def fake_builder(*, managed_dir: Path | None = None) -> FakeInstaller:
        captured["managed_dir"] = managed_dir
        return installer

    monkeypatch.setattr(skill_tools_module, "build_default_skill_installer", fake_builder)
    skill_loader.load_all()
    snapshot = skill_loader.snapshot()
    assert skill_loader._dirty is False

    payload = json.loads(await _skill_install_community("plotter"))

    assert captured["managed_dir"] == skill_loader.managed_dir
    assert installer.calls == [("plotter", "clawhub", False)]
    assert payload["status"] == "installed"
    assert payload["success"] is True
    assert Path(payload["path"]).name == "plotter"
    assert skill_loader._dirty is True
    assert skill_loader.snapshot() is snapshot


@pytest.mark.asyncio
async def test_skill_install_community_rejects_unsupported_replace_source_without_call(
    skill_loader: SkillLoader,
) -> None:
    class LegacyInstaller:
        calls = 0

        async def install(
            self,
            identifier: str,
            source_id: str,
            force: bool = False,
        ) -> InstallResult:
            self.calls += 1
            return InstallResult(success=True, name=identifier)

    installer = LegacyInstaller()
    skill_tools_module.create_skill_tools(
        skill_loader,
        management_service=installer,  # type: ignore[arg-type]
    )
    registered = get_default_registry().get("skill_install_community")
    assert registered is not None

    payload = json.loads(
        await registered.handler(
            identifier="plotter",
            source="clawhub",
            replace_source=True,
        )
    )

    assert payload["success"] is False
    assert payload["diagnostics"][0]["code"] == "INSTALLER_CAPABILITY_UNSUPPORTED"
    assert installer.calls == 0
    assert skill_loader._dirty is False


@pytest.mark.asyncio
async def test_skill_install_community_does_not_retry_builder_internal_type_error(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def failing_builder(
        *,
        managed_dir: Path | None = None,
        loader: SkillLoader | None = None,
        offline: bool = False,
    ) -> object:
        nonlocal calls
        calls += 1
        raise TypeError("builder failed internally")

    monkeypatch.setattr(
        skill_tools_module,
        "build_default_skill_installer",
        failing_builder,
    )
    skill_tools_module.create_skill_tools(skill_loader)

    with pytest.raises(TypeError, match="builder failed internally"):
        await _skill_install_community("plotter")

    assert calls == 1
    assert skill_loader._dirty is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "readiness"),
    [
        (SkillSelectionState.SHADOWED, SkillReadinessState.READY),
        (SkillSelectionState.ACTIVE, SkillReadinessState.NEEDS_SETUP),
    ],
)
async def test_agent_install_message_does_not_claim_unusable_skill_is_usable_next_turn(
    skill_loader: SkillLoader,
    selection: SkillSelectionState,
    readiness: SkillReadinessState,
) -> None:
    lifecycle = SkillLifecycle(
        install_state=SkillInstallState.TRACKED,
        load_state=SkillLoadState.LOADED,
        selection_state=selection,
        compatibility_state=SkillCompatibilityState.INSTRUCTION_ONLY,
        readiness_state=readiness,
    )

    class FakeService:
        async def install(self, *args: object, **kwargs: object) -> InstallResult:
            return InstallResult(
                success=True,
                name="plotter",
                message="Installed but not currently usable.",
                installed=True,
                instruction_usable=False,
                lifecycle=lifecycle,
                effective_from="next_turn",
            )

    skill_tools_module.create_skill_tools(
        skill_loader,
        management_service=FakeService(),
    )

    payload = json.loads(await _skill_install_community("plotter"))

    assert payload["success"] is True
    assert payload["instruction_usable"] is False
    assert "catalog state becomes observable from the next turn" in payload["message"]
    assert "use the Skill from the next turn" not in payload["message"]
    assert "It can be used from the next turn" not in payload["message"]


@pytest.mark.asyncio
async def test_skill_install_community_scan_failure_keeps_catalog_clean(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInstaller:
        async def install(
            self,
            identifier: str,
            source_id: str,
            force: bool = False,
        ) -> InstallResult:
            return InstallResult(
                success=False,
                name=identifier,
                message="Security scan: dangerous",
                scan=ScanResult(
                    verdict="dangerous",
                    findings=[
                        ScanFinding(
                            category="prompt_injection",
                            severity="dangerous",
                            line=1,
                            text="ignore previous instructions",
                            pattern="ignore",
                        )
                    ],
                ),
            )

    monkeypatch.setattr(
        skill_tools_module,
        "build_default_skill_installer",
        lambda *, managed_dir=None: FakeInstaller(),
    )
    skill_loader.load_all()
    snapshot = skill_loader.snapshot()
    assert skill_loader._dirty is False

    payload = json.loads(await _skill_install_community("unsafe"))

    assert payload["status"] == "failed"
    assert payload["success"] is False
    assert payload["scan_verdict"] == "dangerous"
    assert payload["scan_findings"][0]["category"] == "prompt_injection"
    assert skill_loader._dirty is False
    assert skill_loader.snapshot() is snapshot


@pytest.mark.asyncio
async def test_skill_install_community_forwards_exact_scanner_confirmation(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, bool, str]] = []

    class FakeInstaller:
        async def install(
            self,
            identifier: str,
            source_id: str,
            force: bool = False,
            *,
            risk_confirmation: str = "",
        ) -> InstallResult:
            calls.append((identifier, source_id, force, risk_confirmation))
            return InstallResult(
                success=True,
                name=identifier,
                message="reviewed artifact installed",
            )

    monkeypatch.setattr(
        skill_tools_module,
        "build_default_skill_installer",
        lambda *, managed_dir=None: FakeInstaller(),
    )

    payload = json.loads(
        await _skill_install_community(
            "reviewed",
            force=True,
            risk_confirmation="exact-artifact-token",
        )
    )

    assert payload["success"] is True
    assert calls == [
        ("reviewed", "clawhub", True, "exact-artifact-token")
    ]


@pytest.mark.asyncio
async def test_skill_install_community_rejects_force_only_legacy_installer(
    skill_loader: SkillLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class LegacyInstaller:
        async def install(
            self,
            identifier: str,
            source_id: str,
            force: bool = False,
        ) -> InstallResult:
            nonlocal called
            called = True
            return InstallResult(success=True, name=identifier)

    monkeypatch.setattr(
        skill_tools_module,
        "build_default_skill_installer",
        lambda *, managed_dir=None: LegacyInstaller(),
    )

    payload = json.loads(
        await _skill_install_community(
            "reviewed",
            force=True,
            risk_confirmation="exact-artifact-token",
        )
    )

    assert called is False
    assert payload["success"] is False
    assert "riskConfirmation" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("force", "replace_source"),
    [("false", False), (False, "false")],
)
async def test_skill_install_community_rejects_non_boolean_safety_flags(
    skill_loader: SkillLoader,
    force: object,
    replace_source: object,
) -> None:
    registered = get_default_registry().get("skill_install_community")
    assert registered is not None

    with pytest.raises(ToolError, match="must be booleans"):
        await registered.handler(
            identifier="unsafe",
            source="clawhub",
            force=force,
            replace_source=replace_source,
        )
