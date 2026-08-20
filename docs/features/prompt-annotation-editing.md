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
native protocol-v3 annotation bridge; browser-hosted Web UI and older or
partial Desktop shells fail closed. This is not a general comment system, a
browser automation surface, or an Office editor.

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
6. Annotated turns expose exactly four context-bound tools:
   `document_inspect`, `document_read`, `document_locate`, and `document_apply`.
7. Inspection, reading, locating, and proposal preparation remain free of
   durable mutation writes. The service admits at most one commit for the turn;
   a successful commit produces one revision and one change set.

`document_inspect` returns the ordered instructions, a bounded document
summary, adapter capabilities, and safe initial mutation grants.
`document_read` provides paged source or a semantic structure view but never
grants edit authority. `document_locate` asks the active format adapter to
locate one selected semantic target and returns an opaque, turn-scoped grant.
`document_apply` submits the grants chosen for mutation as one atomic proposal.
An instruction may be answered without a mutation; every mutation that is
submitted must still use a grant bound to its own selected context.
The HTML adapter supports the semantic operations `replace_text`,
`set_attribute`, `remove_attribute`, `set_style`, and `remove_node`.

The model never calculates or submits source offsets and never receives a
workspace path, anchor ID, DOM proof, internal document ID, or raw editable
range. It asks `document_locate` for a semantic operation, then supplies only
the opaque grant and the operation-specific `input` requested by that grant;
the server-side `DocumentFormatAdapter` derives and validates the exact source
replacement.
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
not a tool-protocol version. Restricted annotated turns cannot widen the exact
four-tool ceiling.

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
- The mutation outcome and one `tools=[]` authoritative summary round are armed
  only after the first valid `document_apply` intent is observed.
- One agent turn can advance the document head once. A failed edit or validation
  leaves the head unchanged.
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
  protocol-v3 annotation bridge method required by the flow is present at app
  startup. Web and incomplete Desktop bridges default to `false`.

The V1 UI does not expose a Publish action. Users edit the document head and
can inspect Versions and Changes; immutable publication remains a separate
service lifecycle rather than a promise of this editing surface.

The annotation UI also requires all of the following runtime capabilities:

- the application Artifact Workbench is enabled;
- the current document independently advertises `selectionContext = true`,
  `agentEdit = true`, and `promptAnnotations = true`;
- the artifact is a supported single-file UTF-8 HTML document;
- an Electron native workbench surface and the v3 artifact bridge are active;
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
| Direct | Uses the user's fixed model. | A verified tool-capable model may answer or mutate. For an unverified model, document tools are hidden and the model may only answer from the selected context; no mutation lifecycle starts. |
| Router | Applies deterministic artifact floors after classification. | A selection edit has a minimum of `c2`; a multi-element/structural edit has a minimum of `c3`. Budget and fallback policy may move upward but cannot cross below the effective floor. Missing capable tiers fail closed. |
| Ensemble | Runs the configured B5 lineup. | Proposers receive the annotation context but no executable tools. Only the Aggregator receives and may call artifact tools. For mutation turns, proposer tools are forced off, single-model fallback is removed, and an unverified Aggregator fails before provider execution. |

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
WebSocket lifecycle (preview, materialization, EditSession save, exact-four agent edit,
backend publication-journal and immutable-source checks) with the real Electron
native surface suite. Its user-journey fixture starts the current Vue UI and
owned Gateway, generates synthetic HTML as one editable file, selects through the
native picker and trusted overlay, applies exactly one Agent change, observes
Preview plus Versions/Changes refresh, and proves an answer-only follow-up adds
no durable write. It is credential-free and requires Electron foreground focus;
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
runs the owned Gateway/provider path and requires the exact-four tool surface.
It does not replace the separate real-Electron selection gate.
Each successful mutation case in Direct or Router uses three physical
requests: bounded inspection/location, the admitted commit proposal, and a
final `tools=[]` outcome response. A B5 Ensemble case uses the corresponding
three five-member rounds. The outcome-finalization round is required Agent work; it must not be
removed or treated as a free call. The approved matrix therefore expects 42
physical provider calls, allows a bounded worst-case budget of 63, and
hard-stops at 64. A build is not release-ready until the following matrix is
verified end to end:

- Direct: one annotation and a two-annotation batch using a verified
  tool-capable fixed model;
- Router: a single selection at `c2`, a structural batch at `c3`, and no
  fallback below the effective floor;
- Ensemble: the full configured lineup, zero executable proposer tool calls,
  and one Aggregator-owned admitted `document_apply` commit;
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
- requiring authoritative tool-capability metadata before any Direct model or
  Ensemble Aggregator may mutate a document; unverified models remain
  answer-only with document tools hidden;
- keeping every new test in the Windows shard assignments and duration data;
- exercising migrations from released wheels and old profiles;
- preserving opaque bridge handles and typed protocol methods when protocol v3
  evolves;
- running a real Electron corpus on every supported platform before release;
- reviewing limits and garbage collection without weakening immutable
  revisions, CAS, fencing, or transcript replay safety.

---

[Artifacts and media](../artifacts-and-media.md) · [Router](squilla-router.md) · [Ensemble](LLM-ensemble-design.md) · [Docs index](../README.md)
