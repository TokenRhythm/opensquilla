"""Backend contracts and runtime for OpenSquilla OpenTUI surfaces.

Module topology
---------------

Canonical implementations live in the subpackages:

- ``backend/`` — surface-agnostic runtime, contracts, state, and domain events.
- ``adapters/`` — chat/REPL adapters that bridge surfaces onto the backend.
- ``native/`` and ``opentui/`` — the two renderer surfaces.
- ``plugins/`` and ``renderers/`` — plugin projections and backend selection.

Most top-level modules in this package (and their same-named twins in
``opensquilla.cli.repl``) are frozen compatibility aliases: short shims that
replace themselves in ``sys.modules`` with a canonical module. Two aliases
rename their target (``slash_adapter`` -> ``adapters.slash_gateway`` and
``standalone_slash_adapter`` -> ``adapters.slash_standalone``); the rest keep
the target's name. Only ``turn_bridge`` and ``standalone_runtime`` are real
modules at this level. New code should import the canonical ``adapters``/
``backend`` paths directly; aliases whose importers are all gone get removed.
"""

__all__ = [
    "adapters",
    "backend",
    "chat_cmd_exports",
    "chat_compat",
    "commands",
    "contracts",
    "input_bridge",
    "launch_bridge",
    "native",
    "opentui",
    "output_binding",
    "plugins",
    "renderers",
    "source_checkout",
    "runtime_bridge",
    "slash_adapter",
    "slash_bridge",
    "slash_policy",
    "standalone_runtime",
    "standalone_slash_adapter",
    "state",
    "turn_bridge",
    "turn_stream_defaults",
]
