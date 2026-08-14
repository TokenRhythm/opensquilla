# Sandbox Security

OpenSquilla exposes two execution modes:

- **Safe mode** runs tasks through the sandbox and applies the file, command,
  network, and bundled-runtime policy configured in Settings.
- **Full access** runs with the authenticated host user's permissions and does
  not apply Safe-mode policy.

Fresh installations default to Safe mode. Existing installations are migrated
before the gateway starts: old `standard`, `trusted`, and `managed` values are
accepted as input and normalized to Safe, while `full` remains Full access.
New data is written using only `safe` and `full`.

## Authentication boundary

Local owners and authenticated human tokens may use Safe mode or Full access.
LAN requests with no token and requests with an invalid token receive the same
guest-safe execution permissions. An invalid token still counts as a failed
authentication attempt and is rate-limited.

Guest-safe tasks receive a new temporary workspace and HOME directory. They do
not mount the host HOME, an existing host project, host secrets, or OpenSquilla
authority and recovery data. A guest task is rejected when the sandbox is
unavailable; it never falls back to host execution.

Full access requires the `host.execute` capability. Asking for Full access
without it fails instead of silently changing the request to guest-safe.

LAN token authentication is not encrypted when TLS is disabled. Use it only on
a network you trust. OpenSquilla derives the client boundary from the socket
peer and does not trust forwarded client-IP headers.

## File policy

Safe mode permits ordinary file reads and writes by default. OpenSquilla
authority, token, migration, recovery, and backup-vault paths remain internally
unreadable and unwritable.

The file settings contain a deny-write list. Built-in entries protect common
credential and system paths and cannot be removed. Users can add, edit, and
remove custom entries. A write, edit, rename, overwrite, or deletion that
touches a protected path requires approval; other ordinary mutations do not.

Recognized recursive deletion always requires a separate confirmation. The
dialog identifies the target and makes clear that recursive deletion can be
irreversible. Cancel is the default action.

Recursive-delete backup is enabled by default with a 3 GiB quota. Before the
delete, OpenSquilla stages and verifies a recoverable backup. When the quota
would be exceeded, it evicts the oldest complete backups first. If the target
cannot fit even after eviction, deletion remains cancelled unless the user
passes a second explicit irreversible-delete confirmation.

## Command policy

Ordinary Safe-mode commands run automatically. Built-in high-risk remote
actions, including `git push`, package publishing, production deployment, and
recognized destructive cloud or database operations, require approval.

Settings can add tokenized command prefixes to either the approval list or the
automatic list. The decision order is:

1. non-overridable structural protections;
2. user automatic prefixes;
3. user approval prefixes;
4. built-in high-risk actions;
5. automatic execution.

System-level tools have a separate automatic, approval, or disabled setting.
The default is automatic. File and recursive-delete protections still apply
regardless of the command prefix decision.

## Network policy

Safe mode allows public network targets by default. It retains protections
against cloud metadata endpoints, loopback, link-local and private-network
probing, DNS rebinding, unsafe redirects, and malformed domain boundaries.

Settings can define allowed domains, denied domains, or block all network
access. When block-all is enabled, the allow list supplies explicit exceptions.
At equal specificity, a deny rule wins. Sandbox egress cannot connect back to
the gateway listener.

## Bundled developer runtimes

Desktop packages include pinned Python and Node.js runtimes on Windows, macOS,
and Linux for x64 and arm64 targets. Windows packages also include pinned Git
for Windows and Git Bash. Safe mode prefers bundled tools; Full access prefers
the host PATH and uses bundled tools as a fallback.

Runtime assets are checksum-pinned in the release manifest and smoke-tested
before packaging. The exposed tools can be enabled or disabled in sandbox
settings without removing their installed files.

## Sandbox unavailable

When capability probing shows that Safe mode cannot start, the normal mode
selector simply disables Safe mode without an extra banner, badge, or error
color. Desktop startup shows one native safety notice unless the user chooses
“Don't remind me again.”

An authenticated host task whose saved preference is Safe can soft-land to Full
access for that turn. The task records both the desired and effective modes;
the stored preference remains Safe. A sandbox failure after execution starts
stops the task and never replays it automatically with host permissions.

---

[Docs index](README.md) · [Tools and approvals](tools-and-sandbox.md) ·
[Permission guide](approvals-and-permissions.md)
