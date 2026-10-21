# Project Workspaces

A project connects chat tasks to a directory on the computer running the
Gateway. Opening a project does not copy its files or initialize a repository.

## Open or Create a Project

Select **+** beside **Projects** in the Web UI sidebar. **Open existing** is
selected by default: choose a directory and confirm to add it to the project
list. Select the project when starting a task to bind the task to its files.

For an empty project, switch to **Create project**, enter its name, and choose
the parent directory under **Save in**. Creation makes a new child directory
and registers it. An existing directory with that name is not overwritten or
automatically adopted; use **Open existing** to select it instead. The Gateway
remembers the chosen parent directory for subsequent creation.

The dialog identifies the execution computer. When the Gateway runs remotely,
the selected paths belong to that computer. File, shell, and code tools use the
task's bound workspace, subject to their normal permissions and sandbox policy.

Opening a user project does not seed Agent profile templates into it. Automatic
bootstrap, template upgrades, and memory maintenance skip sources overlapping
protected project directories. Explicit file edits still use normal tool
permissions. Project selection does not create a separate project memory store;
see [Memory](memory.md) for configured Agent sources.

## Initialize Project Instructions

Send `/init` from Web UI chat in the selected project. Optional text after the
command can describe what the guide should emphasize:

```text
/init Include the development commands and the boundaries between packages.
```

The Agent inspects existing instructions, manifests, documentation, directory
layout, and build/test configuration before writing. It records supported
project facts rather than generating an empty template. Existing wording and
rules are preserved while missing or stale facts receive small edits. New
guides aim to stay below 100 lines.

The target is the project root's `AGENTS.md`, or an existing `agent.md` when
`AGENTS.md` is absent. Initialization does not install dependencies, run project
setup or tests, initialize Git, or create profile and memory files. It uses the
ordinary chat turn and existing source-editing tools. If those tools are not
authorized, the Agent explains the limitation and performs read-only analysis.

## Use Project Rules

The selected root instruction file is read at the start of each chat turn.
Changes take effect on the next turn; editing the file does not replace the
rules in an already-running turn. The snapshot identifies unreadable or
truncated content instead of silently presenting it as complete.

Automatic loading covers the bound project root. The Agent is instructed to
read relevant deeper-directory rules when working there. Project rules do not
override current user instructions, system requirements, or tool permissions.

## Working Files and Published Results

Working files can continue changing after publication. A historical deliverable
keeps its published content; **Continue editing** opens its current working
version. See [Artifacts and Media](../artifacts-and-media.md) for preview,
download, HTML resources, and channel delivery behavior.

---

[Docs index](../README.md) · [Web UI](../web-ui.md) · [Tools and sandbox](../tools-and-sandbox.md)
