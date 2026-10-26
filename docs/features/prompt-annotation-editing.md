# Preview annotations

HTML, DOCX, PPTX and XLSX use **Annotate changes** in the preview toolbar.
The card next to the selected element uses the application's theme. Enter records
the instruction and resumes selection; Shift+Enter inserts a newline. IME
composition never records a draft. The card arrow also
**records into the existing chat composer**. Only the chat Send action submits or
queues the instructions. Recording never calls the send command.

## Entry points and preparation

Chat cards, paths, workspace files, reference cards and deliveries prepare a
working Document through `workbench.resources.open` with `intent: "annotate"`.
The request uses either a resource reference or a workspace file with its binding
and expected revision. Read-only workspace resolution can report `documentTarget`
and annotation availability; it never creates a document or advances a version.

An immutable delivery opens its associated current Document, or creates an
editable working copy using the existing import rules. The original delivery
remains unchanged. If the version differs, the user sees a notice before selecting
the target. A workspace file is registered in place, without copying it or guessing
associations from its name. HTML subpages must belong to an authorized bundle.

Preparing annotations validates the actual source. External edits create a new
baseline only at this explicit preparation step. Normal preview, `edit-current`,
send and execution do not refresh the baseline. Send and execution reject an
outdated version or source and preserve the instructions for reselection.

## Drafts and selection

Draft identity is the session and canonical Document, independent of its opening
entry point. HTML sends annotations for the current page; Office sends annotations
for the entire file. Other HTML pages retain their drafts. The chat composer is
the only annotation list; the preview has no duplicate bottom summary or record
button. Composer annotations support locating, editing and deleting. Opening an
Office annotation also opens its nearby card, where the target can be reselected.
Reselection replaces the target
atomically with a new ID, preserving the instruction and order.

Office adapters report the final selection and update its temporary display
anchor during scroll, zoom and layout. Locating a draft distinguishes exact,
location-only, unavailable and stale results. Equal text alone never identifies
the original target. Display coordinates are not persisted or sent to the Agent.
The card remains reachable at the preview edge when its target leaves the viewport.

Word supports clicking a paragraph and dragging a precise text range. A paragraph
uses the renderer's source identity, not a search for the first matching text.
Opening the card pauses picking while preserving the committed target, highlight
and geometry tracking. Recording or canceling releases that target.

Office and the version 2 native HTML editor share a placement function. Cards
prefer the space below the visible target, then flip above or to either side.
Cross-axis shifting keeps them inside the preview without moving them to an
unrelated corner. A valid existing side is retained while typing. Scrolling and
zooming recompute the anchor; a target outside the viewport docks the card at the
nearest edge. Office cards show the target summary when exact positioning is
unavailable. Mouse movement alone does not reposition an open editor.

Canceling an existing edit restores its opening text; canceling a new selection
removes only that draft. Closing or switching files saves text first. A storage
failure retains the editor for recovery. Old drafts are preserved; unverifiable
versions require reselection.

The existing persistent chat queue owns queued batches. Queued annotations cannot
be edited until cancellation succeeds. Generic take-back shortcuts skip annotation
items; **Cancel sending and restore annotations** restores their full context under
new draft IDs. Receipt cleanup compares the original target and body so a delayed
receipt cannot remove a replacement or a newer edit.

A batch contains at most 16 annotations, each text field is at most 16 KiB UTF-8,
and the normalized page context is at most 64 KiB. Preflight rejects oversized
batches without truncation or automatic splitting.

## Client compatibility

Office annotation works in Web and Desktop when the feature is enabled. HTML
element selection requires Desktop. New Desktop advertises
`overlayEditorVersion: 2` for the compact native editor, dynamic height and explicit
record action. Legacy `submit` remains a single-record action; older Desktop keeps
its existing editor. The chat composer provides the batch workflow.

This change adds no database migration or second execution queue. Old Office
formats, HTML element selection on Web, historical-version branching and batches
spanning HTML pages remain outside this release.

## HTML context

HTML previews support selecting a region and adding a request to the normal chat
composer. The request includes a page reference, an optional document reference,
and a short selection description. Selecting an element does not capture or
attach a screenshot. Users can add a reference image through the normal chat
attachment controls when visual context helps. Restored annotation drafts ignore
automatic screenshots saved by older clients.

Empty annotations remain editable and do not block sending text, attachments,
or completed annotations. Only annotations with instructions are included in a
send; an empty annotation alone does not create a message.

Annotations do not change the Agent's tool permissions, history, workspace,
skills, routing, or iteration policy. The shared Agent decides whether to read
files, edit them, inspect the page, or answer without making changes. Publishing
an artifact does not end the Agent loop.

## Current files and versions

Generated HTML stays associated with the source files that the Agent published.
Imported HTML receives a working copy of its complete resource bundle in the
session's workspace. The current preview reads those working files. Stylesheet,
script, and image changes refresh the existing desktop page, including when the
HTML entry file is unchanged. Publication preserves the selected resource
collection mode; opening a preview does not expose unrelated workspace files.

A successfully completed turn saves a version when the resource bundle changed.
Versions contain all collected resources and are deduplicated by the bundle's
content digest. Explicit publication uses the normal artifact service. Publishing
a selected historical revision preserves that revision's bytes.

Cancellation or a failed turn leaves actual files available for inspection and
continued work, without an automatic completion version. An explicit publication
that already succeeded remains part of history. A version-save failure is
reported as an error rather than a successful update.

Restoring a version moves the current pointer to that existing version and
restores its complete bundle. It does not append a version or remove later
historical versions. The current version can also be restored to discard
unpublished changes. Unrelated workspace files remain in place, and existing
unpublished bundle files are preserved in a recovery directory in the workspace,
outside the page's resource collection root. Historical versions and downloads
remain immutable. The Source view is read-only; Agent edits use ordinary
workspace file tools.

## Desktop browser

The built-in `browser` tool connects to the desktop application's existing page
through an authenticated loopback service. It supports listing and opening pages,
reading a snapshot, clicking, typing, selecting, scrolling, reloading, and taking
a screenshot. It can operate ordinary web pages independently of an artifact.

Targets are bound to a session and the lifetime of the actual page. Two pages at
the same URL have different references. Closing or recreating a page invalidates
its old reference. The tool cannot substitute a newly opened page for an existing
target. Plan mode permits observations and blocks browser mutations.

Screenshot bytes are transported as image input when the selected model supports
vision. Otherwise the tool result explicitly reports that the image is not
available to that model. The browser connection credential is private to the
owned Gateway and is removed from its environment before tool subprocesses start.

Playwright drives desktop acceptance tests. It is not required by the production
Agent's browser tool.

## Upgrade behavior

Upgrade retires the old HTML editor's pending execution state before accepting
new tasks. Historical migrations remain intact. The forward migration is
idempotent, preserves stored bytes and completed revisions, and does not guess
whether an ambiguous old write was committed. Ordinary import, publication,
restoration, and draft storage retain their recovery behavior.

Save any unsaved text in the old source editor before upgrading. That editor
kept unsaved buffers only in renderer memory; the migration preserves persisted
resources and cannot recover memory that was lost when the old client closed.

Old editor write RPCs return `DOCUMENT_EDITING_RETIRED`, with an instruction to
update the client and reopen the page. Historical annotations and accepted
message receipts remain readable. An old queued annotation is not silently sent
without its selection context: the updated client asks the user to select the
page again.

## Verification

Normal HTML and Office navigation follows an existing, authorized Document
relation to its current revision. Published cards, inline paths, workspace-tree
entries and reference cards reuse that Document's tab. Discovery is read-only:
it neither imports a document nor absorbs an externally edited file. The
explicit annotation preparation operation remains responsible for establishing
a new working baseline.

The file menu's "View this delivered version" and each version-history row's
"View this version" open separately labelled historical tabs. Those previews
retain the selected bytes, including the HTML bundle, and never follow working
source changes. Returning to the latest document reuses its existing tab.

The Files list and its header count share one canonical projection, grouped by
Document lineage or resource identity. A filename or content hash never merges
independent files. Authorized workspace-relative source paths distinguish
same-name files in the list and open tabs; host filesystem paths are not exposed.
Right-side annotation controls continue to record into the left composer; only
the left Send button dispatches a request.

Offline tests cover normal message acceptance and replay, attachments, shared
Agent behavior, complete resource versions, restoration, cancellation, target
identity, and upgrade recovery. Default tests do not call a paid provider.

Optional real-client acceptance uses `desktop/electron/scripts/live-html-journey.mjs`.
Its synthetic tasks exercise generation, annotation, and follow-up through the
composer and actual preview. The relay and HTTP transport in
`scripts/live_tokenrhythm_budget.py` and `scripts/live_tokenrhythm_transport.py`
preserve the official provider URL and normal product settings. Functional mode
records request timing and completion without enforcing price or spending limits;
provider calls can still incur charges. Optional budget mode requires reviewed
pricing and reserves cost before forwarding each request; it rejects unknown
prices.

Detection of stalled progress and repetition uses the shared Agent's configured
guards. The test driver records warnings and termination reasons without adding
its own Agent loop or prescribing a tool sequence. Credentials and runtime
evidence must remain outside the repository.
