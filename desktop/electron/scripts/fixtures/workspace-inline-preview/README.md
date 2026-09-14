# Local Desktop inline-preview fixture

This is a deterministic, **free, loopback-only Ollama-compatible fixture**, not
a general language model. It does not read/write the test workspace itself,
forward requests, possess provider credentials, or simulate Gateway RPC/tool
results. The real Gateway must advertise and execute every requested tool.

Start from any directory:

```sh
node /path/to/opensquilla/desktop/electron/scripts/fixtures/workspace-inline-preview/provider.mjs 0
```

The first JSON line gives `baseUrl`, model and exact prompts. Configure a fresh
Desktop profile to use this Ollama base URL and model
`opensquilla-inline-preview-fixture`, explicitly enabling `supports_vision`
and `supports_tools` for that model, with `context_window = 131072` and
`max_output_tokens = 8192` in its model override. The generic unknown-model
window is too small for the real Agent's prompt and tool schemas. Do not change production permissions or
another profile's provider settings. Use ordinary single-model routing.

In the actual Desktop app, send these exact current-turn messages:

1. `创建正文链接测试网页`: real `write_file` writes HTML and external CSS,
   then `open_workspace_preview` registers the dedicated directory. The answer
   contains the inline-code path `inline-preview/index.html`.
2. Select the heading in the native preview, annotate `把标题改成紫色` and send.
   Real `read_file`, `edit_file` and preview reopening change the external CSS.
   This prompt also works as ordinary text, but that is **not** screenshot or
   native-annotation acceptance evidence.
3. `测试无文件名回复`: reopens the existing Document and deliberately omits its
   filename from the answer to exercise the lightweight fallback link.

## Task isolation and Gateway restart journey

Use two new ordinary sessions in the same isolated test profile. Keep this
provider process running when restarting the Gateway; the fixture's completion
counters are intentionally in memory, not an alternative persistence system.
The following are exact ordinary-text prompts, not native annotations:

| Session | Prompt | Real tool behavior |
| --- | --- | --- |
| A | `创建隔离任务A网页` | Write `inline-preview/index.html` and external `style.css`, both marked `TASK_A`; register the directory preview. |
| B | `创建隔离任务B网页` | Write the same relative paths, marked `TASK_B`; register the directory preview. |
| A | `把任务A标题改成紫色` | Read and verify initial A CSS; edit heading to `#7c3aed`; reopen. |
| A | `把任务A标题改成绿色` | Read and verify A's first revision; edit heading to `#15803d`; reopen. |
| A, after Gateway restart | `把任务A标题改成红色` | Read and verify A's second revision; edit heading to `#dc2626`; reopen. |
| A | `恢复任务A网页预览` | Read and verify A's third revision, then reopen without changing files. |

The acceptance runner must record both sessions' actual persisted bindings,
Document identities, file bytes, revisions and publication counts. Verify B's
files and preview do not change after every A edit. A should have one baseline
plus three changed revisions; B should have one baseline. Restart/reopening
must retain these identities and add no revision or publication. Provider
completion counters alone do **not** establish these facts or prove the Gateway
restarted. A wrong-task or stale CSS read is rejected before the provider emits
the subsequent edit. Windows `\\` paths in real tool receipts are supported;
this is not Windows native-client acceptance by itself.

After the third A edit, optionally send `让子任务验证任务A工作目录` to A.
The parent first reads A's CSS, calls the real `sessions_spawn`, then calls
`sessions_yield` without a session key. The grounded child request reads A's
CSS and writes `child.txt` containing `TASK_A_CHILD_WORKSPACE_OK` plus a newline.
Only the child writes this file. When the real Gateway pushes the completion
group, the fixture validates the queued child's session/task/agent identity;
the parent then reads `child.txt` to verify its bytes. Unknown, failed or
mismatched completions are rejected. Check the child's persisted binding is
identical to A's, B has no `child.txt`, and the parent/child transcripts identify
the actual writer. The provider's `spawned` snapshot records observed queue
identities, not substitute session records. `completed.childWrite` and
`completed.childVerify` should become 1; `completed.spawnA` normally stays 0
because yielding terminates that turn without another model completion.

No extra agent or model deployment is needed: omit subagent model overrides so
the child inherits this fixture. Keep ordinary owner permissions and advertise
`sessions_spawn`/`sessions_yield` alongside the file tools. For a bounded test
profile, `agents_defaults.subagents` can use `allow_agents: []` (self only) and
`max_children_per_session: 1`. Keep the default task concurrency of 8, or at
least 3 with the default two reserved non-subagent slots. Do not disable or
loosen file permissions to make this journey pass.

Each accepted request logs the scenario, step, `imageCount`, `annotationCount`
and next tool. Image counts describe actual model-bound `images` arrays; no
image bytes or arbitrary message content are logged. Annotation acceptance
requires checking nonzero image/annotation counts in addition to actual UI,
file bytes, versions and publication records. The fixture strips only known
production timestamp/runtime/attachment wrappers and validates page-context
annotations. Unknown requests or unsuccessful tool receipts return HTTP 422.

Provider-only tests (their synthetic receipts are **not** end-to-end evidence):

```sh
node --test desktop/electron/scripts/fixtures/workspace-inline-preview/provider.test.mjs
```

## Real UI acceptance runner

`verify-journey.mjs` drives the existing WebUI or Electron app with ordinary UI
input. It launches only its own loopback Gateway/provider and creates a new
profile; it never seeds sessions, Documents, or successful tool receipts. Web
acceptance includes the two-task/restart journey and a real child writer.
Desktop acceptance covers the native annotation, screenshot attachment and CSS
edit described above. It does not automate operating-system save dialogs or
claim Windows/Linux native acceptance when run on macOS.

Prepare an authorized ordinary checkout, dependencies and current WebUI/Electron
builds first. Follow the repository environment preflight; `.codex` and system
temporary directories are rejected. The output parent must exist, and each
output directory must be new and outside the checkout.

```sh
node desktop/electron/scripts/fixtures/workspace-inline-preview/verify-journey.mjs \
  --source-root /absolute/ordinary/checkout \
  --output /absolute/ordinary/evidence/web-first-attempt --surface web
node desktop/electron/scripts/fixtures/workspace-inline-preview/verify-journey.mjs \
  --source-root /absolute/ordinary/checkout \
  --output /absolute/ordinary/evidence/desktop-first-attempt --surface desktop
```

The runner preserves screenshots, process logs, source/build identity, actual
file hashes and read-only database evidence in `report.json`. A failed attempt
remains failed: fix its cause and use a new output directory for the next run.
It closes only processes it owns and retains evidence/profile files for review.
The [Windows validation prompt](WINDOWS-VALIDATION.md) adds native file-manager,
save-dialog and Windows-specific regression checks for a real Windows machine.
