# Approvals and Permissions

Approvals and permissions control how OpenSquilla tools are allowed to act.
They matter most when an agent can write files, run shell commands, publish
artifacts, post into channels, or call external services.

Use this page before running unattended automation or giving a channel-connected
agent broad tool access.

## Permission Profiles

Single-shot automation accepts an explicit permission profile:

```sh
opensquilla agent --permissions restricted -m "Inspect this repo"
opensquilla agent --permissions on -m "Run with host execution and approvals"
opensquilla agent --permissions bypass -m "Trusted local automation"
opensquilla agent --permissions full -m "Fully trusted local automation"
```

Practical meaning:

| Profile | Use when |
| --- | --- |
| `restricted` / `off` | The task should stay conservative and avoid elevated execution. |
| `on` | Host execution is allowed, but approval checks still matter. |
| `bypass` | You trust the task enough to auto-grant approvals while keeping sensitive-path checks. |
| `full` | You fully trust the task and environment. Use sparingly. |

For automation, prefer the narrowest profile that can complete the task.

## Workspace Containment

Set a workspace for file and shell work:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Summarize this repo"
```

Contain writes to the workspace or scratch directory:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and prepare a minimal patch"
```

Use `--workspace-lockdown` for unattended runs where accidental writes outside
the project would be unacceptable.

## Interactive Approvals

Interactive chat surfaces can pause sensitive tool calls for a human decision.
Gateway-backed terminal chat supports:

```text
/approvals
/approvals reset
/permissions status
/permissions on
/permissions off
/permissions bypass
/permissions full
/forget
```

Use these commands when you need to inspect or reset cached approval decisions
during a chat.

The Web UI also provides an approvals surface for reviewing pending actions
outside the message scrollback.

In Safe mode, approvals are intentionally narrow:

- edits, moves, renames, and deletions under protected file paths;
- every statically identified recursive directory deletion, with an
  irreversible-action warning and backup result;
- `git push`, configured high-risk command prefixes, and system tools set to
  **Ask first**.

Other commands and ordinary file mutations run automatically. Full Access
bypasses Safe policy approvals and executes with host permissions.

## Remote Web Guests

A remote Web connection with no token, a malformed token, or an incorrect
token receives the same Guest Safe permissions. It can read ordinary host
files, cannot read built-in credential paths or OpenSquilla authority data,
and can write only inside the server's configured default workspace.

The server chooses that workspace; the Web client cannot replace it or create
another workspace. The boundary is enforced uniformly for file tools, Shell,
Python, Node.js, Git Bash, and child processes. Guests do not receive access to
the global approval queue, so a pending approval cannot turn a guest into an
owner, grant host-wide writes, or affect another session. High-risk actions
that need approval remain blocked until the caller authenticates.

Loopback desktop sessions are local owners. A remote Web session becomes an
authenticated principal only after presenting a valid named token; the
token's configured scopes then determine its authority.

## Sandbox Posture

Inspect sandbox posture:

```sh
opensquilla sandbox status
opensquilla sandbox status --json
```

Set the current compatibility posture:

```sh
opensquilla sandbox on
opensquilla sandbox full
opensquilla sandbox reset
```

The supported product modes are **Safe** and **Full Access**. Older CLI mode
names remain accepted only as an upgrade compatibility shim and are not shown
in the current UI.

Restart the gateway after changing global sandbox posture:

```sh
opensquilla gateway restart
```

## Recommended Defaults

| Situation | Recommended approach |
| --- | --- |
| First run in a repo | `--workspace` plus `--workspace-strict` |
| Read-only investigation | `--permissions restricted` |
| Local patch with tests | `--workspace-lockdown` plus a scratch directory |
| Web UI task with writes | Keep approvals visible and review sensitive actions |
| Channel-connected agent | Conservative permissions and explicit channel setup |
| Unattended automation | Bound timeout/iterations and choose the narrowest workable permissions |

## Troubleshooting

If a tool is denied:

```sh
opensquilla sandbox status
opensquilla doctor
```

Then check:

- whether the surface supports live approvals;
- whether the workspace path is correct;
- whether cached approvals need to be reset;
- whether the task should run with a different permission profile.

Read next:

- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`web-ui.md`](web-ui.md)
- [`channels.md`](channels.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
