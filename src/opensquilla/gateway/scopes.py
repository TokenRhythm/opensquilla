"""Gateway RPC scope policy — single source of truth.

Every release-surface gateway method registered against ``RpcRegistry``
must appear here, either as an explicit entry in ``METHOD_SCOPES`` /
``NODE_ROLE_METHODS`` or be matched by an entry in
``ADMIN_METHOD_PREFIXES``. The registry audits this invariant at boot and
then locks the process-wide method surface, so a missing classification or
late module import fails loudly rather than silently changing request-time
authorization.

Scope implication is namespace-bounded:

* ``operator.admin`` satisfies any ``operator.*`` requirement.
* ``operator.write`` satisfies ``operator.read``.
* No implication crosses the ``operator.*`` boundary into other scope
  namespaces (``node``, future ``system.*``, plugin scopes).

The shape of the table follows the gateway method-scope contract. See
``THIRD_PARTY_NOTICES.md`` for relevant attributions. The Python
implementation is independent.
"""

from __future__ import annotations

from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------

ADMIN_SCOPE = "operator.admin"
READ_SCOPE = "operator.read"
WRITE_SCOPE = "operator.write"
APPROVALS_SCOPE = "operator.approvals"
PROPOSALS_SCOPE = "operator.proposals"
PAIRING_SCOPE = "operator.pairing"
NODE_SCOPE = "node"

OPERATOR_SCOPE_NAMESPACE = "operator."

# Default scope set for a locally-proven operator: same machine, loopback
# transport. Mirrors what the desktop CLI declares on connect.
CLI_DEFAULT_OPERATOR_SCOPES: frozenset[str] = frozenset(
    {
        ADMIN_SCOPE,
        READ_SCOPE,
        WRITE_SCOPE,
        APPROVALS_SCOPE,
        PROPOSALS_SCOPE,
        PAIRING_SCOPE,
    }
)

# Default scope set for a remote / unproven operator under no-auth mode.
# Notably excludes ``operator.admin``: unauthenticated remote callers must
# not get destructive privileges. Pairing and proposals are also excluded:
# proposal mutation promotes generated SKILL.md files into the managed skill
# layer, so remote callers need an authenticated/admin path for that surface.
REMOTE_OPERATOR_SCOPES: frozenset[str] = frozenset({READ_SCOPE, WRITE_SCOPE, APPROVALS_SCOPE})

# Default scopes for the node role (separate scope namespace).
NODE_DEFAULT_SCOPES: frozenset[str] = frozenset({NODE_SCOPE})

# ---------------------------------------------------------------------------
# Method classification
# ---------------------------------------------------------------------------

# Methods callable by the ``node`` role. The dispatch path short-circuits
# scope checks for these when ``role == "node"``. Operators can still call
# them if they hold ``operator.admin`` (admin-as-superuser pragma).
NODE_ROLE_METHODS: frozenset[str] = frozenset({"skills.bins"})

# Method-name prefixes whose unclassified members default to ADMIN_SCOPE.
# Explicit entries in ``METHOD_SCOPES`` take precedence over prefix rules.
ADMIN_METHOD_PREFIXES: tuple[str, ...] = (
    "config.",
    "exec.approvals.",
    "wizard.",
    "update.",
)

# Single source of truth for method → required scope. Order is grouped by
# scope to make audits easy. Comments mark methods that are OpenSquilla-specific
# so future maintainers know they were classified locally.
METHOD_SCOPES: dict[str, str] = {
    # ----- read -----
    "health": READ_SCOPE,
    "status": READ_SCOPE,
    "config.get": READ_SCOPE,
    # OpenSquilla-only; provenance view over non-secret effective LLM fields.
    # Explicit entry required: the `config.` prefix defaults to admin and the
    # boot audit hard-fails on declared-vs-table drift (config.get precedent).
    "config.effective": READ_SCOPE,
    "config.schema.lookup": READ_SCOPE,
    "sessions.get": READ_SCOPE,
    "sessions.list": READ_SCOPE,
    "sessions.search": READ_SCOPE,
    "sessions.preview": READ_SCOPE,
    "sessions.resolve": READ_SCOPE,
    "sessions.bootstrap": READ_SCOPE,
    "sessions.subscribe": READ_SCOPE,
    "sessions.unsubscribe": READ_SCOPE,
    "sessions.messages.subscribe": READ_SCOPE,
    "sessions.messages.unsubscribe": READ_SCOPE,
    "gateway.identity.get": READ_SCOPE,
    "last-heartbeat": READ_SCOPE,
    "system-presence": READ_SCOPE,
    "doctor.status": READ_SCOPE,
    "doctor.memory.status": READ_SCOPE,
    "diagnostics.status": READ_SCOPE,
    "logs.status": READ_SCOPE,
    "logs.tail": READ_SCOPE,
    "logs.trace": READ_SCOPE,
    "models.list": READ_SCOPE,
    "models.routing.get": READ_SCOPE,
    "providers.status": READ_SCOPE,
    # OpenSquilla-only; non-consuming peek at a session's router-control hold
    # plus the valid target menu (see rpc_routing.py).
    "routing.hold.get": READ_SCOPE,
    "search.status": READ_SCOPE,
    "memory.list": READ_SCOPE,
    "memory.search": READ_SCOPE,
    "memory.show": READ_SCOPE,
    "tools.catalog": READ_SCOPE,
    "tools.effective": READ_SCOPE,
    "tools.search_provider": READ_SCOPE,  # OpenSquilla-only; classified read.
    "sandbox.status": READ_SCOPE,  # OpenSquilla-only; sandbox posture summary.
    "sandbox.setup.status": READ_SCOPE,  # OpenSquilla-only; setup readiness.
    "sandbox.explain": READ_SCOPE,  # OpenSquilla-only; deterministic sandbox explanation.
    "sandbox.run_context.get": READ_SCOPE,  # OpenSquilla-only; session sandbox mode.
    "sandbox.path.list": READ_SCOPE,  # OpenSquilla-only; inline path browser listing.
    "channels.status": READ_SCOPE,
    "commands.list_for_surface": READ_SCOPE,  # OpenSquilla-only.
    "chat.history": READ_SCOPE,
    "agents.list": READ_SCOPE,
    "agents.files.list": READ_SCOPE,
    "agents.files.get": READ_SCOPE,
    "agent.identity.get": READ_SCOPE,
    "skills.status": READ_SCOPE,
    "skills.list": READ_SCOPE,
    "skills.get": READ_SCOPE,
    "skills.search": READ_SCOPE,
    "cron.list": READ_SCOPE,
    "cron.status": READ_SCOPE,
    "cron.runs": READ_SCOPE,
    "cron.subscribe": READ_SCOPE,  # OpenSquilla-only; classified read.
    "cron.unsubscribe": READ_SCOPE,  # OpenSquilla-only; classified read.
    "usage.status": READ_SCOPE,
    "usage.cost": READ_SCOPE,
    "usage.query": READ_SCOPE,
    "meta.list": READ_SCOPE,  # OpenSquilla-only; invokable meta-skill catalog.
    "meta.runs.list": READ_SCOPE,
    "meta.runs.failures": READ_SCOPE,
    "meta.runs.cost": READ_SCOPE,
    # OpenSquilla-only — persisted per-turn router decision records (V017
    # router_decisions). The table stores enum tokens and numbers only (no
    # prompt text), so the listing is a plain operator read.
    "router.decisions.list": READ_SCOPE,
    # OpenSquilla-only — self-learning loop status (active model, sample
    # counts, gate reason, last receipt). Derived from on-disk loop state;
    # no prompt text, no side effects — a plain operator read.
    "router.selflearning.status": READ_SCOPE,
    # OpenSquilla-only — onboarding catalog and status are operator-readable.
    "onboarding.status": READ_SCOPE,
    "onboarding.catalog": READ_SCOPE,
    "onboarding.router.catalog": READ_SCOPE,
    # ----- write -----
    "wake": WRITE_SCOPE,
    "send": WRITE_SCOPE,
    "agent": WRITE_SCOPE,
    "agent.wait": WRITE_SCOPE,
    "chat.send": WRITE_SCOPE,
    "chat.abort": WRITE_SCOPE,
    "chat.clarify_submit": WRITE_SCOPE,
    "search.query": WRITE_SCOPE,
    "sessions.create": WRITE_SCOPE,
    "sessions.fork": WRITE_SCOPE,
    "sessions.send": WRITE_SCOPE,
    "sessions.steer": WRITE_SCOPE,
    "sessions.abort": WRITE_SCOPE,
    "sessions.reset": WRITE_SCOPE,
    "sessions.contextCompact": WRITE_SCOPE,
    "sessions.compact": WRITE_SCOPE,
    "sessions.truncate": WRITE_SCOPE,
    "models.routing.set": WRITE_SCOPE,
    # Deleting a session is a routine, per-user write op like reset/truncate above,
    # so it is write-scoped rather than admin-gated. Admin-gating it broke deletion
    # for every no-auth operator on a non-loopback bind — notably the default Docker
    # 0.0.0.0 listen, where even a 127.0.0.1 peer is not the local owner and so gets
    # REMOTE_OPERATOR_SCOPES (no admin) — surfacing as "Failed to delete session"
    # (issues #357, #307).
    "sessions.delete": WRITE_SCOPE,
    "sandbox.workspace.set": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.mount.add": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.mount.remove": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.domain.add": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.domain.remove": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.bundle.enable": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.bundle.disable": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.setup.ensure": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded setup.
    "sandbox.resume": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded denial-pause clear.
    "sandbox.run_context.set": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded handler.
    "sandbox.path.pick": WRITE_SCOPE,  # OpenSquilla-only; owner-guarded host directory picker.
    # OpenSquilla-only; explicit override of `config.` admin prefix.
    "config.patch.safe": WRITE_SCOPE,
    # OpenSquilla-only; manual ``/meta`` command launch stamp.
    "meta.run": WRITE_SCOPE,
    # ----- approvals -----
    # Policy getters/setters explicitly override the ``exec.approvals.`` prefix
    # so that approval workers (which hold operator.approvals) can read/set the
    # per-operator policy without needing full admin.
    "exec.approvals.get": APPROVALS_SCOPE,
    "exec.approvals.set": APPROVALS_SCOPE,
    "exec.approval.request": APPROVALS_SCOPE,
    "exec.approval.waitDecision": APPROVALS_SCOPE,
    "exec.approval.status": APPROVALS_SCOPE,
    "exec.approval.snapshot": APPROVALS_SCOPE,
    "exec.approval.forget": APPROVALS_SCOPE,
    "exec.approval.resolve": APPROVALS_SCOPE,
    "exec.approval.extend": APPROVALS_SCOPE,
    "plugin.approval.request": APPROVALS_SCOPE,
    "plugin.approval.waitDecision": APPROVALS_SCOPE,
    "plugin.approval.status": APPROVALS_SCOPE,
    "plugin.approval.resolve": APPROVALS_SCOPE,
    "plugin.approval.extend": APPROVALS_SCOPE,
    # ----- proposals (auto-propose UI: list/show) -----
    # ``exec.proposals.*`` prefix sits OUTSIDE the ``exec.approvals.``
    # admin prefix so that proposal browsing can remain operator-visible.
    "exec.proposals.pending_count": PROPOSALS_SCOPE,
    "exec.proposals.list": PROPOSALS_SCOPE,
    "exec.proposals.show": PROPOSALS_SCOPE,
    "exec.proposals.settings.get": PROPOSALS_SCOPE,
    # Channel identity pairing is a dedicated operator capability. Admin
    # implies this scope, while remote no-auth operators do not receive it.
    "channels.pairings": PAIRING_SCOPE,
    "channels.pairing.approve": PAIRING_SCOPE,
    "channels.pairing.revoke": PAIRING_SCOPE,
    # Grant/revoke a sender's channel-admin standing. Same narrow scope as
    # pairing: an operator managing a channel's members may promote or demote
    # its senders, but this is not an arbitrary config write.
    "channels.admin.set": PAIRING_SCOPE,
    "exec.proposals.auto_enabled.list": PROPOSALS_SCOPE,
    # ----- admin -----
    # OpenSquilla-only; re-reads the on-disk TOML and swaps the ENTIRE runtime
    # config (values + runtime-secret markers), so it stays admin even though
    # the `config.` prefix default would already classify it as admin.
    "config.reload": ADMIN_SCOPE,
    "chat.inject": ADMIN_SCOPE,
    "system-event": ADMIN_SCOPE,
    "set-heartbeats": ADMIN_SCOPE,
    "secrets.reload": ADMIN_SCOPE,
    "secrets.resolve": ADMIN_SCOPE,
    "agents.create": ADMIN_SCOPE,
    "agents.update": ADMIN_SCOPE,
    "agents.delete": ADMIN_SCOPE,
    "agents.files.set": ADMIN_SCOPE,
    "skills.install": ADMIN_SCOPE,
    "skills.update": ADMIN_SCOPE,
    "skills.uninstall": ADMIN_SCOPE,
    "skills.reload": ADMIN_SCOPE,
    "skills.deps.install": ADMIN_SCOPE,
    "meta.runs.show": ADMIN_SCOPE,
    "meta.runs.draft": ADMIN_SCOPE,
    "meta.runs.confirm_preflight": ADMIN_SCOPE,
    "meta.runs.diff": ADMIN_SCOPE,
    "meta.runs.replay": ADMIN_SCOPE,
    "meta.runs.validate": ADMIN_SCOPE,
    "meta.runs.eval_baseline": ADMIN_SCOPE,
    # OpenSquilla-only — live feedback intake (F7). Resolves a decision id and
    # appends a rating to the per-agent self-learning feedback sidecar. Write
    # scope: chat surfaces submit ratings on behalf of the user; it never
    # mutates routing state directly (consumption is offline, at training).
    "router.feedback.submit": WRITE_SCOPE,
    # Proposal mutation changes the managed skill layer or unattended
    # synthesis state, so require authenticated admin rather than remote
    # no-auth operator.proposals.
    "exec.proposals.accept": ADMIN_SCOPE,
    "exec.proposals.reject": ADMIN_SCOPE,
    "exec.proposals.settings.set": ADMIN_SCOPE,
    "exec.proposals.auto_enabled.disable": ADMIN_SCOPE,
    "channels.logout": ADMIN_SCOPE,
    "channels.restart": ADMIN_SCOPE,  # OpenSquilla-only.
    "channels.get": ADMIN_SCOPE,  # Redacted editable config still exposes secret presence.
    "channels.probe": ADMIN_SCOPE,  # Live credential/network probe.
    "diagnostics.set": ADMIN_SCOPE,
    "onboarding.provider.credential.reveal": ADMIN_SCOPE,
    "onboarding.provider.credential.clear": ADMIN_SCOPE,
    "cron.add": ADMIN_SCOPE,
    "cron.create": ADMIN_SCOPE,  # OpenSquilla-only alias for cron.add.
    "cron.update": ADMIN_SCOPE,
    "cron.remove": ADMIN_SCOPE,
    "cron.run": ADMIN_SCOPE,
    "sessions.patch": ADMIN_SCOPE,
    # OpenSquilla-only — operator router-control holds pin a session's routing
    # tier (or restore auto), rebinding which model serves its turns, so
    # mutation is admin-gated like sessions.patch (which rebinds a session's
    # model the same way). The read peek is classified above.
    "routing.hold.set": ADMIN_SCOPE,
    "routing.hold.clear": ADMIN_SCOPE,
    "memory.index": ADMIN_SCOPE,
    "memory.raw_fallbacks.list": ADMIN_SCOPE,
    "memory.raw_fallbacks.show": ADMIN_SCOPE,
    "memory.repair.list": ADMIN_SCOPE,
    "memory.repair.run": ADMIN_SCOPE,
    "memory.repair.show": ADMIN_SCOPE,
    # Settings-only profile import discovery. These methods expose no paths
    # and never apply an import, but host-level inventory remains admin-only.
    "migration.sources.list": ADMIN_SCOPE,
    "migration.sources.preview": ADMIN_SCOPE,
    # OpenSquilla-only — onboarding mutations require admin scope.
    "onboarding.provider.configure": ADMIN_SCOPE,
    # The probe persists nothing but carries candidate credentials.
    "onboarding.provider.probe": ADMIN_SCOPE,
    "onboarding.llmProfile.upsert": ADMIN_SCOPE,
    "onboarding.llmProfile.credential.clear": ADMIN_SCOPE,
    "onboarding.llmProfile.remove": ADMIN_SCOPE,
    "onboarding.llmProfile.activate": ADMIN_SCOPE,
    "onboarding.llmProfile.probe": ADMIN_SCOPE,
    "onboarding.llmProfile.models.discover": ADMIN_SCOPE,
    "onboarding.llmProfile.draft.probe": ADMIN_SCOPE,
    "onboarding.llmProfile.draft.models.discover": ADMIN_SCOPE,
    # Model discovery is read-shaped but admin-scoped for the same reason as
    # the probe: its params accept candidate credentials (apiKey/apiKeyEnv),
    # which must never be acceptable from a read/write-tier caller.
    "onboarding.models.discover": ADMIN_SCOPE,
    "onboarding.router.configure": ADMIN_SCOPE,
    "onboarding.ensemble.configure": ADMIN_SCOPE,
    "onboarding.memory_embedding.configure": ADMIN_SCOPE,
    "onboarding.search.configure": ADMIN_SCOPE,
    "onboarding.imageGeneration.configure": ADMIN_SCOPE,
    "onboarding.audio.configure": ADMIN_SCOPE,
    "onboarding.channel.probe": ADMIN_SCOPE,
    "onboarding.channel.upsert": ADMIN_SCOPE,
    "onboarding.channel.remove": ADMIN_SCOPE,
    "onboarding.channel.enable": ADMIN_SCOPE,
    "onboarding.channel.disable": ADMIN_SCOPE,
}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_required_scope(method: str) -> str | None:
    """Return the required scope for ``method``, or ``None`` if unclassified.

    Lookup order matches the registration-time check: explicit table entry
    wins, then admin prefix, then ``None``. Node-role methods are not in
    the operator table; callers that need to authorize them should consult
    :data:`NODE_ROLE_METHODS` and the calling role first.
    """
    explicit = METHOD_SCOPES.get(method)
    if explicit is not None:
        return explicit
    if any(method.startswith(p) for p in ADMIN_METHOD_PREFIXES):
        return ADMIN_SCOPE
    return None


def is_classified(method: str) -> bool:
    """Return True iff ``method`` has a known scope classification."""
    if method in METHOD_SCOPES or method in NODE_ROLE_METHODS:
        return True
    return any(method.startswith(p) for p in ADMIN_METHOD_PREFIXES)


def operator_scope_satisfies(required: str, granted: Iterable[str]) -> bool:
    """Namespace-bounded scope implication check.

    * ``operator.admin`` satisfies any ``operator.*`` requirement.
    * ``operator.write`` satisfies ``operator.read``.
    * For ``operator.admin`` requirement, only an explicit grant works.
    * For non-operator scopes (``node`` etc.), exact match is required.
      Explicit pragma: ``operator.admin`` *also* satisfies ``node`` so
      that a local admin can call diagnostic node-role methods such as
      ``skills.bins``; this preserves prior behavior and matches the
      "admin is superuser on this gateway" intent.
    """
    granted_set = granted if isinstance(granted, (set, frozenset)) else set(granted)

    if required == ADMIN_SCOPE:
        return ADMIN_SCOPE in granted_set
    if required.startswith(OPERATOR_SCOPE_NAMESPACE):
        if ADMIN_SCOPE in granted_set:
            return True
        if required == READ_SCOPE:
            return READ_SCOPE in granted_set or WRITE_SCOPE in granted_set
        return required in granted_set
    if required == NODE_SCOPE:
        return NODE_SCOPE in granted_set or ADMIN_SCOPE in granted_set
    return required in granted_set


def normalize_operator_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Expand implied scopes into a normalized set.

    Stored / configured scope lists are normalized so that a token
    declared as ``["operator.write"]`` behaves identically whether the
    consumer checks via :func:`operator_scope_satisfies` or by direct
    membership. Idempotent; safe to call repeatedly.
    """
    out = set(scopes)
    if ADMIN_SCOPE in out:
        out.update({READ_SCOPE, WRITE_SCOPE})
    elif WRITE_SCOPE in out:
        out.add(READ_SCOPE)
    return frozenset(out)


def authorize_call(
    method: str,
    required_scope: str,
    role: str,
    granted: Iterable[str],
) -> tuple[bool, str | None]:
    """Decide whether ``role`` with ``granted`` scopes may call ``method``.

    ``required_scope`` is the scope the registry recorded at registration
    time (authoritative per request). The central table in this module
    governs the *invariant* that every core method's recorded scope
    matches its canonical classification, but runtime authorization uses
    the registered scope so that test-only dispatchers with ad-hoc
    methods still work without polluting the production table.

    Returns ``(allowed, missing_scope)``. ``missing_scope`` is ``None``
    on allow; on deny it names the scope the caller would need.
    """
    granted_set = granted if isinstance(granted, (set, frozenset)) else frozenset(granted)

    if role == "node":
        if method in NODE_ROLE_METHODS or required_scope == NODE_SCOPE:
            return (True, None) if NODE_SCOPE in granted_set else (False, NODE_SCOPE)
        # Node role cannot invoke operator methods regardless of scope.
        return False, NODE_SCOPE

    # Operator role below.
    if required_scope == NODE_SCOPE:
        # Operators call node-role methods only via admin (superuser pragma).
        if ADMIN_SCOPE in granted_set:
            return True, None
        return False, ADMIN_SCOPE

    if operator_scope_satisfies(required_scope, granted_set):
        return True, None
    return False, required_scope


# ---------------------------------------------------------------------------
# Loopback detection
# ---------------------------------------------------------------------------


def is_loopback_address(addr: str | None) -> bool:
    """Return True iff ``addr`` is a literal loopback IPv4/IPv6 address.

    Only string-level checks; no DNS, no hostname resolution. ``localhost``
    is treated as loopback for parity with the bind-host check, but the
    canonical case in production is a numeric peer address from the WS
    upgrade request.
    """
    if not addr:
        return False
    host = addr.split("%", 1)[0]  # strip IPv6 zone-id
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in ("::1", "localhost"):
        return True
    if host.startswith("127."):
        parts = host.split(".")
        if len(parts) != 4:
            return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False
    return False


def is_loopback_bind(host: str | None) -> bool:
    """Return True iff the gateway bound to a loopback-only address.

    A non-loopback bind (``0.0.0.0``, ``::``, a LAN address) means the
    gateway accepts non-local peers and must not auto-grant admin even
    if a particular peer happens to be loopback.
    """
    if not host:
        return False
    if host in ("localhost",):
        return True
    return is_loopback_address(host)
