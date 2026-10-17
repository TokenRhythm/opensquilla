# Project workspace resources: implementation review and validation

All six implementation stages are recorded as local commits. PR #1930 contained
only P1 and was closed; its title, body and closure message are English. No
replacement PR was opened before completing implementation and the available
source/runtime validation described below.

The original comparison base is `d3f926c17e361b27f0410e6ea52526c9a2bcbb3d`.
Final integration also includes the subsequent main change
`7cdca98faa48fbfb6b5b0826b4f8f9d811122d78` for `/plan` submission. Its overlapping
composer paths passed combined `/plan` and `/init` verification. A queued Plan
prompt starting with `/init` or `//init` initially failed three new real queue/drain
regressions; reusing the existing literal-slash escape fixed them without adding
another command or queue representation.

## Review findings addressed

Independent source reviews covered each phase and the composed call chains.
The following findings resulted in changes or additional regression coverage:

- P1: legacy session databases without newer columns, CLI default bootstrap and
  non-main Agent source protection. The test fixtures now create their explicitly
  bound workspace rather than relying on removed implicit directory creation.
- P2: late Gateway identity must reload the remembered creation parent; execution
  host text must not use the WebUI development proxy as the host name.
- P3: split MEMORY/notes sources and legacy environment overrides, WEB/CLI owner
  callers, explicit policy denial, actual-write bootstrap invalidation and optional
  revision compatibility for existing RPC clients. `memory_save` remains default-deny.
- P4: init capability revoked before an unsent queued RPC must not leave an unknown
  remote-acceptance state; unavailable write tools must use the read-only template.
  Turn-command intent survives queue persistence, reconnect and duplicate handling.
- P5: published Deliverable rows must retain A; canonical Document rows remain
  current B and are labelled accordingly. Late Continue-editing responses cannot
  take focus after a tab switch, including switching away and back.
- P6: validation/capture concurrency, exact validated single-file bytes, missing
  receipts, cancellation during filesystem reads, cleanup after successful send,
  safe user-visible errors, and old/new material identity under the existing outbox.

No migration framework, global USER source, project memory partition, alternate
outbox, persistent ZIP cache, public deployment service or new history type was
introduced. Existing source files, resource identities and queue/outbox lifecycles
remain authoritative.

## Automated evidence

These are affected-module and integration checks, not a complete repository test
run. Groups overlap; their totals must not be added as a count of unique tests.
Existing Windows/POSIX/link skips were retained without adding skips or retries.

| Scope | Result | Local record |
| --- | --- | --- |
| Startup, project creation/CWD, effective sources, rules/init and runtime | 1,535 passed, 30 skipped | `.tmp/final-backend-summary.json` |
| Material capture, publication, channel adapters/outbox, download and preview RPC | 1,219 passed, 12 skipped | `.tmp/final-delivery-summary.json` |
| Affected frontend collection | 33 files, 1,048 passed | `.tmp/p2-p4-p5-final-results.json` |
| Independent P3-only source tree | 244 passed | `.tmp/p3-isolated.xml` |
| Independent P4-only source tree | 319 passed, 8 skipped; config and Vue checks passed | `.tmp/p4-isolated-tests-fixed.xml` |
| Python static checks | Ruff: 115 changed files; mypy: 49 non-generated source files | `.tmp/final-static-summary.json` |
| Contract generation | Current and legacy toolchains; determinism check passed | Final codegen check output |
| Architecture, public release hygiene and static golden checks | 21 passed | Final static test output |
| Prompt records | 16 exact template/tool/result comparisons passed | `.tmp/final-static-summary.json` |
| Frontend build before main integration | 438 files verified and staged | `.tmp/p5-final-build.log` |
| Main integration: sender, slash, queue, WAL, adapters and card entry points | 10 files, 622 passed | `.tmp/main-plan-init-final-results.json` |
| Final full frontend typecheck and build | Both passed; 438 build files verified | `.tmp/main-plan-init-typecheck.log`, `.tmp/main-plan-init-build.log` |

The full frontend typecheck initially found a test fixture's resource-reference
union error. The fixture now uses the existing canonical constructor; final Vue
typecheck and 27 affected navigation tests passed. Architecture/configuration checks
had already passed. Initial failed logs are retained separately from passing runs.

The first delivery milestone exposed 13 fixture failures caused by the deliberate
P1 removal of implicit workspace creation. Eleven explicit fixture `mkdir` calls
fixed setup without changing product behavior or weakening assertions; the complete
delivery group then passed.

Desktop bridge evidence is separate: relevant TypeScript modules compiled; file
actions passed 26 tests with two existing Windows link skips; lease-broker tests
passed; a hidden isolated Electron fixture exercised production preload, signed
broker and ownership/failure paths. OS opening was mocked. The attempted full
Desktop compile used stale shared dependencies and is not claimed as passing.

## Real browser and local-runtime evidence

All fixtures use disposable project directories and isolated profiles under `.tmp`.
No real user project, profile or native application was modified.

| Scenario | Observed result |
| --- | --- |
| Create project with Chinese text and a space | Created an empty directory under the selected parent; parent remembered and execution host displayed |
| Create the same name again | Clear existing-directory error; no implicit adoption |
| Open existing project | Existing AGENTS bytes and directory contents remained unchanged |
| Explicit main `/init` | Real Gateway/TaskRuntime/TurnRunner delivered init intent and V1 rules to a local deterministic provider |
| Next turn and `//init` | V2 rules loaded on the next turn; escaped init remained ordinary user text |
| Embedded side chat `/init` | Explicit intent and V2 rules reached the same runtime pipeline |
| Cross-project Agent profile | Real `agent_file` read/replace/read used one independent Agent source, invalidated prior snapshots and appeared in project B's next prompt; no project copies |
| Main HTML and Word history cards | Preview showed published A; downloaded bytes matched A's digest |
| Main Continue editing | Opened current B; reopening the old Word card still showed A |
| Side-session route | HTML and Word history showed A; Continue editing opened B for both |
| File collection | Canonical Document rows explicitly displayed Current working file |
| Historical HTML ZIP download | Actual UI download contained only `index.html` and `style.css`; the entry digest matched published A |
| Missing historical entry | Temporarily withholding only a fixture A blob produced an unavailable error; no current B substituted; the blob was restored |
| Browser without native file actions | Menu offered preview/download/ZIP; no claim that a server path was opened on the browser computer |
| Preview-lease capability disabled | Actual Chrome preview showed fixed A; UI download digest matched A |

Browser screenshots are retained under `.tmp/workspace-acceptance/`. The side
artifact check used its actual side-session route; it does not claim an embedded
side-panel click test. Profile evidence uses real tools and prompt assembly, not a
claim that an external model inferred the new profile correctly.

## Bounded resource measurements

One representative Windows fixture run used the final loader, prompt assembler and
ZIP exporter. These are observations, not performance thresholds or release claims.

| Rules fixture | Files read | UTF-8 bytes returned | Prompt increase |
| --- | --- | --- | --- |
| No rules | 0 | 0 | 0 characters |
| Root only | 1 | 71 | 1,067 characters |
| Three directory levels | 3 | 104 | 1,927 characters / 1,947 bytes |
| 220-character total content budget | 2 | 384 including lookahead | 2,171 characters; third file marked unread due to budget |

No-rules ordinary-turn execution made one local provider call. A non-HTML channel
delivery did not instantiate a ZIP exporter. No directory-tree scan or additional
model turn was added for either case.

Five fixed HTML materials totaling 230,776 bytes produced a 199,365-byte ZIP in
18.866 ms. Two concurrent held exports took 24.898 and 26.737 ms with a measured
398,730-byte temporary-file peak. Cleanup left zero temporary files. Self-contained
50-byte HTML retained its HTML filename and MIME type. Measurements exclude RSS,
immutable storage size, consumer hold time, cold-cache guarantees and remote upload.
The detailed record is `.tmp/final-resource-measurements.json`.

## Prompt changes and remaining evidence boundaries

Exact inserted/replaced text and injection locations are recorded separately:

- [P3: profile and memory sources](project-workspace-resources-p3-implementation.md).
- [P4: init and turn-start project rules](project-workspace-resources-p4-implementation.md).
- [P6: publication and channel receipt guidance](project-workspace-resources-p6-implementation.md).

P1, P2 and P5 have no model prompt changes. The explicitly authorized live-model
checks used only the prepared synthetic project, current product prompts and
source tools through the configured TokenRhythm provider (`deepseek-v4-pro-0813`).
New-guide creation succeeded with 42 lines and only an AGENTS write. Incremental
editing initially stopped on a provider overload without changing the file. After
the user authorized further API calls, the next attempt preserved every existing
nonempty line, added only the focused test command documented in README, and read
the result back. All seven input evidence files retained their hashes. The updated
guide then appeared in local main/side prompt assembly. Across both successful
cases and the retained failed attempt there were 15 provider requests and 23 tool
calls. The evidence is
`.tmp/provider-init-review-1792258724865846700/results/summary.json`.

The new guide's phrase "does no validation itself" is less precise than "does no
additional domain validation" because Python float conversion can reject input.
The original model output is retained, not silently corrected. Project commands
were documented but deliberately not executed during read-only init exploration.
These two fixtures establish the observed behavior, not perfect guide accuracy
for arbitrary repositories. No extra prompt wording was added in response.

Final main-integration checks and the Chrome capability-disabled path passed.
The capability fixture returns HTTP 501 only for preview-lease creation and uses
real artifact/download routes; it is not an old Gateway binary. The in-app browser
showed a blank Blob iframe on this fixture, whereas Chrome displayed the same A
correctly. This browser-specific discrepancy is retained without asserting its
root cause or making a speculative product change.

There is no real channel-recipient delivery evidence, native application click
acceptance, packaged Desktop build result or physically remote host acceptance.
Adapter/bridge tests, source browsers, live providers and packaged/native results
are separate evidence levels. Platform-specific skips require suitable CI/runtime
coverage and are not silently counted as passing.
