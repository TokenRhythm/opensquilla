"""Architecture gates for the typed RPC Contract migration."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "src" / "opensquilla"
GATEWAY_ROOT = PACKAGE_ROOT / "gateway"
CONTRACT_ROOT = PACKAGE_ROOT / "contracts"
GENERATED_CONTRACT_ROOT = CONTRACT_ROOT / "generated"
RPC_CONTEXT = GATEWAY_ROOT / "rpc" / "registry.py"
GENERATED_WIRE_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_list_contract.py",
        "src/opensquilla/gateway/adapters/sessions_list_contract.py",
        "src/opensquilla/contracts/adapters/sessions_resolve_contract.py",
        "src/opensquilla/gateway/adapters/sessions_resolve_contract.py",
        "src/opensquilla/contracts/adapters/sessions_search_contract.py",
        "src/opensquilla/gateway/adapters/sessions_search_contract.py",
        "src/opensquilla/contracts/adapters/sessions_changed_contract.py",
        "src/opensquilla/contracts/adapters/conversation_events.py",
        # S16-A keeps approval wire models at the dormant ApprovalCenter
        # boundary; no Gateway handler or UI consumer may import them yet.
        "src/opensquilla/contracts/adapters/approval_center_contract.py",
        # S17 keeps the two migrated Goal operations behind GoalCenter; the
        # remaining Goal mutations stay on the legacy path.  S18 adds the
        # Gateway registration boundary without changing their implementation.
        "src/opensquilla/contracts/adapters/goals_contract.py",
        "src/opensquilla/gateway/adapters/goals_contract.py",
        "src/opensquilla/gateway/adapters/plans_contract.py",
        # Session read Contracts are consumed only by the Gateway registration
        # Adapter; Application Modules and handlers receive domain values.
        "src/opensquilla/gateway/adapters/session_read_contract.py",
        # Session lifecycle wire models terminate at the registration Adapter;
        # the Application Module receives transport-neutral typed commands.
        "src/opensquilla/gateway/adapters/session_lifecycle_contract.py",
        # Reset and compact wire models terminate at SessionMaintenance's
        # generated registration Adapter.
        "src/opensquilla/gateway/adapters/session_maintenance_contract.py",
        # Canonical and legacy turn wire models terminate at TurnAdmission's
        # generated registration Adapter.
        "src/opensquilla/gateway/adapters/turn_admission_contract.py",
        # Durable pending-input wire models terminate at the queue Adapter;
        # the Application Module receives queue identities and revisions.
        "src/opensquilla/gateway/adapters/pending_input_queue_contract.py",
        # Usage, command, feedback, prompt-cache, and clarification wire
        # models terminate at their generated registration Adapter.
        "src/opensquilla/gateway/adapters/conversation_ancillary_contract.py",
        # AgentCatalog wire models terminate at its generated registration
        # Adapter; the Application Module sees explicit create/update commands.
        "src/opensquilla/gateway/adapters/agent_catalog_contract.py",
        # Channel administration wire models terminate at its generated
        # registration Adapter; Application Modules see typed channel intents.
        "src/opensquilla/gateway/adapters/channel_administration_contract.py",
        # Cron scheduling and subscription wire models terminate at the
        # generated registration Adapter; Application Modules stay transport-neutral.
        "src/opensquilla/gateway/adapters/cron_scheduler_contract.py",
        # Runtime/readiness/log wire models terminate at the Observability
        # registration Adapter; collectors receive transport-neutral queries.
        "src/opensquilla/gateway/adapters/observability_contract.py",
        # SkillCatalog read wire models terminate at its generated registration
        # Adapter; the Application Module sees domain identities and queries.
        "src/opensquilla/gateway/adapters/skill_catalog_contract.py",
        # SkillManagement wire models terminate at its generated registration
        # Adapter; the Application Module sees explicit mutation commands.
        "src/opensquilla/gateway/adapters/skill_management_contract.py",
        # Proposal review wire models terminate at its registration Adapter;
        # scheduler rollback and catalog invalidation stay in the Application Module.
        "src/opensquilla/gateway/adapters/skill_proposal_review_contract.py",
        # Artifact Workbench wire models terminate at its registration Adapter;
        # the Application composition receives only typed domain commands.
        "src/opensquilla/gateway/adapters/artifact_workbench_contract.py",
        # SandboxRuntime handlers stay legacy-compatible while generated
        # descriptors own registration metadata and success validation.
        "src/opensquilla/gateway/adapters/sandbox_runtime_contract.py",
        # Platform setup wire models terminate at the generated registration
        # Adapter; Application Modules receive transport-neutral commands.
        "src/opensquilla/gateway/adapters/platform_setup_contract.py",
    }
)
GENERATED_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_list_contract.py",
        "src/opensquilla/engine/commands.py",
        "src/opensquilla/gateway/app.py",
        "src/opensquilla/gateway/guest_rpc_policy.py",
        "src/opensquilla/gateway/rpc_system.py",
        "src/opensquilla/gateway/scopes.py",
    }
)
SESSIONS_RESOLVE_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_resolve_contract.py",
        "src/opensquilla/gateway/scopes.py",
    }
)
SESSIONS_SEARCH_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_search_contract.py",
        "src/opensquilla/gateway/scopes.py",
    }
)
GOALS_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/goals_contract.py",
    }
)
GOAL_MUTATION_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/gateway/adapters/goals_contract.py",
    }
)
PLANS_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/gateway/adapters/plans_contract.py",
    }
)
SESSIONS_CHANGED_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_changed_contract.py",
    }
)
CONVERSATION_EVENTS_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/conversation_events.py",
    }
)
SESSIONS_CREATE_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/gateway/scopes.py",
    }
)
SESSIONS_RENAME_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/gateway/guest_rpc_policy.py",
        "src/opensquilla/gateway/scopes.py",
    }
)
SESSIONS_DELETE_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/gateway/guest_rpc_policy.py",
        "src/opensquilla/gateway/scopes.py",
    }
)
SESSIONS_LIST_LITERAL_ALLOWLIST: Counter[str] = Counter(
    {
        # This is a transport concurrency policy registry.  WebSocket is an
        # explicitly frozen file in the first vertical slice, not a second
        # method implementation or payload schema.
        "src/opensquilla/gateway/websocket.py": 1,
    }
)
SESSIONS_RESOLVE_LITERAL_ALLOWLIST: Counter[str] = Counter()
SESSIONS_LIST_GATEWAY_ADAPTER = PACKAGE_ROOT / "gateway" / "adapters" / "sessions_list_contract.py"
RUNTIME_RPC_METHOD_BASELINE = 306
STATIC_RPC_DECORATOR_BASELINE = 140

# Physical lines in the sessions/runtime slice remain tracked for the final
# closure measurement below.  The temporary S2a cumulative growth budget was
# intentionally retired when the sessions.search slice completed: later
# slices are allowed to improve a shared compatibility handler, and carrying
# that historical ceiling forward would either block valid work or encourage
# arbitrary budget increases.  Z1 is the authoritative gate for net authored
# production LOC reduction after each complete domain migration.
AUTHORED_RUNTIME_FILES = (
    "opensquilla-webui/src/App.vue",
    "opensquilla-webui/src/components/sessions/SessionInspectDrawer.vue",
    "opensquilla-webui/src/composables/usage/useUsageData.ts",
    "opensquilla-webui/src/composables/useSessions.ts",
    "opensquilla-webui/src/main.ts",
    "opensquilla-webui/src/types/rpc.ts",
    "opensquilla-webui/src/views/OverviewView.vue",
    "opensquilla-webui/src/views/SessionsView.vue",
    "opensquilla-webui/src/views/UsageView.vue",
    "opensquilla-webui/src/adapters/gateway/sessionDirectoryV4.ts",
    "opensquilla-webui/src/adapters/gateway/sessionLifecycleV4.ts",
    "opensquilla-webui/src/modules/sessionDirectory.ts",
    "opensquilla-webui/src/modules/sessionLifecycle.ts",
    "opensquilla-webui/src/modules/sessionRunStatus.ts",
    "src/opensquilla/cli/gateway_client.py",
    "src/opensquilla/contracts/adapters/sessions_list_contract.py",
    "src/opensquilla/contracts/adapters/sessions_resolve_contract.py",
    "src/opensquilla/engine/commands.py",
    "src/opensquilla/gateway/adapters/sessions_list_contract.py",
    "src/opensquilla/gateway/adapters/sessions_resolve_contract.py",
    "src/opensquilla/gateway/app.py",
    "src/opensquilla/gateway/guest_rpc_policy.py",
    "src/opensquilla/gateway/rpc_sessions.py",
    "src/opensquilla/gateway/rpc_system.py",
    "src/opensquilla/gateway/scopes.py",
    "src/opensquilla/gateway_client.py",
    "src/opensquilla/application/session_directory.py",
    "src/opensquilla/session_key.py",
)
AUTHORED_RUNTIME_LOC_BASELINE = 26_507

F2_TRANSPORT_FOUNDATION_FILES = (
    "opensquilla-webui/src/adapters/gateway/privateHttpTransport.ts",
    "opensquilla-webui/src/adapters/gateway/privateTransports.ts",
    "src/opensquilla/gateway/adapters/contract_method.py",
)
F2_GATEWAY_COMPOSITION_ROOT = (
    "opensquilla-webui/src/adapters/gateway/gatewayAdapters.ts"
)
# F2 adds three explicitly reviewed HTTP hardening slices on top of the
# initial 849-line foundation: 58 lines for body lifecycle ownership, 103
# lines for filename/method/body validation (less 9 lines from native brand
# tightening), 135 lines for cancellation-safe response-body ownership, and
# 3 lines for hostile request-option normalization and 3 lines for endpoint
# input normalization.
# Keep the Transport allowance explicit so later domain slices cannot hide
# authored growth behind this infrastructure exception.  The Gateway Adapter
# composition root is deliberately excluded: every completed domain slice
# adds typed imports, one Interface member, and one Adapter registration there,
# which is reviewed architecture rather than Transport-foundation growth.
# Its structure is governed separately below and by the WebUI architecture
# import gate.  The three stable Transport files totalled 1,125 physical lines
# on the reviewed #1525 baseline.
F2_TRANSPORT_FOUNDATION_LOC_CEILING = 1_125

WEBUI_SOURCE_ROOT = ROOT / "opensquilla-webui" / "src"
WEBUI_LEGACY_TRANSPORT_IDENTIFIERS = (
    "supportsMethod",
    "supportsEvent",
    "waitForConnection",
    "markMethodUnavailable",
    "createLegacySessionConversation",
)

R3_APPLICATION_MODULE_FILES = (
    "src/opensquilla/application/app_settings.py",
    "src/opensquilla/application/provider_configuration.py",
    "src/opensquilla/application/sandbox_runtime.py",
    "src/opensquilla/application/session_read.py",
    "src/opensquilla/application/setup_workflow.py",
    "src/opensquilla/application/session_maintenance.py",
    "src/opensquilla/application/observability.py",
    "src/opensquilla/application/skill_catalog.py",
    "src/opensquilla/application/skill_management.py",
    "src/opensquilla/application/skill_proposal_review.py",
    "src/opensquilla/application/artifact_workbench.py",
)

# Generated schema artifacts and consumer tests are intentionally excluded:
# this ledger measures the five authored seams that carry SandboxRuntime's
# domain, wire projection, and registration complexity.  The large-PR plan
# requires a hard split before this surface exceeds 3,000 physical lines.
SANDBOX_RUNTIME_AUTHORED_FILES = (
    "opensquilla-webui/src/adapters/gateway/sandboxRuntimeV4.ts",
    "opensquilla-webui/src/modules/sandboxRuntime.ts",
    "src/opensquilla/application/sandbox_runtime.py",
    "src/opensquilla/gateway/adapters/sandbox_runtime.py",
    "src/opensquilla/gateway/adapters/sandbox_runtime_contract.py",
)
SANDBOX_RUNTIME_AUTHORED_LOC_CEILING = 3_000

# This ledger is deliberately separate from SandboxRuntime: it measures only
# the authored SessionLifecycle Module/Port and Gateway Adapter seams.  The
# large-PR plan requires the predefined split before this seam exceeds 3,000
# physical lines; generated artifacts, fixtures, and the reused legacy writer
# fencing Implementation in rpc_sessions.py are not newly authored seams.
SESSION_LIFECYCLE_AUTHORED_FILES = (
    "src/opensquilla/application/session_lifecycle.py",
    "src/opensquilla/gateway/adapters/session_lifecycle.py",
    "src/opensquilla/gateway/adapters/session_lifecycle_contract.py",
    "src/opensquilla/application/session_maintenance.py",
    "src/opensquilla/gateway/adapters/session_maintenance.py",
    "src/opensquilla/gateway/adapters/session_maintenance_contract.py",
    "src/opensquilla/application/turn_admission.py",
    "src/opensquilla/gateway/adapters/turn_admission.py",
    "src/opensquilla/gateway/adapters/turn_admission_contract.py",
    "src/opensquilla/application/pending_input_queue.py",
    "src/opensquilla/gateway/adapters/pending_input_queue.py",
    "src/opensquilla/gateway/adapters/pending_input_queue_contract.py",
    "src/opensquilla/application/conversation_ancillary.py",
    "src/opensquilla/gateway/adapters/conversation_ancillary.py",
    "src/opensquilla/gateway/adapters/conversation_ancillary_contract.py",
)
SESSION_LIFECYCLE_AUTHORED_LOC_CEILING = 3_000

# Existing cross-rpc private imports are architectural debt. This exact ledger
# prevents growth and also fails stale when an import is removed, so reductions
# must be made explicit instead of leaving an ever-growing allowlist.
APPROVED_PRIVATE_RPC_IMPORTS: Counter[tuple[str, str, str]] = Counter(
    {
        (
            "src/opensquilla/cli/agent_cmd.py",
            "opensquilla.gateway.rpc_sessions",
            "_apply_run_context_route_metadata",
        ): 1,
        (
            "src/opensquilla/cli/tui/standalone_runtime.py",
            "opensquilla.gateway.rpc_sessions",
            "_apply_run_context_route_metadata",
        ): 1,
        (
            "src/opensquilla/gateway/channel_dispatch.py",
            "opensquilla.gateway.rpc_sessions",
            "_apply_run_context_route_metadata",
        ): 2,
        (
            "src/opensquilla/gateway/rpc_artifact_editing.py",
            "opensquilla.gateway.rpc_artifacts",
            "_session_id_for_key",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_chat.py",
            "opensquilla.gateway.rpc_sessions",
            "_handle_sessions_abort",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_chat.py",
            "opensquilla.gateway.rpc_sessions",
            "_handle_sessions_send",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_models.py",
            "opensquilla.gateway.rpc_config",
            "_handle_config_patch_safe",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_sessions.py",
            "opensquilla.gateway.rpc_chat",
            "_handle_chat_history",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_workbench_resources.py",
            "opensquilla.gateway.rpc_artifacts",
            "_session_id_for_key",
        ): 1,
        (
            "src/opensquilla/session/naming.py",
            "opensquilla.gateway.rpc_chat",
            "_effective_compaction_model",
        ): 1,
        (
            "src/opensquilla/session/naming.py",
            "opensquilla.gateway.rpc_chat",
            "_resolve_compaction_provider",
        ): 1,
        (
            "src/opensquilla/session/naming.py",
            "opensquilla.gateway.rpc_sessions",
            "_emit_to_subscribers",
        ): 1,
    }
)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _python_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path, node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        return [node.module] if node.module else []

    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    module_parts = ["opensquilla", *relative.parts]
    if module_parts[-1] == "__init__":
        package_parts = module_parts[:-1]
    else:
        package_parts = module_parts[:-1]
    ascend = node.level - 1
    anchor = package_parts[: len(package_parts) - ascend]
    suffix = node.module.split(".") if node.module else []
    return [".".join([*anchor, *suffix])]


def test_contract_package_remains_a_leaf_module() -> None:
    violations: list[str] = []
    for path in _python_files(CONTRACT_ROOT):
        for node in ast.walk(_tree(path)):
            for module in _imported_modules(path, node):
                if module == "opensquilla" or (
                    module.startswith("opensquilla.")
                    and not module.startswith("opensquilla.contracts")
                ):
                    violations.append(f"{_relative(path)} imports {module}")
    assert violations == []


def _generated_python_wire_consumers() -> set[str]:
    consumers: set[str] = set()
    for path in _python_files(PACKAGE_ROOT):
        # Generated registries are allowed to compose other generated models.
        # The boundary enforced below is between generated code and authored
        # production code, not between files inside the generated package.
        if path.is_relative_to(GENERATED_CONTRACT_ROOT):
            continue
        for node in ast.walk(_tree(path)):
            modules = _imported_modules(path, node)
            if any(
                module.startswith("opensquilla.contracts.generated.")
                and not module.endswith("_metadata")
                for module in modules
            ):
                consumers.add(_relative(path))

    return consumers


def test_generated_python_wire_types_are_adapter_only() -> None:
    consumers = _generated_python_wire_consumers()

    assert consumers == GENERATED_WIRE_IMPORT_ALLOWLIST


def test_generated_registry_stays_inside_the_generated_boundary() -> None:
    registry = GENERATED_CONTRACT_ROOT / "v4" / "gateway_contract_registry.py"
    imported_modules = {
        module for node in ast.walk(_tree(registry)) for module in _imported_modules(registry, node)
    }

    assert any(
        module.startswith("opensquilla.contracts.generated.v4.") for module in imported_modules
    )
    assert _relative(registry) not in _generated_python_wire_consumers()


def test_schema_derived_method_metadata_consumers_are_exact() -> None:
    allowlists = {
        "sessions.list": (
            "opensquilla.contracts.generated.v4.sessions_list_metadata",
            GENERATED_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.resolve": (
            "opensquilla.contracts.generated.v4.sessions_resolve_metadata",
            SESSIONS_RESOLVE_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.search": (
            "opensquilla.contracts.generated.v4.sessions_search_metadata",
            SESSIONS_SEARCH_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.changed": (
            "opensquilla.contracts.generated.v4.sessions_changed_metadata",
            SESSIONS_CHANGED_METADATA_IMPORT_ALLOWLIST,
        ),
        "conversation.events": (
            "opensquilla.contracts.generated.v4.conversation_events_metadata",
            CONVERSATION_EVENTS_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.create": (
            "opensquilla.contracts.generated.v4.sessions_create_metadata",
            SESSIONS_CREATE_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.rename": (
            "opensquilla.contracts.generated.v4.sessions_rename_metadata",
            SESSIONS_RENAME_METADATA_IMPORT_ALLOWLIST,
        ),
        "sessions.delete": (
            "opensquilla.contracts.generated.v4.sessions_delete_metadata",
            SESSIONS_DELETE_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.status": (
            "opensquilla.contracts.generated.v4.goals_status_metadata",
            GOALS_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.set": (
            "opensquilla.contracts.generated.v4.goals_set_metadata",
            GOALS_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.capabilities": (
            "opensquilla.contracts.generated.v4.goals_capabilities_metadata",
            GOALS_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.reattach": (
            "opensquilla.contracts.generated.v4.goals_reattach_metadata",
            GOALS_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.edit": (
            "opensquilla.contracts.generated.v4.goals_edit_metadata",
            GOAL_MUTATION_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.pause": (
            "opensquilla.contracts.generated.v4.goals_pause_metadata",
            GOAL_MUTATION_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.resume": (
            "opensquilla.contracts.generated.v4.goals_resume_metadata",
            GOAL_MUTATION_METADATA_IMPORT_ALLOWLIST,
        ),
        "goals.clear": (
            "opensquilla.contracts.generated.v4.goals_clear_metadata",
            GOAL_MUTATION_METADATA_IMPORT_ALLOWLIST,
        ),
        "plans.setMode": (
            "opensquilla.contracts.generated.v4.plans_set_mode_metadata",
            PLANS_METADATA_IMPORT_ALLOWLIST,
        ),
        "plans.revise": (
            "opensquilla.contracts.generated.v4.plans_revise_metadata",
            PLANS_METADATA_IMPORT_ALLOWLIST,
        ),
        "plans.implement": (
            "opensquilla.contracts.generated.v4.plans_implement_metadata",
            PLANS_METADATA_IMPORT_ALLOWLIST,
        ),
        "plans.cancelRun": (
            "opensquilla.contracts.generated.v4.plans_cancel_run_metadata",
            PLANS_METADATA_IMPORT_ALLOWLIST,
        ),
    }
    for method, (module_name, allowlist) in allowlists.items():
        consumers = {
            _relative(path)
            for path in _python_files(PACKAGE_ROOT)
            for node in ast.walk(_tree(path))
            if module_name in _imported_modules(path, node)
        }
        unexpected = consumers - allowlist
        stale = allowlist - consumers
        assert unexpected == set(), f"unexpected {method} metadata imports: {unexpected}"
        assert stale == set(), f"stale {method} metadata import allowlist: {stale}"


def test_sessions_list_authored_literal_debt_is_exact() -> None:
    actual: Counter[str] = Counter()
    for path in _python_files(PACKAGE_ROOT):
        if path.is_relative_to(GENERATED_CONTRACT_ROOT):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Constant) and node.value == "sessions.list":
                actual[_relative(path)] += 1

    unexpected = actual - SESSIONS_LIST_LITERAL_ALLOWLIST
    stale = SESSIONS_LIST_LITERAL_ALLOWLIST - actual
    assert unexpected == Counter(), f"unexpected sessions.list literals: {unexpected}"
    assert stale == Counter(), f"stale sessions.list literal allowlist: {stale}"


def test_sessions_resolve_authored_literal_debt_is_exact() -> None:
    actual: Counter[str] = Counter()
    for path in _python_files(PACKAGE_ROOT):
        if path.is_relative_to(GENERATED_CONTRACT_ROOT):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Constant) and node.value == "sessions.resolve":
                actual[_relative(path)] += 1

    unexpected = actual - SESSIONS_RESOLVE_LITERAL_ALLOWLIST
    stale = SESSIONS_RESOLVE_LITERAL_ALLOWLIST - actual
    assert unexpected == Counter(), f"unexpected sessions.resolve literals: {unexpected}"
    assert stale == Counter(), f"stale sessions.resolve literal allowlist: {stale}"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["opensquilla", *parts])


def _module_import_graph() -> dict[str, set[str]]:
    paths = _python_files(PACKAGE_ROOT)
    known = {_module_name(path) for path in paths}
    graph: dict[str, set[str]] = {name: set() for name in known}
    for path in paths:
        source = _module_name(path)
        for node in ast.walk(_tree(path)):
            for target in _imported_modules(path, node):
                if target in known:
                    graph[source].add(target)
    return graph


def _reaches(graph: dict[str, set[str]], start: str, target: str) -> bool:
    pending = [start]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False


def test_contract_gateway_adapters_do_not_join_a_gateway_cycle() -> None:
    graph = _module_import_graph()
    resolve_adapter = PACKAGE_ROOT / "gateway" / "adapters" / "sessions_resolve_contract.py"
    goals_adapter = PACKAGE_ROOT / "gateway" / "adapters" / "goals_contract.py"
    sandbox_adapter = PACKAGE_ROOT / "gateway" / "adapters" / "sandbox_runtime_contract.py"
    lifecycle_adapter = (
        PACKAGE_ROOT / "gateway" / "adapters" / "session_lifecycle_contract.py"
    )
    for adapter_path in (
        SESSIONS_LIST_GATEWAY_ADAPTER,
        resolve_adapter,
        goals_adapter,
        sandbox_adapter,
        lifecycle_adapter,
    ):
        adapter = _module_name(adapter_path)
        cycle_edges = sorted(
            dependency for dependency in graph[adapter] if _reaches(graph, dependency, adapter)
        )
        gateway_dependencies = sorted(
            dependency
            for dependency in graph[adapter]
            if dependency.startswith("opensquilla.gateway")
        )
        assert gateway_dependencies == ["opensquilla.gateway.adapters.contract_method"], (
            f"{adapter} may depend only on the generic registration Adapter: {gateway_dependencies}"
        )
        assert cycle_edges == [], f"{adapter} joined a Python import cycle: {cycle_edges}"


def test_sandbox_application_module_is_transport_neutral_and_typed() -> None:
    module_path = PACKAGE_ROOT / "application" / "sandbox_runtime.py"
    tree = _tree(module_path)
    forbidden_import_prefixes = (
        "opensquilla.contracts.generated",
        "opensquilla.gateway",
        "opensquilla.runtime_packs",
        "opensquilla.sandbox",
    )
    forbidden_typing_names = {"Any", "Mapping", "MutableMapping"}
    wire_field_names = {
        "autonomousPaused",
        "componentId",
        "operationId",
        "policyVersion",
        "requiresAdmin",
        "runMode",
        "schemaVersion",
        "sessionKey",
    }

    imported_modules: set[str] = set()
    imported_typing_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            if node.module == "typing":
                imported_typing_names.update(alias.name for alias in node.names)

    forbidden_imports = sorted(
        module
        for module in imported_modules
        if module.startswith(forbidden_import_prefixes)
    )
    leaked_wire_fields = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in wire_field_names
        }
    )
    payload_projectors = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "to_payload"
        }
    )
    production_fakes = sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("InMemory")
    )

    assert forbidden_imports == [], (
        f"sandbox application imports infrastructure: {forbidden_imports}"
    )
    assert imported_typing_names.isdisjoint(forbidden_typing_names), (
        "sandbox application must expose typed DTOs instead of generic JSON bags: "
        f"{sorted(imported_typing_names & forbidden_typing_names)}"
    )
    assert leaked_wire_fields == [], (
        f"sandbox application owns wire field names: {leaked_wire_fields}"
    )
    assert payload_projectors == [], "sandbox application must not project transport payloads"
    assert production_fakes == [], f"test fakes leaked into production: {production_fakes}"


def test_sandbox_runtime_authored_surface_stays_within_large_pr_ceiling() -> None:
    current = _physical_lines(SANDBOX_RUNTIME_AUTHORED_FILES)
    assert current <= SANDBOX_RUNTIME_AUTHORED_LOC_CEILING, (
        f"SandboxRuntime authored seams total {current} lines; split the domain at its "
        f"predefined Module/Port or consumer boundary before exceeding "
        f"{SANDBOX_RUNTIME_AUTHORED_LOC_CEILING}"
    )


def test_session_lifecycle_application_module_is_transport_neutral_and_typed() -> None:
    module_path = PACKAGE_ROOT / "application" / "session_lifecycle.py"
    tree = _tree(module_path)
    forbidden_import_prefixes = (
        "opensquilla.contracts.generated",
        "opensquilla.gateway",
    )
    forbidden_typing_names = {"Any", "Mapping", "MutableMapping"}
    wire_field_names = {
        "agentId",
        "authProfile",
        "beforeMessageId",
        "displayName",
        "forkMode",
        "parentKey",
        "providerOverride",
        "seededMessage",
        "sessionId",
        "throughTurnId",
        "workspaceId",
    }

    imported_modules: set[str] = set()
    imported_typing_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            if node.module == "typing":
                imported_typing_names.update(alias.name for alias in node.names)

    forbidden_imports = sorted(
        module
        for module in imported_modules
        if module.startswith(forbidden_import_prefixes)
    )
    leaked_wire_fields = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in wire_field_names
        }
    )
    payload_projectors = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "to_payload"
        }
    )
    production_fakes = sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("InMemory")
    )

    assert forbidden_imports == [], (
        f"SessionLifecycle application imports infrastructure: {forbidden_imports}"
    )
    assert imported_typing_names.isdisjoint(forbidden_typing_names), (
        "SessionLifecycle application must expose typed DTOs instead of JSON bags: "
        f"{sorted(imported_typing_names & forbidden_typing_names)}"
    )
    assert leaked_wire_fields == [], (
        f"SessionLifecycle application owns wire field names: {leaked_wire_fields}"
    )
    assert payload_projectors == [], (
        "SessionLifecycle application must not project transport payloads"
    )
    assert production_fakes == [], f"test fakes leaked into production: {production_fakes}"


def test_session_lifecycle_authored_surface_stays_within_large_pr_ceiling() -> None:
    current = _physical_lines(SESSION_LIFECYCLE_AUTHORED_FILES)
    assert current <= SESSION_LIFECYCLE_AUTHORED_LOC_CEILING, (
        f"SessionLifecycle authored seams total {current} lines; split the domain at its "
        f"predefined lifecycle/maintenance or Module/consumer boundary before exceeding "
        f"{SESSION_LIFECYCLE_AUTHORED_LOC_CEILING}"
    )


def test_conversation_ancillary_application_does_not_depend_on_gateway() -> None:
    path = PACKAGE_ROOT / "application" / "conversation_ancillary.py"
    tree = _tree(path)
    forbidden_imports: list[str] = []
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("opensquilla.gateway"):
                forbidden_imports.append(module)
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("opensquilla.gateway"):
                    forbidden_imports.append(alias.name)

    assert forbidden_imports == []
    assert "RpcContext" not in imported_names


def test_agent_catalog_application_does_not_depend_on_gateway() -> None:
    path = PACKAGE_ROOT / "application" / "agent_catalog.py"
    tree = _tree(path)
    forbidden_imports: list[str] = []
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("opensquilla.gateway"):
                forbidden_imports.append(module)
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("opensquilla.gateway"):
                    forbidden_imports.append(alias.name)

    assert forbidden_imports == []
    assert "RpcContext" not in imported_names


def test_channel_administration_application_does_not_depend_on_gateway() -> None:
    path = PACKAGE_ROOT / "application" / "channel_administration.py"
    tree = _tree(path)
    forbidden_imports: list[str] = []
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("opensquilla.gateway"):
                forbidden_imports.append(module)
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("opensquilla.gateway"):
                    forbidden_imports.append(alias.name)

    assert forbidden_imports == []
    assert "RpcContext" not in imported_names


def test_cron_scheduler_application_does_not_depend_on_gateway() -> None:
    path = PACKAGE_ROOT / "application" / "cron_scheduler.py"
    tree = _tree(path)
    forbidden_imports: list[str] = []
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("opensquilla.gateway"):
                forbidden_imports.append(module)
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("opensquilla.gateway"):
                    forbidden_imports.append(alias.name)

    assert forbidden_imports == []
    assert "RpcContext" not in imported_names


def test_rpc_context_does_not_grow_past_pinned_main() -> None:
    tree = _tree(RPC_CONTEXT)
    context = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RpcContext"
    )
    fields = [node for node in context.body if isinstance(node, ast.AnnAssign)]
    assert len(fields) <= 33


def _physical_lines(relative_paths: tuple[str, ...]) -> int:
    return sum(
        len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        for relative in relative_paths
        if (ROOT / relative).is_file()
    )


def test_z1_authored_runtime_is_smaller_than_platform_baseline() -> None:
    current = _physical_lines(AUTHORED_RUNTIME_FILES)
    assert current < AUTHORED_RUNTIME_LOC_BASELINE, (
        f"Z1 authored runtime is {current} lines; complete domain migration must remain below "
        f"the reviewed #1525 baseline of {AUTHORED_RUNTIME_LOC_BASELINE}"
    )


def test_z1_webui_legacy_transport_surface_is_closed() -> None:
    legacy_rpc_types = WEBUI_SOURCE_ROOT / "types" / "rpc.ts"
    assert not legacy_rpc_types.exists(), (
        "types/rpc.ts must be deleted after domain ownership closes"
    )

    forbidden_identifiers: dict[str, list[str]] = {}
    raw_store_imports: list[str] = []
    for path in sorted(WEBUI_SOURCE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".vue"}:
            continue
        relative = path.relative_to(WEBUI_SOURCE_ROOT).as_posix()
        if ".test." in path.name or ".spec." in path.name:
            continue
        if relative.startswith("contracts/generated/"):
            continue
        source = path.read_text(encoding="utf-8")
        leaked = [name for name in WEBUI_LEGACY_TRANSPORT_IDENTIFIERS if name in source]
        if leaked:
            forbidden_identifiers[relative] = leaked
        if (
            ("@/stores/rpc" in source or "./stores/rpc" in source or "../stores/rpc" in source)
            and relative not in {"main.ts", "stores/rpc.ts"}
        ):
            raw_store_imports.append(relative)

    assert forbidden_identifiers == {}, (
        "legacy generic transport capabilities must stay deleted from production WebUI: "
        f"{forbidden_identifiers}"
    )
    assert raw_store_imports == [], (
        "useRpcStore is private to the composition root and transport implementation: "
        f"{raw_store_imports}"
    )


def test_f2_transport_foundation_stays_within_explicit_ceiling() -> None:
    current = _physical_lines(F2_TRANSPORT_FOUNDATION_FILES)
    assert current <= F2_TRANSPORT_FOUNDATION_LOC_CEILING, (
        f"F2 authored Transport foundation grew to {current} lines; "
        f"the reviewed ceiling is {F2_TRANSPORT_FOUNDATION_LOC_CEILING}"
    )


def test_f2_gateway_composition_root_stays_declarative() -> None:
    source = (ROOT / F2_GATEWAY_COMPOSITION_ROOT).read_text(encoding="utf-8")
    forbidden = {
        "rpc.call(": "raw RPC calls",
        "rpc.request(": "raw RPC requests",
        "fetch(": "direct HTTP requests",
        "supportsMethod(": "wire capability checks",
        "supportsEvent(": "wire event capability checks",
        "waitForConnection(": "connection lifecycle ownership",
        "markMethodUnavailable(": "wire compatibility state",
    }
    leaked = [label for token, label in forbidden.items() if token in source]
    assert leaked == [], (
        "Gateway Adapter composition root must remain declarative; found "
        + ", ".join(leaked)
    )


def _method_registration_sites() -> list[tuple[str, str, str]]:
    sites: list[tuple[str, str, str]] = []
    for path in _python_files(GATEWAY_ROOT):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "method"
                    and decorator.args
                ):
                    continue
                argument = decorator.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    method = argument.value
                elif isinstance(argument, ast.Name):
                    method = argument.id
                else:
                    method = ast.unparse(argument)
                sites.append((_relative(path), node.name, method))
    return sites


def test_static_rpc_decorator_sites_are_exact_and_contract_methods_are_adapter_registered() -> None:
    sites = _method_registration_sites()
    assert len(sites) == STATIC_RPC_DECORATOR_BASELINE
    assert [site for site in sites if site[2] in {"sessions.list", "SESSIONS_LIST_METHOD"}] == []
    assert [
        site for site in sites if site[2] in {"sessions.resolve", "SESSIONS_RESOLVE_METHOD"}
    ] == []
    assert [
        site
        for site in sites
        if site[2]
        in {
            "sessions.create",
            "sessions.fork",
            "sessions.forkThroughTurn",
            "sessions.rename",
            "sessions.delete",
            "sessions.reset",
            "sessions.contextCompact",
            "sessions.compact",
            "chat.send",
            "chat.abort",
            "sessions.send",
            "sessions.abort",
            "sessions.steer.v2",
            "sessions.steer",
            "sessions.pending_inputs.enqueue",
            "sessions.pending_inputs.list",
            "sessions.pending_inputs.update",
            "sessions.pending_inputs.reorder",
            "sessions.pending_inputs.cancel",
            "sessions.pending_inputs.dispatch",
            "sessions.pending_inputs.steer",
            "usage.status",
            "usage.query",
            "usage.cost",
            "commands.list_for_surface",
            "router.feedback.submit",
            "sessions.promptCacheKeepalive.status",
            "sessions.promptCacheKeepalive.set",
            "chat.clarify_submit",
            "agents.list",
            "agents.create",
            "agents.update",
            "agents.delete",
            "channels.status",
            "channels.get",
            "channels.probe",
            "channels.logout",
            "channels.restart",
            "channels.pairings",
            "channels.pairing.approve",
            "channels.admin.set",
            "channels.pairing.revoke",
            "cron.list",
            "cron.status",
            "cron.add",
            "cron.create",
            "cron.update",
            "cron.remove",
            "cron.run",
            "cron.runs",
            "cron.subscribe",
            "cron.unsubscribe",
            "status",
            "router.selflearning.status",
            "doctor.status",
            "logs.status",
            "logs.tail",
        }
    ] == []
    assert [
        site
        for site in sites
        if site[2]
        in {
            "goals.status",
            "goals.set",
            "goals.capabilities",
            "goals.reattach",
            "goals.edit",
            "goals.pause",
            "goals.resume",
            "goals.clear",
            "GOALS_STATUS_METHOD",
            "GOALS_SET_METHOD",
            "GOALS_CAPABILITIES_METHOD",
            "GOALS_REATTACH_METHOD",
            "GOALS_EDIT_METHOD",
            "GOALS_PAUSE_METHOD",
            "GOALS_RESUME_METHOD",
            "GOALS_CLEAR_METHOD",
        }
    ] == []
    assert [
        site
        for site in sites
        if site[2]
        in {
            "chat.history",
            "sessions.messages.subscribe",
            "sessions.messages.hydrate",
            "sessions.messages.snapshot",
            "sessions.messages.unsubscribe",
            "sessions.preview",
        }
    ] == []
    assert [
        site
        for site in sites
        if site[2]
        in {
            "sandbox.setup.status",
            "sandbox.setup.ensure",
            "sandbox.capability.status",
            "sandbox.policy.get",
            "sandbox.policy.defaults",
            "sandbox.policy.update",
            "sandbox.run_mode.preference.get",
            "sandbox.run_mode.preference.set",
            "sandbox.runtime.status",
            "sandbox.runtime.install",
            "sandbox.runtime.cancel",
            "sandbox.runtime.remove",
            "sandbox.runtime.discard_download",
            "sandbox.resume",
        }
    ] == []
    from opensquilla.gateway.adapters.artifact_workbench_contract import (
        ARTIFACT_WORKBENCH_CONTRACT_METHODS,
    )

    assert [
        site for site in sites if site[2] in ARTIFACT_WORKBENCH_CONTRACT_METHODS
    ] == []


def test_runtime_rpc_surface_is_exact_and_contract_methods_use_generic_adapter() -> None:
    from opensquilla.contracts.generated.v4.sessions_list_metadata import (
        SESSIONS_LIST_METHOD,
        SESSIONS_LIST_SCOPE,
    )
    from opensquilla.gateway.rpc import get_dispatcher

    registry = get_dispatcher()
    methods = registry.list_methods()
    assert len(methods) == RUNTIME_RPC_METHOD_BASELINE
    assert len(methods) == len(set(methods))

    entry = registry.get_entry(SESSIONS_LIST_METHOD)
    assert entry is not None
    assert entry.name == SESSIONS_LIST_METHOD
    assert entry.required_scope == SESSIONS_LIST_SCOPE
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.contracts.generated.v4.sessions_resolve_metadata import (
        SESSIONS_RESOLVE_METHOD,
        SESSIONS_RESOLVE_SCOPE,
    )

    entry = registry.get_entry(SESSIONS_RESOLVE_METHOD)
    assert entry is not None
    assert entry.name == SESSIONS_RESOLVE_METHOD
    assert entry.required_scope == SESSIONS_RESOLVE_SCOPE
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.contracts.generated.v4.chat_history_metadata import (
        CHAT_HISTORY_METHOD,
        CHAT_HISTORY_SCOPE,
    )
    from opensquilla.contracts.generated.v4.sessions_messages_hydrate_metadata import (
        SESSIONS_MESSAGES_HYDRATE_METHOD,
        SESSIONS_MESSAGES_HYDRATE_SCOPE,
    )
    from opensquilla.contracts.generated.v4.sessions_messages_snapshot_metadata import (
        SESSIONS_MESSAGES_SNAPSHOT_METHOD,
        SESSIONS_MESSAGES_SNAPSHOT_SCOPE,
    )
    from opensquilla.contracts.generated.v4.sessions_messages_subscribe_metadata import (
        SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
        SESSIONS_MESSAGES_SUBSCRIBE_SCOPE,
    )
    from opensquilla.contracts.generated.v4.sessions_messages_unsubscribe_metadata import (
        SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD,
        SESSIONS_MESSAGES_UNSUBSCRIBE_SCOPE,
    )
    from opensquilla.contracts.generated.v4.sessions_preview_metadata import (
        SESSIONS_PREVIEW_METHOD,
        SESSIONS_PREVIEW_SCOPE,
    )

    for method, scope in (
        (CHAT_HISTORY_METHOD, CHAT_HISTORY_SCOPE),
        (SESSIONS_MESSAGES_SUBSCRIBE_METHOD, SESSIONS_MESSAGES_SUBSCRIBE_SCOPE),
        (SESSIONS_MESSAGES_HYDRATE_METHOD, SESSIONS_MESSAGES_HYDRATE_SCOPE),
        (SESSIONS_MESSAGES_SNAPSHOT_METHOD, SESSIONS_MESSAGES_SNAPSHOT_SCOPE),
        (SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD, SESSIONS_MESSAGES_UNSUBSCRIBE_SCOPE),
        (SESSIONS_PREVIEW_METHOD, SESSIONS_PREVIEW_SCOPE),
    ):
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.name == method
        assert entry.required_scope == scope
        assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
        assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.contracts.generated.v4.goals_capabilities_metadata import (
        GOALS_CAPABILITIES_METHOD,
        GOALS_CAPABILITIES_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_clear_metadata import (
        GOALS_CLEAR_METHOD,
        GOALS_CLEAR_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_edit_metadata import (
        GOALS_EDIT_METHOD,
        GOALS_EDIT_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_pause_metadata import (
        GOALS_PAUSE_METHOD,
        GOALS_PAUSE_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_reattach_metadata import (
        GOALS_REATTACH_METHOD,
        GOALS_REATTACH_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_resume_metadata import (
        GOALS_RESUME_METHOD,
        GOALS_RESUME_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_set_metadata import (
        GOALS_SET_METHOD,
        GOALS_SET_SCOPE,
    )
    from opensquilla.contracts.generated.v4.goals_status_metadata import (
        GOALS_STATUS_METHOD,
        GOALS_STATUS_SCOPE,
    )

    for method, scope in (
        (GOALS_STATUS_METHOD, GOALS_STATUS_SCOPE),
        (GOALS_SET_METHOD, GOALS_SET_SCOPE),
        (GOALS_CAPABILITIES_METHOD, GOALS_CAPABILITIES_SCOPE),
        (GOALS_REATTACH_METHOD, GOALS_REATTACH_SCOPE),
        (GOALS_EDIT_METHOD, GOALS_EDIT_SCOPE),
        (GOALS_PAUSE_METHOD, GOALS_PAUSE_SCOPE),
        (GOALS_RESUME_METHOD, GOALS_RESUME_SCOPE),
        (GOALS_CLEAR_METHOD, GOALS_CLEAR_SCOPE),
    ):
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.name == method
        assert entry.required_scope == scope
        assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
        assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.contracts.generated.v4.plans_cancel_run_metadata import (
        PLANS_CANCEL_RUN_METHOD,
        PLANS_CANCEL_RUN_SCOPE,
    )
    from opensquilla.contracts.generated.v4.plans_implement_metadata import (
        PLANS_IMPLEMENT_METHOD,
        PLANS_IMPLEMENT_SCOPE,
    )
    from opensquilla.contracts.generated.v4.plans_revise_metadata import (
        PLANS_REVISE_METHOD,
        PLANS_REVISE_SCOPE,
    )
    from opensquilla.contracts.generated.v4.plans_set_mode_metadata import (
        PLANS_SET_MODE_METHOD,
        PLANS_SET_MODE_SCOPE,
    )

    for method, scope in (
        (PLANS_SET_MODE_METHOD, PLANS_SET_MODE_SCOPE),
        (PLANS_REVISE_METHOD, PLANS_REVISE_SCOPE),
        (PLANS_IMPLEMENT_METHOD, PLANS_IMPLEMENT_SCOPE),
        (PLANS_CANCEL_RUN_METHOD, PLANS_CANCEL_RUN_SCOPE),
    ):
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.name == method
        assert entry.required_scope == scope
        assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
        assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.contracts.generated.v4.gateway_contract_registry import (
        GATEWAY_METHOD_CONTRACTS,
    )
    from opensquilla.gateway.adapters.sandbox_runtime_contract import (
        SANDBOX_RUNTIME_CONTRACT_METHODS,
    )

    for method in SANDBOX_RUNTIME_CONTRACT_METHODS:
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.name == method
        assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
        assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
        assert entry.handler.__name__ == "handle_contract_method"

    from opensquilla.gateway.adapters.agent_catalog_contract import (
        AGENT_CATALOG_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.artifact_workbench_contract import (
        ARTIFACT_WORKBENCH_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.channel_administration_contract import (
        CHANNEL_ADMINISTRATION_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.conversation_ancillary_contract import (
        CONVERSATION_ANCILLARY_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.cron_scheduler_contract import (
        CRON_SCHEDULER_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.observability_contract import (
        OBSERVABILITY_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.pending_input_queue_contract import (
        PENDING_INPUT_QUEUE_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.session_lifecycle_contract import (
        SESSION_LIFECYCLE_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.session_maintenance_contract import (
        SESSION_MAINTENANCE_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.skill_catalog_contract import (
        SKILL_CATALOG_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.skill_management_contract import (
        SKILL_MANAGEMENT_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.skill_proposal_review_contract import (
        SKILL_PROPOSAL_REVIEW_CONTRACT_METHODS,
    )
    from opensquilla.gateway.adapters.turn_admission_contract import (
        TURN_ADMISSION_CONTRACT_METHODS,
    )

    for method in (
        *SESSION_LIFECYCLE_CONTRACT_METHODS,
        *SESSION_MAINTENANCE_CONTRACT_METHODS,
        *TURN_ADMISSION_CONTRACT_METHODS,
        *PENDING_INPUT_QUEUE_CONTRACT_METHODS,
        *CONVERSATION_ANCILLARY_CONTRACT_METHODS,
        *AGENT_CATALOG_CONTRACT_METHODS,
        *CHANNEL_ADMINISTRATION_CONTRACT_METHODS,
        *CRON_SCHEDULER_CONTRACT_METHODS,
        *OBSERVABILITY_CONTRACT_METHODS,
        *SKILL_CATALOG_CONTRACT_METHODS,
        *SKILL_MANAGEMENT_CONTRACT_METHODS,
        *SKILL_PROPOSAL_REVIEW_CONTRACT_METHODS,
        *ARTIFACT_WORKBENCH_CONTRACT_METHODS,
    ):
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.name == method
        assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
        assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
        assert entry.handler.__name__ == "handle_contract_method"


def test_cross_rpc_private_import_debt_is_exact() -> None:
    actual: Counter[tuple[str, str, str]] = Counter()
    for path in _python_files(PACKAGE_ROOT):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("opensquilla.gateway.rpc_"):
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    actual[(_relative(path), node.module, alias.name)] += 1

    unexpected = actual - APPROVED_PRIVATE_RPC_IMPORTS
    stale = APPROVED_PRIVATE_RPC_IMPORTS - actual
    assert unexpected == Counter(), f"unexpected private RPC imports: {unexpected}"
    assert stale == Counter(), f"stale private RPC import allowlist: {stale}"


def test_r3_application_modules_do_not_depend_on_gateway_context() -> None:
    violations: list[str] = []
    for relative in R3_APPLICATION_MODULE_FILES:
        path = ROOT / relative
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "opensquilla.gateway"
            ):
                violations.append(f"{relative}:{node.lineno}: imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("opensquilla.gateway"):
                        violations.append(f"{relative}:{node.lineno}: imports {alias.name}")
            elif isinstance(node, ast.Name) and node.id == "RpcContext":
                violations.append(f"{relative}:{node.lineno}: references RpcContext")

    assert violations == [], "R3 Application Modules crossed the Gateway seam:\n" + "\n".join(
        violations
    )
