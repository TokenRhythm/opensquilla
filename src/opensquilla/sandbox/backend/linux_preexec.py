"""Linux child pre-exec hooks shared by process entry points."""

from __future__ import annotations

from collections.abc import Callable

from opensquilla.sandbox.backend.linux_limits import resource_preexec_from_policy
from opensquilla.sandbox.backend.linux_seccomp import network_seccomp_preexec_from_policy


def process_preexec_from_policy(policy: dict[str, object]) -> Callable[[], None] | None:
    hooks = [
        hook
        for hook in (
            resource_preexec_from_policy(policy),
            network_seccomp_preexec_from_policy(policy),
        )
        if hook is not None
    ]
    if not hooks:
        return None

    def apply_hooks() -> None:
        for hook in hooks:
            hook()

    return apply_hooks


__all__ = ["process_preexec_from_policy"]
