# Prompt-Annotation Editing

This page is the maintainer and operator guide for source-backed artifact
annotations. The feature lets a user select an element in an HTML preview and
attach trusted document context to the next chat message. The active agent may
answer from that context without writing the document, or apply selected edits
as one reversible change set when the request requires a mutation.

## User experience

An HTML file has one identity and a visible version history. Opening it shows
the current page; Source, Versions, and Changes are adjacent views of that same
file. A user never imports a working copy or chooses between a generated file
and an editable file.

Annotations follow the page. If the user or OpenSquilla changes the HTML after
an annotation is created, the Gateway resolves the instruction against the
current page when the message is sent. A uniquely identified element remains
an exact target. If the element moved, was rewritten, or can no longer be
identified uniquely, the instruction is still sent as bounded page context so
the model can attempt a safe current-page match. One unresolved annotation
does not prevent other exact annotations in the same message from being
applied. The original generated/downloadable file and every prior version stay
unchanged in history.

Versioned HTML resources are enabled by default. Source-backed DOM annotations
default on only in Electron builds that synchronously expose the complete
native protocol-v4 annotation bridge, including the candidate-preview
lifecycle; browser-hosted Web UI and older or partial Desktop shells fail
closed. A protocol-v3 shell may retain source-only editing compatibility, but
cannot claim autonomous visual verification. This is not a general comment
system, a browser automation surface, or an Office editor.

## Architecture and invariants

The request path is deliberately narrow:

1. The Electron main process selects a DOM element through the Chrome DevTools
   Protocol (CDP) overlay without modifying the artifact DOM.
2. A trusted, sandboxed application overlay collects the instruction. The
   instruction is never inserted into the preview page.
3. The Gateway maps the runtime element path and a source-backed proof of the
   selected element's ancestor chain back to one exact opening-tag span in the
   canonical UTF-8 source.
4. A durable `draft` annotation is bound to the session, document, immutable
   revision, and anchor.
5. `chat.send` accepts the user message and ordered annotation snapshots in the
   same SQLite transaction. A failed compare-and-swap accepts neither.
6. Annotated turns expose a ten-tool context-bound surface: the five source
   tools (`document_inspect`, `document_read`, `document_locate`,
   `document_apply`, and `document_patch`), four bound-preview tools
   (`document_browser_inspect`, `document_browser_act`,
   `document_browser_screenshot`, and `document_browser_reload`), and the
   lifecycle tool `document_finish`.
7. Inspection, reading, locating, and source writers remain free of durable
   revision writes. Writers stage one draft candidate; only
   `document_finish(commit)` publishes one revision and one change set.

`document_inspect` returns the ordered instructions, a bounded document
summary, adapter capabilities, and safe initial mutation grants.
`document_read` provides paged source or a semantic structure view but never
grants edit authority. `document_locate` asks the active format adapter to
locate one selected semantic target and returns an opaque, turn-scoped grant.
`document_apply` submits the grants chosen for mutation as one atomic proposal.
`document_patch` is the exact-source writer for instructions that cannot be
expressed by grants, including insertion, outer-structure changes, global CSS,
and scripts. It edits only the current bound Document and accepts no filesystem
path. An instruction may be answered without a mutation.
The HTML adapter supports the semantic operations `replace_text`,
`set_attribute`, `remove_attribute`, `set_style`, and `remove_node`.

The model chooses at most one source writer for each response. If every
requested change has a suitable grant, it uses one `document_apply`; otherwise
it reads the required source pages and submits all changes together in one
`document_patch`. It must not call both writers in one response. Each writer
stages a candidate and keeps the loop alive. The model may inspect the bound
preview, repair the candidate, and inspect again across iterations. A fresh
verification receipt and matching candidate SHA are required before
`document_finish(commit)`; `document_finish(discard)` explicitly abandons the
candidate without changing the canonical head.

`document_browser_screenshot` keeps the PNG out of the JSON tool text. When the
selected model explicitly supports vision, the bounded image is delivered as
an ephemeral user image block after the tool result; text-only or ensemble
legs receive only the dimensions/status and can continue with DOM, console,
and bounded browser actions. The image is not written to the workspace or
persisted in the transcript.

The model never calculates or submits source offsets and never receives a
workspace path, anchor ID, DOM proof, or internal document ID. Semantic edits
use only opaque grants. Source fallback uses the current source SHA plus exact,
uniquely matching `expectedText`/`replacement` edits. The server rejects stale
SHA values, missing or repeated matches, overlaps, no-ops, invalid HTML, and
partial proposals before publication.
`replace_text` is escaped as HTML text, opening-tag changes preserve unaffected
source, and `remove_node` removes either a proven balanced element range or one
proven HTML void element such as `img`. Unsupported or ambiguous structures
fail closed instead of falling back to fuzzy matching or model-written source
spans.

The opaque grant wire token is a random 256-bit `hrg_` value. A grant is bound
to the current task, session epoch,
document, revision, source SHA, verified range hash, semantic operation, and
annotation orders. Stale, expired, reused, duplicate, selection-unbound,
mismatched, or overlapping grants reject the entire writer call before
candidate publication, ChangeSet creation, or Revision creation. ChangeSet
audit data contains only hashes and character counts, never grant tokens or
source fragments.

There is one prompt-annotation tool contract and no client-selectable protocol
version. Accepted and replayed responses report the accepted annotation IDs,
not a tool-protocol version. Restricted annotated turns cannot widen the
ten-tool ceiling or access workspace mutators.

The following contracts must remain true:

- Revisions, anchors, audit events, and sent annotation snapshots are immutable.
- A document head advances only through an expected-head/state-revision
  compare-and-swap. Writer leases use fencing tokens.
- Edit sessions retain only editor baseline and lifecycle state. They never
  retain writer authority; each manual save acquires and releases one short
  writer lease around its commit.
- A send batch contains at most 16 annotations and belongs to one session and
  document. Draft targets are normalized to the current head during turn
  acceptance.
- An instruction is limited to 16 KiB of UTF-8 data. The rendered active-turn
  context is limited to 64 KiB.
- A changed head deterministically remaps remaining drafts during acceptance.
  Unique matches become current exact targets; missing or ambiguous matches
  become contextual targets. Cross-session or mismatched document ownership
  still fails before any provider call.
- The active turn receives the bounded instruction and source quote. Later
  turns receive only an inert historical marker, so an old instruction cannot
  silently run again.
- A context-only answer does not arm the mutation ledger, create a mutation
  outcome, reserve a summary round, or require a second provider request.
- A candidate draft is not a durable mutation outcome. The model may claim the
  page was updated only after `document_finish(commit)` returns an applied
  result; timeout, cancellation, or discard rejects an uncommitted draft.
- One agent turn can advance the document head once. A failed edit or validation
  leaves the head unchanged, while additional candidate repairs replace the
  same draft without advancing generation.
- The HTML adapter validates the selected semantic operation, attribute or
  inline-style value, source-range proof, source-preserving candidate, and a
  bounded HTML structure scan before publication. It does not certify visual
  correctness, external stylesheet behavior, or script semantics; those checks
  remain separate release gates.
- The stable document card continues to identify the document while its latest
  download resolves to the current head. A whole agent change set can be
  reverted.

## Capability defaults and runtime gates

The renderer resolves two independent feature defaults in
`opensquilla-webui/src/stores/app.ts`:

- `documentWorkbenchResources` defaults to `true` and enables resource
  discovery, HTML preview, silent legacy materialization, and versioned editing;
- `artifactPromptAnnotations` defaults to `true` only when the client is
  Electron Desktop and every native surface, preview-lease, screenshot, and
  protocol-v4 annotation bridge method required by the flow is present at app
  startup. Web and incomplete Desktop bridges default to `false`; a v3 bridge
  can use the source-only compatibility path but is not eligible for the
  autonomous visual loop.

The V1 UI does not expose a Publish action. Users edit the document head and
can inspect Versions and Changes; immutable publication remains a separate
service lifecycle rather than a promise of this editing surface.

The annotation UI also requires all of the following runtime capabilities:

- the application Artifact Workbench is enabled;
- the current document independently advertises `selectionContext = true`,
  `agentEdit = true`, and `promptAnnotations = true`;
- the artifact is a supported single-file UTF-8 HTML document;
- an Electron native workbench surface and the v4 artifact bridge are active;
  v3 remains a source-only compatibility path;
- selection resolution, focus, and trusted-overlay capabilities are available.

Browser-hosted Web UI retains the HTML Workbench but does not offer DOM
selection. Where annotation context is relevant it presents a Desktop-required
hint instead of a non-functional picker.

There is intentionally no end-user setting for these safety boundaries. An
operator or test may provide `window.OPENSQUILLA_FEATURES` before the app store
is created. Overrides are applied last, so an explicit `false` is the emergency
kill switch even on a complete Desktop bridge:

```html
<script>
  window.OPENSQUILLA_FEATURES = {
    ...(window.OPENSQUILLA_FEATURES || {}),
    artifactPromptAnnotations: false,
  }
</script>
```

The value is read when the app store is created. Setting it in the console
after the application has booted does not change the current store. The
override is an operational/testing boundary, not a persisted user preference.

## Supported and unsupported inputs

The initial supported surface is deliberately small:

- Electron Desktop only;
- a single-file `.html`/`.htm` Document generated in a session or materialized
  from an older attachment or deliverable;
- strict UTF-8 source of at most the editor limit;
- one or more top-frame DOM elements whose path, tag, attributes, and ancestor
  identity map uniquely to opening tags in the canonical source;
- manual source editing, annotation-driven agent editing, history, change-set
  review, and whole-turn revert.

The feature fails closed for:

- DOCX, XLSX, PPTX, PDF, and legacy Office formats;
- HTML bundles, project directories, Vue/React/Vite runtime trees, and HMR;
- browser-hosted Web UI selection;
- iframe or shadow-DOM nodes, pseudo-elements, text ranges, canvas pixels,
  video regions, and image-coordinate selections;
- runtime-only selected elements, or selected elements whose path, opening-tag
  attributes, or ancestor identity no longer match source;
- non-UTF-8 HTML, ambiguous source mappings, and unsupported visual selection;
- general browser use or arbitrary JavaScript/CDP execution by a model.
- JavaScript source grants or script editing. These remain unsupported until a
  bounded JavaScript parser and candidate validator are connected.

Unsupported documents remain downloadable. A preview is available only when
that format's independent `preview` capability is true; selection and edit
capabilities are never inferred from preview support.

## Direct, Router, and Ensemble semantics

All three modes receive the same accepted annotation snapshots and use the
same context-bound tool implementations.

| Mode | Model policy | Mutation policy |
| --- | --- | --- |
| Direct | Uses the user's fixed model. | A model with unknown or unverified capability provenance receives the same authorized document tools. Only an explicit `supports_tools = false` declaration rejects the mutation before provider execution. |
| Router | Applies deterministic artifact floors after classification. | A selection edit has a minimum of `c2`; a multi-element/structural edit has a minimum of `c3`. Budget and fallback policy may move upward but cannot cross below the effective floor. Missing capable tiers fail closed. |
| Ensemble | Runs the configured B5 lineup. | Proposers receive the annotation context but no executable tools. Only the Aggregator receives and may call artifact tools. For mutation turns, proposer tools are forced off and single-model fallback is removed. An unknown or unverified Aggregator is allowed; an unready Aggregator or one explicitly declaring `supports_tools = false` fails before provider execution. |

Proposer output is advisory text. It never advances the document head. The
Aggregator must independently submit the mutation proposal through the normal
registry, permission, validation, lease, and compare-and-swap path. Only an
admitted commit may advance the head.

## Persistence and migrations

Four additive migrations provide the durable substrate:

- `V037__artifact_sessions` creates documents, immutable revisions, change
  sets, anchors, writer leases, edit sessions, and audit events. It also adds
  immutability triggers and document/turn indexes.
- `V038__artifact_prompt_annotations` creates durable annotation drafts with
  `draft`, `sent`, and `discarded` states. It depends on V037 and enforces body,
  send-linkage, session, document, and revision indexes.
- `V039__artifact_mutation_attempts` adds the durable, proposal-bound commit
  receipt used for idempotency and restart reconciliation.
- `V040__document_resources` adds source bindings plus import and immutable
  publication journals for Workbench resources.

Before an upgrade, take the normal profile/database backup and verify it is
readable. Migrations must be exercised from both a fresh database and the
oldest supported upgrade database. Do not manually delete the tables or run
down migrations on a profile that may contain artifact history: V037 rollback
deletes annotation drafts and V035 rollback deletes artifact revision history.

The operational rollback is to turn the feature gate off while retaining the
additive schema. If a binary downgrade is required, restore a compatible
pre-upgrade profile backup instead of attempting ad hoc SQL surgery.

## Trust boundaries

- The artifact page is untrusted content in an isolated workbench surface. It
  receives no OpenSquilla credentials, Node/Electron API, local file access, or
  system-browser login state.
- Annotation input is application-owned UI in a separate sandboxed
  `WebContentsView`. It has no network, navigation, popups, DevTools, or Node
  integration and exposes only typed draft/submit/cancel messages.
- The Electron bridge listens only on loopback, uses a random per-launch
  bearer token, implements a fixed protocol, bounds requests and responses,
  and never exposes raw CDP methods, expressions, URLs, or surface identifiers
  to a model.
- Desktop derives the active preview's immutable artifact identity from the
  Gateway-authorized preview lease, never from annotation parameters supplied
  by the renderer. Selection resolution and later focus both require that
  identity to match the active document before any anchor or draft is written.
- The renderer sends an opaque selection handle. The Gateway rereads the
  current head and validates the selected element's source-backed ancestor
  proof, unique path, source SHA, opening-tag boundaries, anchor, session epoch,
  and revision before creating or consuming a draft. Text, descendants, and
  unrelated DOM branches are deliberately excluded from the proof, so benign
  runtime updates elsewhere do not invalidate an otherwise exact selection.
- Artifact tools are owner-only, interactive Web/Desktop capabilities. Guest,
  channel, cron, reviewer, subagent, and nested-agent callers cannot mutate the
  document.
- Model-facing tool schemas contain no local path, session/document ID, bridge
  token, CDP node ID, source offset, anchor/locator proof, raw XML/HTML patch,
  or arbitrary JavaScript argument. The model requests bounded semantic
  operations through the active document adapter. Opaque range grants are
  scoped to one turn and cleared at the terminal turn finalizer.
- Router telemetry is content-free: it records only enumerated artifact format,
  operation class, and minimum tier. It must not record names, instructions,
  source quotes, locators, or durable IDs.

## Verification

Run all commands from the repository root unless the command changes directory.

### Offline backend contracts

```sh
uv run pytest -q \
  tests/test_artifact_session \
  tests/test_migrations/test_v037_artifact_sessions.py \
  tests/test_migrations/test_v038_artifact_prompt_annotations.py \
  tests/test_migrations/test_v039_artifact_mutation_attempts.py \
  tests/test_migrations/test_v040_document_resources.py \
  tests/test_gateway/test_artifact_tool_context.py \
  tests/test_gateway/test_desktop_artifact_bridge.py \
  tests/test_gateway/test_prompt_annotations.py \
  tests/test_gateway/test_rpc_artifact_editing.py \
  tests/test_engine/test_artifact_execution_policy.py \
  tests/test_engine/test_artifact_routing_policy.py \
  tests/test_engine/test_artifact_ensemble_policy.py \
  tests/test_session/test_artifact_session_lifecycle.py \
  tests/test_tools/test_artifact_range_grants.py \
  tests/test_tools/test_document_format_adapters.py \
  tests/test_tools/test_document_editing_tools.py
```

Also run the repository quality and packaging gates:

```sh
uv run ruff check src migrations tests
uv run mypy src/opensquilla --show-error-codes
uv run pytest -q tests/test_ci/test_migrations_packaged.py
uv build --wheel
```

### Web UI and real Electron

```sh
cd opensquilla-webui
npm run test:unit
npm run typecheck
npm run build
```

```sh
cd desktop/electron
npm run test:desktop-workbench
npm run test:offline-document-workbench-e2e
```

The desktop suite must exercise a real Electron process, not only mocked
renderer APIs. Release certification must cover hover/click interception,
trusted-overlay z-order, IME and keyboard actions, autosave/restart recovery,
focus, navigation/crash cleanup, one-revision refresh, and whole-turn revert on
the supported operating-system matrix. It must also prove that unrelated
runtime DOM mutations (including a document larger than the retired whole-DOM
limit) do not block an exact selection, while a changed selected element,
changed ancestor, wrong active artifact, or runtime-only path still fails
closed before draft persistence.

The offline document Workbench gate additionally composes an owned-Gateway
WebSocket lifecycle (preview, materialization, EditSession save, autonomous
document-agent edit loop, backend publication-journal and immutable-source checks) with the real Electron
native surface suite. Its user-journey fixture starts the current Vue UI and
owned Gateway, generates synthetic HTML as one editable file, selects through the
native picker and trusted overlay, stages and verifies one candidate (repairing
it when needed) before one final Agent commit, observes Preview plus
Versions/Changes refresh, and proves an answer-only follow-up adds no durable
write. A discarded or interrupted candidate creates no revision. It is
credential-free and requires Electron foreground focus;
a locked or background-only macOS session fails the gate.

### Live provider certification

Keep provider credentials only in the process environment or an ignored local
environment file. Never put a key in a command, fixture, report, or committed
configuration. A sanitized provider/Gateway prerequisite can be run with:

```sh
uv run python scripts/live_provider_profile_gateway_e2e.py \
  --providers tokenrhythm \
  --output "${TMPDIR:?}/opensquilla-provider-gateway.json"
```

That script certifies provider transport and accounting; it is not by itself
PromptAnnotation certification. For every release that changes this path, an
isolated owned Gateway and Desktop build must additionally pass this live matrix:

The dedicated certification boundary can be checked with:

```sh
uv run python scripts/live_artifact_prompt_annotations_e2e.py \
  --output "${TMPDIR:?}/opensquilla-prompt-annotations.json" \
  --confirm-live-cost \
  --confirm-rotated-key \
  --execute-live-matrix
```

Without `--execute-live-matrix`, the command is an intentional zero-call dry
run that writes `certification=incomplete`. With the flag, the isolated worker
runs the owned Gateway/provider path and requires the ten-tool document-agent surface.
It does not replace the separate real-Electron selection gate.
The fixed Direct `glm-5.2` source-fallback case must exercise a repair loop:
`document_inspect → document_read → document_patch →
document_browser_inspect → document_browser_screenshot` (or a bounded browser
action) `→ document_patch → document_browser_inspect → document_finish(commit)
→ tools=[]` finalization. The model chooses whether to take each observation,
repair, or discard step; the harness checks this representative sequence but
does not impose a PromptAnnotation-specific iteration count. The live harness
therefore uses a 120-second per-case deadline; this is a task deadline, not a
provider retry. A B5 Ensemble case uses the configured proposer/Aggregator
rounds while keeping proposer and finalizer tools empty. The outcome-finalization
round is required Agent work; it must not be removed or treated as a free call.
The approved autonomous-loop matrix therefore reserves 42 baseline physical
provider calls, allows a bounded worst-case reservation of 63, and hard-stops
raw provider traffic at 64. Preview, browser action, `document_finish`, and
tools-disabled finalizer requests are all included in that physical count;
none is deducted or treated as free work.
A build is not release-ready until the following matrix is
verified end to end:

- Direct: one source-fallback insertion and a two-annotation semantic batch
  using fixed model `glm-5.2`;
- Router: a single selection at `c2`, a structural batch at `c3`, and no
  fallback below the effective floor;
- Ensemble: the full configured lineup, zero executable proposer tool calls,
  an Aggregator-owned candidate loop, and one final `document_finish(commit)`;
- rejection cases for stale head, cross-session draft, DOM mismatch, and visual
  selection, each with zero provider calls;
- exactly one revision and one change set for every successful batch, plus a
  successful whole-turn revert.

Store only case name, mode/tier/model, tool name and count, content hashes, and
boolean results. Scan the report and temporary directory for credentials and
delete the temporary data after review. If this feature-specific live matrix
has not been completed, set the release override for
`artifactPromptAnnotations` to `false`.

## Release, rollback, and maintenance

For each release:

1. Run the offline, packaged-wheel, Web UI, and real Electron suites against
   the release candidate.
2. Complete the live Direct/Router/Ensemble matrix with an isolated profile.
3. Canary the exact Desktop build and watch sanitized audit events,
   annotation remap outcomes, validation failures, and orphan cleanup.
4. Keep the default enabled only while the one-turn/one-change-set, zero-call
   rejection, and Aggregator-only mutation invariants remain true.

For an incident, apply the explicit `artifactPromptAnnotations: false` override
first, fence active annotation sessions, and restart the affected
Desktop-managed Gateway. Existing document heads, revisions, downloads, and
sent history remain readable. Restore a prior head through the
revision/change-set service; do not overwrite artifact blobs or edit migration
tables manually.

Ongoing maintenance should include:

- rerunning the DOM-path/parse5 golden corpus after Electron, Chromium, parse5,
  or Monaco upgrades;
- treating tool-capability provenance as routing and diagnostic metadata:
  unknown or unverified Direct models and Ensemble Aggregators retain their
  authorized tools, while explicit `supports_tools = false` declarations are
  rejected before provider execution;
- preserving the actual mutation safety boundary in registry visibility,
  dispatch authorization, argument validation, writer leases, and atomic
  compare-and-swap commit;
- keeping every new test in the Windows shard assignments and duration data;
- exercising migrations from released wheels and old profiles;
- preserving opaque bridge handles and typed protocol methods as protocol v4
  evolves, with v3 retained only for source-only compatibility;
- running a real Electron corpus on every supported platform before release;
- reviewing limits and garbage collection without weakening immutable
  revisions, CAS, fencing, or transcript replay safety.

---

[Artifacts and media](../artifacts-and-media.md) · [Router](squilla-router.md) · [Ensemble](LLM-ensemble-design.md) · [Docs index](../README.md)
