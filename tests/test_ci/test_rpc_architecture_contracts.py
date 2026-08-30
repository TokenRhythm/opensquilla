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
SESSIONS_LIST_GATEWAY_ADAPTER = (
    PACKAGE_ROOT / "gateway" / "adapters" / "sessions_list_contract.py"
)
RUNTIME_RPC_METHOD_BASELINE = 306
STATIC_RPC_DECORATOR_BASELINE = 296

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

F2_FOUNDATION_RUNTIME_FILES = (
    "opensquilla-webui/src/adapters/gateway/gatewayAdapters.ts",
    "opensquilla-webui/src/adapters/gateway/privateHttpTransport.ts",
    "opensquilla-webui/src/adapters/gateway/privateTransports.ts",
    "src/opensquilla/gateway/adapters/contract_method.py",
)
# F2 adds three explicitly reviewed HTTP hardening slices on top of the
# initial 849-line foundation: 58 lines for body lifecycle ownership, 103
# lines for filename/method/body validation (less 9 lines from native brand
# tightening), 135 lines for cancellation-safe response-body ownership, and
# 3 lines for hostile request-option normalization and 3 lines for endpoint
# input normalization.
# Keep the allowance explicit so later domain slices cannot hide authored
# growth behind this infrastructure exception.  Session-directory changes and
# lifecycle now each register one reviewed domain Adapter in the composition
# root; the 12-line increase is the deliberate cumulative seam cost for those
# two slices. Session routing adds one more adapter registration and its typed
# composition-root seam (4 lines); keep this allowance explicit rather than
# turning the foundation exception into an open-ended budget.
F2_FOUNDATION_RUNTIME_LOC_CEILING = 1_158

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
            "src/opensquilla/diagnostics_sources.py",
            "opensquilla.gateway.rpc_logs",
            "_build_logs_status",
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
            "src/opensquilla/gateway/rpc_channels.py",
            "opensquilla.gateway.rpc_config",
            "_persist_config",
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
            "src/opensquilla/gateway/rpc_doctor.py",
            "opensquilla.gateway.rpc_channels",
            "_handle_channels_status",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_doctor.py",
            "opensquilla.gateway.rpc_logs",
            "_build_logs_status",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_doctor.py",
            "opensquilla.gateway.rpc_system",
            "_handle_doctor_memory_status",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_doctor.py",
            "opensquilla.gateway.rpc_tools",
            "_handle_providers_status",
        ): 1,
        (
            "src/opensquilla/gateway/rpc_doctor.py",
            "opensquilla.gateway.rpc_tools",
            "_handle_search_status",
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
            "src/opensquilla/gateway/rpc_system.py",
            "opensquilla.gateway.rpc_config",
            "_persist_config",
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
        module
        for node in ast.walk(_tree(registry))
        for module in _imported_modules(registry, node)
    }

    assert any(
        module.startswith("opensquilla.contracts.generated.v4.")
        for module in imported_modules
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
        assert unexpected == set(), (
            f"unexpected {method} metadata imports: {unexpected}"
        )
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
    for adapter_path in (SESSIONS_LIST_GATEWAY_ADAPTER, resolve_adapter):
        adapter = _module_name(adapter_path)
        cycle_edges = sorted(
            dependency
            for dependency in graph[adapter]
            if _reaches(graph, dependency, adapter)
        )
        gateway_dependencies = sorted(
            dependency
            for dependency in graph[adapter]
            if dependency.startswith("opensquilla.gateway")
        )
        assert gateway_dependencies == [
            "opensquilla.gateway.adapters.contract_method"
        ], (
            f"{adapter} may depend only on the generic registration Adapter: "
            f"{gateway_dependencies}"
        )
        assert cycle_edges == [], f"{adapter} joined a Python import cycle: {cycle_edges}"


def test_rpc_context_does_not_grow_past_pinned_main() -> None:
    tree = _tree(RPC_CONTEXT)
    context = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RpcContext"
    )
    fields = [node for node in context.body if isinstance(node, ast.AnnAssign)]
    assert len(fields) <= 33


def _physical_lines(relative_paths: tuple[str, ...]) -> int:
    return sum(
        len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        for relative in relative_paths
    )


def test_f2_foundation_runtime_stays_within_explicit_ceiling() -> None:
    current = _physical_lines(F2_FOUNDATION_RUNTIME_FILES)
    assert current <= F2_FOUNDATION_RUNTIME_LOC_CEILING, (
        f"F2 authored foundation runtime grew to {current} lines; "
        f"the reviewed ceiling is {F2_FOUNDATION_RUNTIME_LOC_CEILING}"
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
    assert [
        site
        for site in sites
        if site[2] in {"sessions.list", "SESSIONS_LIST_METHOD"}
    ] == []
    assert [
        site
        for site in sites
        if site[2] in {"sessions.resolve", "SESSIONS_RESOLVE_METHOD"}
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
