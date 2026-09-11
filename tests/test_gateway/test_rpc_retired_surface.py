"""The retired Gateway RPC surface stays removed."""

from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from opensquilla.gateway.rpc import get_dispatcher

RETIRED_RPC_METHODS = frozenset(
    {
        "agent",
        "agent.wait",
        "send",
        "wake",
        "system-event",
        "system-presence",
        "secrets.reload",
        "secrets.resolve",
        "documents.editSessions.start",
        "documents.editSessions.heartbeat",
        "documents.editSessions.close",
        "artifacts.prompt_annotations.create",
        "artifacts.prompt_annotations.focus",
        "artifacts.prompt_annotations.update",
        "artifacts.prompt_annotations.discard",
        "artifacts.source.patch",
        "exec.approval.forget",
        "sessions.compact",
        "sessions.steer",
        "cron.add",
    }
)

CANONICAL_REPLACEMENTS = frozenset(
    {
        "sessions.contextCompact",
        "sessions.steer.v2",
        "cron.create",
    }
)


def test_retired_rpc_methods_are_absent_and_replacements_remain() -> None:
    registered = set(get_dispatcher().methods())

    assert RETIRED_RPC_METHODS.isdisjoint(registered)
    assert CANONICAL_REPLACEMENTS <= registered
    assert {"sessions.compact", "sessions.steer", "cron.add"}.isdisjoint(
        GATEWAY_METHOD_CONTRACTS
    )
    assert CANONICAL_REPLACEMENTS <= GATEWAY_METHOD_CONTRACTS.keys()
