# HTML preview annotations

HTML previews support selecting a region and adding a request to the normal chat
composer. The request includes a page reference, an optional document reference,
and a short selection description. A captured preview image uses the same upload
and attachment pipeline as a reference image added by the user.

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
