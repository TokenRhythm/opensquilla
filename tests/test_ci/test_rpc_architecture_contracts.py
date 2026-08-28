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
    }
)
GENERATED_METADATA_IMPORT_ALLOWLIST = frozenset(
    {
        "src/opensquilla/contracts/adapters/sessions_list_contract.py",
        "src/opensquilla/engine/commands.py",
        "src/opensquilla/gateway/adapters/sessions_list_contract.py",
        "src/opensquilla/gateway/app.py",
        "src/opensquilla/gateway/guest_rpc_policy.py",
        "src/opensquilla/gateway/rpc_system.py",
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
SESSIONS_LIST_GATEWAY_ADAPTER = (
    PACKAGE_ROOT / "gateway" / "adapters" / "sessions_list_contract.py"
)
RUNTIME_RPC_METHOD_BASELINE = 306
STATIC_RPC_DECORATOR_BASELINE = 298

# Physical lines in the same runtime files at 5440fd7a. Contract sources,
# generators, fixtures, tests and generated artifacts are reported separately;
# this guard prevents the production seam from becoming a net wrapper layer.
AUTHORED_RUNTIME_LOC_BASELINE = 25_539
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
    "opensquilla-webui/src/modules/sessionDirectory.ts",
    "opensquilla-webui/src/modules/sessionRunStatus.ts",
    "src/opensquilla/cli/gateway_client.py",
    "src/opensquilla/contracts/adapters/sessions_list_contract.py",
    "src/opensquilla/engine/commands.py",
    "src/opensquilla/gateway/adapters/sessions_list_contract.py",
    "src/opensquilla/gateway/app.py",
    "src/opensquilla/gateway/guest_rpc_policy.py",
    "src/opensquilla/gateway/rpc_sessions.py",
    "src/opensquilla/gateway/rpc_system.py",
    "src/opensquilla/gateway/scopes.py",
    "src/opensquilla/gateway_client.py",
)

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
    consumers: set[str] = set()
    for path in _python_files(PACKAGE_ROOT):
        for node in ast.walk(_tree(path)):
            if any(
                module
                == "opensquilla.contracts.generated.v4.sessions_list_metadata"
                for module in _imported_modules(path, node)
            ):
                consumers.add(_relative(path))

    unexpected = consumers - GENERATED_METADATA_IMPORT_ALLOWLIST
    stale = GENERATED_METADATA_IMPORT_ALLOWLIST - consumers
    assert unexpected == set(), f"unexpected sessions.list metadata imports: {unexpected}"
    assert stale == set(), f"stale sessions.list metadata import allowlist: {stale}"


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


def test_sessions_list_gateway_adapter_does_not_join_a_gateway_cycle() -> None:
    graph = _module_import_graph()
    adapter = _module_name(SESSIONS_LIST_GATEWAY_ADAPTER)
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
        "sessions.list Gateway Adapter may depend only on the generic "
        f"registration Adapter: {gateway_dependencies}"
    )
    assert cycle_edges == [], (
        "sessions.list Gateway Adapter joined a Python import cycle: "
        f"{cycle_edges}"
    )


def test_rpc_context_does_not_grow_past_pinned_main() -> None:
    tree = _tree(RPC_CONTEXT)
    context = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RpcContext"
    )
    fields = [node for node in context.body if isinstance(node, ast.AnnAssign)]
    assert len(fields) <= 33


def test_authored_runtime_slice_is_smaller_than_pinned_main() -> None:
    current = sum(
        len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        for relative in AUTHORED_RUNTIME_FILES
    )
    assert current < AUTHORED_RUNTIME_LOC_BASELINE, (
        f"sessions.list authored runtime grew to {current} lines; "
        f"pinned main had {AUTHORED_RUNTIME_LOC_BASELINE}"
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


def test_static_rpc_decorator_sites_are_exact_and_sessions_list_is_generic_registered() -> None:
    sites = _method_registration_sites()
    assert len(sites) == STATIC_RPC_DECORATOR_BASELINE
    assert [
        site
        for site in sites
        if site[2] in {"sessions.list", "SESSIONS_LIST_METHOD"}
    ] == []


def test_runtime_rpc_surface_is_exact_and_sessions_list_uses_contract_adapter() -> None:
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
