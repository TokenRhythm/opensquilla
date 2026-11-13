"""In-process desktop-pet reminder and directed-dispatch service (PRD F2/F3).

Importing this package has no side effects: nothing registers, starts a task,
or probes a port until :class:`~opensquilla.pet.service.PetReminderService` is
explicitly started by the gateway boot path (and only when ``[pet].enabled``).

The package talks to the gateway through the narrow seams declared in
:mod:`opensquilla.pet.ports` (event observation, RPC invocation, redaction) so
it can later move to a built-in plugin without reaching into gateway internals.
"""

from __future__ import annotations

__all__: list[str] = []
