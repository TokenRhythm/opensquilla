"""Pure scope identity for the explicitly versioned WebChat ingress."""

RESOLVABLE_SCOPE_SUFFIX = ":resolvable.v1"


def resolvable_webchat_scope(role: str) -> str:
    # Legacy source hints always end in the authenticated role. Appending the
    # protocol after that role prevents an ignored opt-in field from opting in.
    return f"web:webchat:{role}{RESOLVABLE_SCOPE_SUFFIX}"


def is_resolvable_turn_scope(scope: str) -> bool:
    return scope.startswith("web:webchat:") and scope.endswith(RESOLVABLE_SCOPE_SUFFIX)
