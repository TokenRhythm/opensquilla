# Attachment Drag-and-Drop Upload Spec

Date: 2026-06-29
Status: Draft

## Problem

OpenSquilla should let users attach files by dragging them into the chat surface
in both the browser Web UI and the Electron desktop client. The behavior must be
consistent across surfaces and must use the existing gateway attachment
ingestion path rather than adding a parallel desktop-only file pipeline.

The current code already contains most of the upload path:

- `opensquilla-webui/src/views/ChatView.vue` listens for drag/drop on the chat
  thread and passes dropped files to `addAttachment`.
- `opensquilla-webui/src/composables/chat/useChatAttachments.ts` validates file
  type and size, inlines small files, and stages large files through
  `/api/v1/files/upload`.
- `opensquilla-webui/src/composables/chat/useChatSend.ts` sends staged files as
  `{ type, file_uuid, mime, name }`.
- `src/opensquilla/gateway/uploads.py` exposes the multipart upload route and
  stores staged bytes behind an opaque `file_uuid`.
- `src/opensquilla/gateway/attachment_ingest.py` resolves `file_uuid` entries
  into transcript material references before a turn runs.
- `desktop/electron/src/main.ts` creates a sandboxed, context-isolated browser
  window; `desktop/electron/src/preload.cts` exposes only explicit desktop APIs.

The missing product-quality layer is a complete, reviewed drag-upload UX and
test contract that covers both browser and desktop surfaces, including auth,
error states, retry behavior, and cross-platform desktop validation.

## Goals

- Support dragging files from the OS/browser file picker into the Web UI chat
  surface.
- Support the same drag behavior in the Electron desktop client without a
  separate upload implementation.
- Preserve the existing attachment policy: max count, per-category size caps,
  total size cap, MIME sniffing, and staged upload TTL. Any file type is
  admitted; supported images retain canonical image input. Ordinary files
  expose filename, MIME, size, and a tool-readable workspace path; their contents
  enter model context only after the agent reads them with available tools.
  Internally archived long user input retains its existing bounded preview.
  Existing upload payloads and attachment history remain readable without a
  migration; workspace materialization and size limits remain unchanged.
- Make upload progress and failure states visible in the composer.
- Prevent sending while any attachment is still reading or uploading.
- Ensure token-authenticated gateways can upload files by using the same bearer
  token source as the WebSocket/RPC client.
- Keep raw local file paths out of renderer code and out of chat payloads.
- Persist only stable transcript material references after `chat.send` accepts
  the turn; do not persist `file_uuid` in transcripts.

## Non-Goals

- Do not implement folder drag-and-drop.
- Do not upload directory trees or recursively enumerate local paths.
- Do not expose arbitrary local filesystem reads through Electron preload.
- Do not make Electron bypass the gateway upload endpoint.
- Do not add resumable/chunked uploads in this iteration.
- Do not add per-type client-side gating: admission is any-type (opaque
  staging), and rejections happen only on size/count policy, mirroring the
  gateway's category router in `contracts/attachments.py`.
- Do not support remote URL drag/drop as file upload unless the browser supplies
  a real `File` object.

## Existing Surfaces

### Web UI

- `ChatView.vue` owns high-level chat surface events, including drag/drop and
  paste.
- `ChatComposer.vue` renders pending attachment chips and the file input.
- `useChatAttachments.ts` owns attachment state, MIME resolution, inline reads,
  staged uploads, and local validation.
- `useChatSend.ts` serializes pending attachments into the `chat.send` RPC
  payload.

### Gateway

- `contracts/attachments.py` defines allowed media types and size limits.
- `uploads.py` handles multipart staging and returns `file_uuid`.
- `attachment_ingest.py` validates inline and staged attachments, writes
  transcript material, and returns consumed staged UUIDs.
- `rpc_sessions.py` evicts consumed `file_uuid` values only after the turn has
  been accepted into the runtime.

### Desktop

- The Electron renderer runs the same Vue Web UI as the browser.
- The Electron window is sandboxed and context-isolated.
- The preload API currently exposes gateway status, settings, onboarding, and
  artifact open helpers; it does not expose raw file reads.

## Recommended Design

Use one shared Web UI upload path for browser and Electron:

```text
User drops files
  -> ChatView drop handler
  -> useChatAttachments.addAttachments(files)
  -> inline small files OR POST large files to /api/v1/files/upload
  -> ChatComposer renders pending chips
  -> useChatSend serializes attachments
  -> chat.send RPC
  -> gateway attachment_ingest resolves file_uuid
  -> turn runtime receives stable attachment refs
```

Electron should not get a special upload bridge in the first iteration. When a
file is dragged from the OS into the Electron browser window, Chromium exposes
it to renderer code as a `File`. That is the same object shape the browser path
uses, so the renderer can call the same composable.

Add a desktop-specific bridge only if manual validation proves a platform cannot
produce usable `DataTransfer.files`, or if a later feature explicitly needs
native behavior such as folder selection, revealing local file paths, or
watching local files for changes.

## User Experience

### Drop Zone

The primary drop zone is the chat thread/composer area.

Expected behavior:

- `dragenter` / `dragover` with at least one file item shows a visible drop
  affordance.
- `dragleave` uses a drag-depth counter or equivalent containment check so the
  affordance does not flicker when moving across child elements.
- `drop` extracts only `File` objects and ignores non-file drag data.
- Dropping files focuses the composer and appends attachments to the pending
  list.
- Unsupported items produce a toast and do not block valid files in the same
  drop.

### Attachment Chips

Each pending attachment chip should show:

- name;
- MIME or concise type label;
- size;
- state: reading, uploading, ready, failed;
- thumbnail for image inline attachments when available;
- remove button;
- retry button for staged upload failures.

Sending is disabled while any attachment is `reading` or `uploading`.

### Empty Message Behavior

If the user sends with attachments and no text, the existing fallback message
`Describe these attachments` remains acceptable.

The visible bubble should still show the user's display text when present and
attachment chips/previews when attachments exist.

## Attachment State Model

Use an explicit state machine in `useChatAttachments.ts`.

Recommended internal states:

| State | Meaning | Sendable |
| --- | --- | --- |
| `inline_pending` | FileReader is reading a small file. | No |
| `inline` | Base64 data is ready in the pending payload. | Yes |
| `uploading` | Multipart upload is in progress. | No |
| `staged` | Gateway returned a `file_uuid`. | Yes |
| `failed` | Read or upload failed and can be removed or retried. | No |

The public type may keep the existing names, but code paths should treat them as
states rather than ad-hoc variants.

`addAttachment(file)` should be implemented in terms of
`addAttachments(files: File[])` so file picker, paste, and drag/drop share batch
validation behavior.

## Data Contracts

### Inline Attachment Payload

Small files are sent through `chat.send` as:

```json
{
  "type": "image/png",
  "mime": "image/png",
  "name": "screenshot.png",
  "data": "<base64>"
}
```

### Staged Upload Request

Large files are uploaded first:

```http
POST /api/v1/files/upload
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

Multipart fields:

- `file`: the file bytes and filename;
- `mime`: the client-resolved MIME, used only as a claim; gateway revalidates.

Expected success response:

```json
{
  "file_uuid": "u-...",
  "filename": "report.pdf",
  "mime": "application/pdf",
  "size": 12345
}
```

### Staged `chat.send` Payload

After staging, `chat.send` sends:

```json
{
  "type": "application/pdf",
  "mime": "application/pdf",
  "name": "report.pdf",
  "file_uuid": "u-..."
}
```

The gateway resolves this into a stable attachment ref and must not persist
`file_uuid` into transcript envelopes.

## Auth Requirements

The multipart upload route intentionally requires header-based auth in token
mode. Query-string token auth must remain rejected for uploads.

Vue upload requests must therefore include the same token source used by the
WebSocket/RPC connection:

- read `sessionStorage.getItem("opensquilla.wsToken")`;
- when present, set `Authorization: Bearer <token>`;
- keep `credentials: "same-origin"` for same-origin cookies and browser policy;
- never place the token in the upload URL.

This is important because the current Vue staged upload path uses
`credentials: "same-origin"` but does not include an authorization header. The
legacy static chat upload path already includes the bearer token and should be
used as the behavior reference.

## Security and Safety Rules

- Frontend validation is advisory UX only; gateway validation remains
  authoritative.
- The renderer must never send local filesystem paths in chat payloads.
- Electron preload must not expose an arbitrary `readFile(path)` API for this
  feature.
- `file_uuid` is a short-lived upload-store identifier, not a durable reference.
- Staged upload bytes are evicted only after the turn is accepted.
- Failed `chat.send` after successful staging must keep the staged file retryable
  until TTL expiry.
- MIME sniffing mismatches are handled by gateway policy, not by trusting the
  browser-provided `File.type`.
- Directory entries and zero-byte files should be rejected with clear UX.

## Implementation Plan

### Frontend

1. Add `addAttachments(files: File[])` in `useChatAttachments.ts`.
2. Route file picker, paste, and drop through `addAttachments`.
3. Add drag-depth or containment-safe drop-zone state in `ChatView.vue`.
4. Show a drop affordance only when the drag payload contains files.
5. Add upload auth headers to staged upload requests.
6. Add retry handling for staged upload failures.
7. Keep `ChatComposer.vue` presentational: props down, events up.
8. Ensure `useChatSend.ts` does not clear failed attachments as if they were
   successfully queued.

### Gateway

1. Keep `/api/v1/files/upload` as the only staged upload endpoint.
2. Keep the upload route header-token requirement unchanged.
3. Confirm `UploadStore` and `attachment_ingest` reject unsupported MIME, over
   size, unknown UUID, restart-lost UUID, and total size overflow.
4. Add targeted tests only where the new frontend contract exposes gaps.

### Desktop

1. Validate drag/drop in packaged or dev Electron on macOS, Windows, and Linux
   where available.
2. Keep Electron preload unchanged unless validation proves native bridging is
   required.
3. If a bridge becomes required, expose only a narrow capability such as
   `stageDroppedFile(handle)` and keep all byte validation in the gateway.

## Test Plan

### Frontend Unit Tests

- `resolveAttachmentMime()` prefers allowed browser MIME and falls back to
  extension.
- Unknown UTF-8 text degrades to `text/plain`.
- Unknown binary files are rejected.
- Oversize files are rejected by per-MIME cap.
- `addAttachments()` handles mixed valid and invalid files without dropping the
  valid ones.
- Inline files transition from `inline_pending` to `inline`.
- Large stageable files transition from `uploading` to `staged`.
- Failed staged upload leaves a failed/removable state.
- Upload requests include `Authorization: Bearer <token>` when the token exists.
- Upload requests do not put the token in the URL.

### Vue Component Tests

- Dragging files over the chat surface shows the drop affordance.
- Dragging non-file data does not show the drop affordance.
- Dropping files calls the attachment composable once with all dropped files.
- Sending is disabled while any attachment is reading or uploading.
- Removing an attachment updates the pending list without mutating child props.

### Gateway Tests

- Multipart upload rejects missing auth header in token mode.
- Multipart upload accepts bearer token in token mode.
- Multipart upload rejects query-token-only auth.
- Staged `file_uuid` resolution returns an attachment ref with no `file_uuid` or
  inline `data`.
- Restart-lost UUID produces the re-upload error path.
- Successful turn acceptance evicts consumed UUIDs.
- Failed turn acceptance does not evict consumed UUIDs.

### Browser E2E

- Drop one small image into Web UI, send, and assert `chat.send` carries inline
  base64 attachment data.
- Drop one large PDF into Web UI, wait for staged chip, send, and assert
  `chat.send` carries `file_uuid`.
- Drop a valid file and an invalid file together; valid file remains pending and
  invalid file produces a toast.
- Verify mobile and desktop layouts do not overflow with long filenames or wide
  image thumbnails.

### Desktop Manual Smoke

Run on Electron dev and packaged builds:

1. Start the desktop client.
2. Drag a small PNG from the OS file manager into the chat thread.
3. Confirm the composer shows a thumbnail chip.
4. Send and confirm the user bubble renders the attachment.
5. Drag a PDF larger than the inline threshold.
6. Confirm upload progress changes to staged/ready.
7. Send and confirm the gateway accepts the turn.
8. Repeat with token auth enabled.
9. Confirm no local file path appears in the chat payload, transcript, or logs.

## Acceptance Criteria

- Browser Web UI supports drag-and-drop upload for every MIME currently allowed
  by `contracts/attachments.py`.
- Electron desktop supports the same drag-and-drop behavior without a separate
  upload code path.
- Token-authenticated uploads succeed because the frontend sends bearer auth
  headers.
- Query-token-only multipart upload remains rejected.
- Users cannot send while attachments are still reading or uploading.
- Failed uploads are visible and removable or retryable.
- Staged attachments are sent as `file_uuid` and are materialized into stable
  transcript attachment refs before runtime execution.
- `file_uuid` never appears in persisted transcript envelopes.
- Successful turn acceptance evicts consumed staged uploads.
- Frontend and backend tests cover valid, invalid, oversize, auth, retry, and
  layout cases.

## Rollout

- Ship behind the normal chat composer behavior with no user-facing setting.
- Keep existing file input and paste upload behavior working during rollout.
- Validate Web UI first, then Electron dev, then packaged desktop.
- Add troubleshooting guidance only if desktop platform differences require
  user-visible explanation.

## Open Questions

- Should failed staged upload chips offer retry, or should failure remove the
  chip and rely on toast only?
- Should duplicate files in the same drop be deduplicated by
  `name + size + lastModified`, or should duplicates be allowed?
- Should the drop affordance cover only the chat thread or the full chat view?
- Should future folder drag support be handled through a separate desktop-only
  import flow rather than this attachment pipeline?
