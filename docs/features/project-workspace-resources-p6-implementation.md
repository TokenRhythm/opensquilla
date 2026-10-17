# P6 implementation and exact prompt changes

HTML publication and native channel delivery now use the same fixed materials.
Publication remains distinct from a confirmed channel receipt.

## Behavior and compatibility

- Explicit publication validates the captured HTML materials before registration.
  The conservative automatic backstop collects the same static dependency bundle
  for its existing eligible HTML candidates. It does not expand candidate discovery
  or convert ordinary WebUI previews into deliverables.
- Bundle digests include dependencies. Changing CSS without changing the entry HTML
  produces a different publication; previously registered material stays fixed.
  Single-file HTML publishes the exact validated bytes, without reopening its source.
- HTTP and channel export share `artifact_archives.HtmlZipExporter`. Its existing
  two-slot limit covers capture, validation, compression, consumption and cleanup.
  Cancellation joins filesystem work before cleanup. Multi-file HTML becomes a ZIP;
  self-contained HTML retains its filename and MIME type. ZIP bytes are deterministic
  for a fixed bundle. The path basename, declared filename, MIME and size agree.
- The channel outbox retains the legacy artifact delivery key and compares actual
  prepared-material metadata under that key. An incompatible old/new request is
  rejected without uploading again or borrowing a different material's receipt.
  Both upgrade and downgrade are covered, including explicit retryable rejection.
- A missing receipt, a legacy `None` return or an uncertain send exception stays
  unconfirmed. A confirmed receipt survives a later cleanup error. Existing receipt
  recovery, retry rules and durable states remain authoritative; no second outbox,
  spool, persistent ZIP cache or public website deployment is added.
- User-visible failures use fixed state messages and known safe material/limit
  explanations. Raw adapter reasons can contain request or credential data and are
  not echoed. Rejected authorization cannot become a download-URL fallback.
- WeCom's SDK file reader settles before a cancelled adapter returns, so the caller
  cannot remove temporary materials while its filesystem thread still reads them.

The content material/error value types move to `contracts/content_material.py`.
The existing application imports remain compatible re-exports. The shared builder
does not depend on the Gateway or channel implementations.

## Exact prompt changes

These are the Q4 changes in the approved plan. Q1/Q2 are recorded in the P4 document;
Q3 and profile/memory tool guidance are recorded in the P3 document. P1/P2/P5 add no
model prompt instructions.

### System delivery guidance

Location: `src/opensquilla/identity/templates/system_prompt.j2`, under
`Generated File Delivery`, in full mode with publication and file-authoring tools.

Replace the first delivery instruction with:

```text
When the user requests a file deliverable, prepare the final workspace file and its required local materials with `publish_artifact`.
```

Replace the unchanged-file instruction with:

```text
If `publish_artifact` reports that the same unchanged file or bundle is already registered, do not register it again. Publication prepares the deliverable; do not claim native channel delivery without a confirmed delivery receipt.
```

Ordinary HTML preview, explicit export intent, URL rules and existing Goal guidance
remain in their existing branches.

### Publication tool description

Location: `src/opensquilla/tools/builtin/artifacts.py`, the opening sentences of the
`publish_artifact` decorator's description:

```text
Prepare a fixed snapshot or bundle of an existing workspace file for delivery on the current surface. This does not deploy a website or confirm a channel send receipt.
```

The following workspace restrictions, bundle parameters and URL instructions remain.

### Publication result guidance

Location: `src/opensquilla/tools/builtin/artifacts.py::_publish_note`.

For `CallerKind.CHANNEL`, append to `final_response`:

```text
Channel delivery success is determined by its delivery receipt.
```

In the owner Web/CLI local-path sentence, replace `on this machine` with
`on the execution host`. The resulting sentence is:

```text
Mention the local_path as the local entry path when the user needs to open the generated file on the execution host.
```

### Workspace preview description

Location: `src/opensquilla/tools/builtin/workspace_preview.py`, final sentence of the
`open_workspace_preview` decorator's description. Replace the ambiguous instruction
to publish again with:

```text
Correct HTML input errors and call open_workspace_preview again after edits.
```

## Review and verification scope

Independent reviews found and corrected the single-file validation/write race,
capture outside the concurrency limit, unsafe adapter error text, missing-receipt
success classification, and cleanup incorrectly overriding a confirmed receipt.
Regression coverage exercises fixed bundle bytes, exact ZIP members and metadata,
material limits, upgrade/downgrade outbox records, cancellation and delayed reads,
and adapter-specific behavior with local doubles. No real channel message is sent
without an authorized destination. Source tests do not establish packaged desktop
behavior or real provider delivery.
