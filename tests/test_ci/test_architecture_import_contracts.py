"""Architecture import-contract regression tests."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "opensquilla"

APPROVED_PACKAGE_IMPORTS: frozenset[tuple[str, str]] = frozenset({
    ("agents", "gateway"),
    ("agents", "identity"),
    ("agents", "onboarding"),
    ("agents", "session"),
    # Format-specific delivery validation reuses canonical attachment MIME and
    # container signatures; contracts remains implementation-free.
    ("artifact_validation.py", "contracts"),
    ("channels", "engine"),
    ("channels", "contracts"),
    ("channels", "gateway"),
    ("channels", "session"),
    ("channels", "tools"),
    ("cli", "agents"),
    ("cli", "contracts"),
    # opensquilla swebench CLI drives the contrib SWE-bench harness (lazy import).
    ("cli", "contrib"),
    ("cli", "dist"),
    ("cli", "engine"),
    ("cli", "eval"),
    ("cli", "gateway"),
    ("cli", "health"),
    ("cli", "memory"),
    ("cli", "mcp_server"),
    ("cli", "migration"),
    ("cli", "observability"),
    ("cli", "onboarding"),
    ("cli", "persistence"),
    # CLI maintenance commands attach the same typed provider-correlation
    # envelope as the shared turn loop; provider remains a lower-level leaf.
    ("cli", "provider"),
    # The root CLI exposes the offline recovery adapter and writer entrypoints
    # acquire recovery locks; recovery never imports the CLI back.
    ("cli", "recovery"),
    ("cli", "sandbox"),
    ("cli", "search"),
    ("cli", "session"),
    ("cli", "skills"),
    ("cli", "tools"),
    ("cli", "uninstall"),
    # code-task assembles the subagent's per-run config from the operator's
    # own provider sections and validates it against the gateway config
    # schema before spawning (lazy import; gateway never imports contrib, so
    # no cycle).
    ("contrib", "gateway"),
    # code-task's credential preflight reuses the onboarding provider probe and
    # the provider failure taxonomy / registry to classify results; neither
    # onboarding nor provider imports contrib, so no cycle.
    ("contrib", "onboarding"),
    ("contrib", "provider"),
    # The diagnostics-bundle shim composes gateway redaction, the offline
    # doctor, and onboarding config resolution lazily for the bundle
    # generator; a top-level module (permissions.py precedent) so the
    # low-level observability package never imports upper layers itself.
    ("diagnostics_sources.py", "cli"),
    ("diagnostics_sources.py", "gateway"),
    ("diagnostics_sources.py", "onboarding"),
    ("engine", "agents"),
    ("engine", "channels"),
    ("engine", "contracts"),
    ("engine", "gateway"),
    ("engine", "identity"),
    ("engine", "memory"),
    ("engine", "observability"),
    ("engine", "persistence"),
    ("engine", "plugins"),
    ("engine", "provider"),
    ("engine", "safety"),
    ("engine", "sandbox"),
    ("engine", "session"),
    ("engine", "skills"),
    ("engine", "squilla_router"),
    ("engine", "tools"),
    # The measurement-only eval harness observes providers (and pricing) through
    # their public surface; nothing imports eval back, so it joins no cycle.
    ("eval", "engine"),
    ("eval", "provider"),
    ("gateway", "agents"),
    ("gateway", "application"),
    ("gateway", "chat"),
    ("gateway", "channels"),
    ("gateway", "contracts"),
    ("gateway", "engine"),
    ("gateway", "health"),
    ("gateway", "identity"),
    ("gateway", "mcp"),
    ("gateway", "memory"),
    # Gateway PID compatibility delegates to the shared recovery lock protocol.
    ("gateway", "recovery"),
    ("gateway", "observability"),
    ("gateway", "onboarding"),
    ("gateway", "persistence"),
    ("gateway", "provider"),
    ("gateway", "sandbox"),
    # Browser-facing approval projections reuse the canonical secret redactor;
    # safety is a lower-level leaf and does not import gateway back.
    ("gateway", "safety"),
    ("gateway", "scheduler"),
    ("gateway", "search"),
    ("gateway", "session"),
    ("gateway", "skills"),
    # Gateway's post-dream hook drives the opt-in router self-learning
    # orchestrator (offline retrain; default-off, fail-open).
    ("gateway", "squilla_router"),
    ("gateway", "tools"),
    ("identity", "safety"),
    ("identity", "session"),
    ("mcp", "tools"),
    ("memory", "agents"),
    ("memory", "compat"),
    ("memory", "engine"),
    ("memory", "gateway"),
    ("memory", "identity"),
    ("memory", "provider"),
    ("memory", "safety"),
    ("memory", "session"),
    ("memory", "tools"),
    ("migration", "gateway"),
    # Full-profile import publishes through the shared no-replace recovery
    # transaction layer; recovery does not import migration back.
    ("migration", "recovery"),
    ("migration", "onboarding"),
    ("onboarding", "channels"),
    ("onboarding", "gateway"),
    ("onboarding", "provider"),
    ("onboarding", "search"),
    # Runtime writers acquire the hardened profile-operation lock through a
    # narrow top-level facade. Recovery owns the platform-specific mechanics;
    # lower-level packages must not import the recovery package directly.
    ("profile_operation_lock.py", "recovery"),
    ("permissions.py", "sandbox"),
    # turn_error_writer scrubs free-text error records through the low-level
    # observability.redact utility before insert — sound downward layering.
    ("persistence", "observability"),
    ("persistence", "skills"),
    ("provider", "engine"),
    ("provider", "safety"),
    # Provider argument repair reuses the tool alias/schema helpers (lazy import).
    ("provider", "tools"),
    ("result_budget.py", "search"),
    # Trusted web-tool failure parsing reuses the canonical search query
    # normalizer; the helper stays top-level so execution_status remains
    # independent of package import side effects.
    ("search_tool_outcome.py", "search"),
    ("router_control.py", "engine"),
    ("sandbox", "application"),
    ("sandbox", "gateway"),
    ("sandbox", "safety"),
    ("sandbox", "tools"),
    ("scheduler", "agents"),
    ("scheduler", "channels"),
    ("scheduler", "compat"),
    ("scheduler", "engine"),
    ("scheduler", "gateway"),
    ("scheduler", "session"),
    ("scheduler", "skills"),
    ("scheduler", "tools"),
    # Self-learning's opt-in audit sidecar reuses the decision-log redactor;
    # observability is a leaf package, so this closes no cycle.
    ("squilla_router", "observability"),
    ("session", "compat"),
    ("session", "engine"),
    ("session", "gateway"),
    ("session", "memory"),
    ("session", "persistence"),
    ("session", "provider"),
    ("session", "tools"),
    ("skills", "engine"),
    ("skills", "gateway"),
    ("skills", "memory"),
    ("skills", "observability"),
    ("skills", "persistence"),
    ("skills", "provider"),
    ("skills", "safety"),
    ("skills", "tools"),
    ("tools", "agents"),
    ("tools", "channels"),
    ("tools", "engine"),
    ("tools", "gateway"),
    ("tools", "identity"),
    ("tools", "memory"),
    ("tools", "provider"),
    ("tools", "safety"),
    ("tools", "sandbox"),
    ("tools", "scheduler"),
    ("tools", "search"),
    ("tools", "session"),
    ("tools", "skills"),
    ("uninstall", "gateway"),
})

APPROVED_CYCLIC_PACKAGES: frozenset[str] = frozenset({
    "agents",
    "channels",
    "engine",
    "gateway",
    "identity",
    "mcp",
    "memory",
    "onboarding",
    "persistence",
    "provider",
    "sandbox",
    "scheduler",
    "session",
    "skills",
    "tools",
})


def _top_level_packages() -> set[str]:
    return {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    }


def _resolve_relative_import(file_path: Path, node: ast.ImportFrom) -> list[str]:
    rel_path = file_path.relative_to(PACKAGE_ROOT)
    package_parts = ("opensquilla", *rel_path.parent.parts)
    if node.level > len(package_parts):
        return []

    base_parts = package_parts[: len(package_parts) - node.level + 1]
    module_parts = tuple(node.module.split(".")) if node.module else ()
    resolved = ".".join((*base_parts, *module_parts))
    if resolved == "opensquilla":
        return [f"opensquilla.{alias.name}" for alias in node.names if alias.name != "*"]
    return [resolved]


def _module_imports(tree: ast.AST, file_path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                modules.extend(_resolve_relative_import(file_path, node))
            elif node.module:
                modules.append(node.module)
    return modules


def _package_import_edges() -> set[tuple[str, str]]:
    packages = _top_level_packages()
    edges: set[tuple[str, str]] = set()
    for file_path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in file_path.parts:
            continue
        source_pkg = file_path.relative_to(PACKAGE_ROOT).parts[0]
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for module in _module_imports(tree, file_path):
            if not module.startswith("opensquilla."):
                continue
            parts = module.split(".")
            if len(parts) < 2:
                continue
            target_pkg = parts[1]
            if target_pkg in packages and target_pkg != source_pkg:
                edges.add((source_pkg, target_pkg))
    return edges


def _strongly_connected_components(
    edges: set[tuple[str, str]], packages: set[str]
) -> list[frozenset[str]]:
    adjacency: dict[str, set[str]] = {package: set() for package in packages}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set())

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[frozenset[str]] = []

    def visit(package: str) -> None:
        nonlocal index
        indexes[package] = index
        lowlinks[package] = index
        index += 1
        stack.append(package)
        on_stack.add(package)

        for target in adjacency.get(package, set()):
            if target not in indexes:
                visit(target)
                lowlinks[package] = min(lowlinks[package], lowlinks[target])
            elif target in on_stack:
                lowlinks[package] = min(lowlinks[package], indexes[target])

        if lowlinks[package] == indexes[package]:
            component: set[str] = set()
            while True:
                target = stack.pop()
                on_stack.remove(target)
                component.add(target)
                if target == package:
                    break
            components.append(frozenset(component))

    for package in sorted(adjacency):
        if package not in indexes:
            visit(package)
    return components


def test_package_imports_do_not_add_new_edges() -> None:
    """New top-level package imports must update the architecture contract deliberately."""
    actual_edges = _package_import_edges()
    unexpected = actual_edges - APPROVED_PACKAGE_IMPORTS
    assert not unexpected, "Unexpected package import edges: " + ", ".join(
        f"{source}->{target}" for source, target in sorted(unexpected)
    )


def test_relative_imports_are_resolved_for_edge_detection() -> None:
    tree = ast.parse("from ..gateway.routing import build_channel_route_envelope\n")
    fake_file = PACKAGE_ROOT / "scheduler" / "handlers.py"

    assert "opensquilla.gateway.routing" in _module_imports(tree, fake_file)


def test_new_packages_do_not_join_existing_circular_dependency_baseline() -> None:
    """The known cyclic package set is a shrink target, not an expansion point."""
    actual_edges = _package_import_edges()
    cyclic_packages = frozenset(
        package
        for component in _strongly_connected_components(actual_edges, _top_level_packages())
        if len(component) > 1
        for package in component
    )
    unexpected = cyclic_packages - APPROVED_CYCLIC_PACKAGES
    assert not unexpected, "Packages unexpectedly joined import cycles: " + ", ".join(
        sorted(unexpected)
    )


def test_contracts_package_stays_implementation_free() -> None:
    actual_edges = _package_import_edges()
    implementation_edges = {
        target for source, target in actual_edges if source == "contracts"
    }

    assert not implementation_edges, (
        "contracts must not import implementation packages: "
        + ", ".join(sorted(implementation_edges))
    )
