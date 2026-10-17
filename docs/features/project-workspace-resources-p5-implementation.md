# P5 implementation: fixed history and current working files

Published history now opens its original artifact. Continuing work on that file
requires the explicit **Continue editing** action. Existing Document operations,
publication transactions, source locations, and channel delivery remain unchanged.

## Resource identity and behavior

- The implementation reuses `WorkbenchResourceRef`, canonical `type:id` values,
  prepared-preview references, and `workbenchResourceType/workbenchResourceId`.
  A plain legacy artifact identity does not grant Document or Deliverable authority.
  Untyped legacy previews remain read-only.
- Historical chat cards and history navigation resolve the original Deliverable.
  Viewing does not call `openCurrent`, import a Document, or follow a related
  Document's later head. Missing original material fails visibly without replacing
  it with the current file.
- HTML retains the existing lease/bundle path and its platform capabilities. It is
  not universally wrapped in an offline-only prepared preview. Office retains the
  existing `artifactWorkbench.previews.createResource` path: authenticated binary
  loading, SHA-256 verification, and the local OOXML renderer. This phase does not
  add a `workbench.previews.create` RPC call to that path.
- Fixed previews ignore Document head events and do not expose Document source,
  revision, publication, or annotation mutation controls. Their file downloads,
  external single-file copies, and HTML ZIP export target the original artifact.
  A single external HTML file does not promise to include adjacent CSS or images;
  complete bundled use retains the existing ZIP export.
- **Continue editing** resolves the typed Deliverable and reuses the existing
  `workbench.resources.open(edit-current)` contract. The existing import fallback
  is available only after that explicit action. A related current Document can
  therefore open B while its historical card continues to represent A.
- Existing child authorization and parent Workbench visibility remain separate.
  Session, Gateway/profile, connection epoch, route, close, and tab-navigation
  guards retire stale responses. Switching away and back also retires an earlier
  Continue-editing response rather than allowing it to take focus.

## Files collection semantics

The collection keeps the existing `canonicalWorkbenchResources` lineage
aggregation. A server-declared related Document replaces its Deliverable row in
that navigation collection; unrelated files are not merged by name or digest.

| Visible resource | Label | Ordinary Open |
| --- | --- | --- |
| Aggregated Document | Current working file | Current Document head, including B |
| Independent Deliverable | Published version | Original artifact A |
| Attachment | Existing file metadata | Existing attachment/current-open behavior |

The labels are translated in all six supported locales. The collection's
Document aggregation does not change the original historical chat card. It also
does not add duplicate history rows or a new resource model. Workspace-preview
cards and the filesystem tree retain their existing current working-file behavior.

## Implementation locations

- `workbench/artifactItems.ts` classifies existing typed references and chooses
  fixed file-action targets.
- `views/ChatView.vue` preserves original Deliverable identity for history entry
  points and download actions, while retaining explicit workspace-preview routing.
- `components/workbench/artifactWorkbenchProvider.ts` pins rendering and actions,
  suppresses mutable Document behavior, and exposes Continue editing.
- `components/workbench/AppWorkbench.vue` preserves navigation authority and
  visibility, routes collection Deliverables to fixed previews, and reuses the
  current-open contract only for the appropriate explicit/current paths.
- `WorkbenchResourceCollectionPanel.vue` and its provider display current/published
  labels. `ArtifactDocumentPanel.vue` avoids calling a fixed download the latest
  version.

P5 changes no model prompt, tool description, RPC schema, backend storage layout,
garbage collection, migration, publication transaction, or channel outbox.

## Validation evidence and limits

- The final combined affected P2/P4/P5 frontend run passed **1,048 tests in 33
  files**. Coverage includes actual entry-point execution, mounted collection
  routing, child authority/parent visibility, current-Document compatibility,
  original-byte downloads and external copies, unavailable material, head events,
  and close/tab-switch/return races. This count is the combined affected set, not
  1,048 P5-only tests.
- Architecture, transport boundaries, configuration type checking, locale checks,
  and Vue type checking passed. The last Vue pass followed a test-only typed
  resource-constructor correction. The verified WebUI build contains **438 files**;
  its existing chunk-size and dynamic-import warnings remain nonfatal.
- Independent source review identified the collection Deliverable entry and
  Continue-editing focus race; both were corrected and independently retested.
  Overlapping independent runs are not added to the combined test count.
- Real local browser checks used an isolated Gateway and real SessionManager,
  ArtifactSessionService, publication RPC, and WorkingFiles. HTML A was published
  before saving current B. A valid DOCX A was published before the existing pinned
  Office candidate transaction committed B. No model provider or native channel
  was called to create these acceptance records.

| Real browser check reported by the integration runner | Result |
| --- | --- |
| MAIN HTML historical preview | A rendered |
| MAIN HTML historical download | Download SHA-256 matched published A |
| MAIN HTML Continue editing | Current B rendered |
| MAIN Office historical preview | DOCX A rendered |
| MAIN Office historical download | Download SHA-256 matched published A |
| MAIN Office Continue editing | Current B rendered |
| MAIN Office old card reopened after Continue editing | Original A rendered again |
| SIDE session route opened by its direct key: HTML historical preview | A rendered |
| SIDE session route opened by its direct key: HTML Continue editing | Current B rendered |
| SIDE session route opened by its direct key: Office historical preview | DOCX A rendered |
| SIDE session route opened by its direct key: Office Continue editing | Current B rendered |
| SIDE HTML ZIP downloaded through the UI | Exactly `index.html` and `style.css`; entry SHA-256 matched published A |
| MAIN HTML historical card with original A bytes temporarily unavailable | Explicit unavailable error; no Workbench or current-B substitution |
| Chrome with preview-lease creation disabled (HTTP 501) | Compatibility preview rendered A; UI download SHA-256 matched A |

The side-session checks above used direct session-route navigation with its real
child key; they do not establish the embedded side-panel lifecycle or parent-panel
presentation. For the missing-material check, only the isolated fixture's A blob
was temporarily renamed and then restored. Opening its historical card displayed
`This page is temporarily unavailable. Try again.` and did not open a Workbench
or substitute B. The capability-disabled fixture uses the real current Gateway
and storage with preview-lease creation returning 501; it is not an old binary.
The same fixture had a blank Blob iframe in the in-app browser but rendered A in
Chrome; that unexplained environment difference remains recorded. Physically
remote Gateway, old Gateway binary, packaged Desktop, native external opening,
and real channel delivery are not established by these browser checks.

Local evidence is recorded in `.tmp/p2-p4-p5-final-results.json`,
`.tmp/p5-final-navigation-results.json`, `.tmp/p5-final-typecheck.log`,
`.tmp/p5-final-vue.log`, and `.tmp/p5-final-build.log`. The isolated seed helper is
`.tmp/run_artifact_acceptance.py`; `.tmp/artifact-acceptance-current.json` identifies
its exact main/side records and A/B hashes. These are local acceptance artifacts,
not committed product fixtures or a new runtime subsystem.
