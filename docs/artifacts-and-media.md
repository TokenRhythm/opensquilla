# Artifacts and Media

OpenSquilla can create and deliver files as part of agent work: reports, HTML
files, PDFs, slide decks, spreadsheets, generated images, and other artifacts.
Use artifacts when the output is too large, visual, structured, or important to
leave only in chat text.

## Artifacts

Artifacts are user-visible files created during a session. In Web UI chat they
appear as artifact cards when the runtime publishes them. In CLI runs, artifact
events can include file names, ids, and download URLs.

Common use cases:

- generate a report;
- create a standalone HTML prototype;
- build a CSV/XLSX workbook;
- create a PDF briefing;
- produce a slide deck;
- package generated output for channel delivery.

Ask directly:

```text
Create a one-page HTML dashboard from this data and publish it as an artifact.
```

```text
Generate a PDF briefing with sources and publish the final file.
```

### HTML projects and webpage preview

`publish_artifact` can preserve a generated HTML project instead of publishing
only its entry file:

```text
publish_artifact(
  path="site/index.html",
  bundle="directory",
  bundle_root="site",
)
```

- `bundle="auto"` (the default) follows statically identifiable local
  references from HTML, CSS, and JavaScript. Missing or rejected references are
  reported as a partial bundle.
- `bundle="directory"` snapshots the complete dedicated project directory and
  fails atomically if it contains a rejected path or sensitive file.
- `bundle="none"` preserves the legacy single-file behavior.

Historical single-file HTML remains readable without migration. If it refers
to local CSS, scripts, or other files that were never stored, the preview is
reported as partial instead of silently claiming that every resource loaded.

Generated web projects should use a dedicated subdirectory and `directory`
mode. Bundles are static sites: OpenSquilla does not start Vite, webpack, HMR,
or a project backend for them. Open an already-running development server in
the Desktop side browser when the project needs those services.

Desktop and a loopback Web UI default to a full-network preview. This runs the
page in a temporary, isolated browser context: normal browser JavaScript,
modules, workers, WebAssembly, WebGL, fonts, media, HTTP(S), WebSocket, and
page-level CORS/CSP rules still apply, but the page receives no OpenSquilla
credentials, Node/Electron APIs, host files, or system-browser login state.
Desktop offline mode keeps JavaScript enabled while restricting network access
to the artifact itself, including blocking WebRTC/TURN/STUN and speculative
DNS. A browser-hosted offline preview cannot enforce that all-protocol boundary
against arbitrary page JavaScript. It therefore runs bundle scripts in an
opaque sandbox with a restrictive response policy; external network access is
blocked, while workers, service workers, persistent storage, and root-absolute
paths are not guaranteed. A Web UI reached from another machine is always
forced to this visibly limited offline preview.

Set `OPENSQUILLA_PREVIEW_FORCE_OFFLINE=1` before starting the Desktop app or
gateway to disable full-network artifact previews as an incident-response
measure.

The release-gated Desktop PromptAnnotation workflow can bind modification
instructions to exact elements in supported single-file HTML artifacts. It is
disabled by default and has narrower format and trust-boundary requirements
than ordinary preview. Maintainers and operators should use the
[Prompt-Annotation editing guide](features/prompt-annotation-editing.md) for
capability checks, safe local verification, rollout, and rollback.

### Workbench resources and editable Documents

The release-gated resource Workbench keeps four lifecycles separate even when
they share one right-hand panel:

- an attachment is an immutable session input;
- a Document is an editable identity with a current head, immutable Revisions,
  and audited ChangeSets;
- a deliverable is an immutable published snapshot;
- a preview or EditSession is temporary host state, not stored content.

Previewing an attachment is read-only. Selecting **Edit** explicitly imports a
copy into a Document; repeated or response-lost requests resolve the same
durable import receipt. Editing the Document never changes the source
attachment. Selecting **Publish** fixes one named Revision into a new
deliverable, so later Document edits cannot alter an already published file.

The initial editable format is a bounded, single-file, NUL-free UTF-8 HTML
document. Office files remain discoverable and downloadable, but preview and
edit capabilities stay false with an explicit reason until their format
adapter and renderer are available. Workspace paths, `file://` URLs, remote
URLs, and automatic upload promotion are not canonical Document sources.

## When to Use Artifacts Instead of Chat

Use artifacts for:

- files the user should download or share;
- tables or reports that need layout;
- generated apps, dashboards, or prototypes;
- long output that would be awkward in chat;
- channel delivery where the platform supports file upload.

Use chat text for short answers, decisions, and next steps.

## Document Skills

OpenSquilla includes skills for common document formats:

- `docx` for Word documents;
- `pptx` for PowerPoint decks;
- `xlsx` for Excel workbooks;
- `pdf-toolkit` for structured PDF work;
- `html-to-pdf` for styled PDF rendering.

Discover them:

```sh
opensquilla skills search pdf
opensquilla skills view pptx
opensquilla skills view xlsx
```

Some document features require optional native/system dependencies. Use
`opensquilla skills list` and `opensquilla doctor` to check readiness.

## Image Input and Generation

In terminal chat, send an image for analysis:

```text
/image /path/to/screenshot.png Describe what is wrong with this UI.
```

Configure image generation:

```sh
opensquilla configure image-generation
```

Supported built-in image providers include OpenAI Images, OpenRouter Images,
and Qwen Token Plan (`wan2.7-image` / `wan2.7-image-pro`). Token Plan uses
`QWEN_TOKEN_PLAN_API_KEY`; its image-generation provider is distinct from the
Qwen model used to analyze image inputs.

Then ask for images in chat:

```text
Generate a clean product mockup image for this landing page.
```

Image provider support depends on configured provider credentials, optional
dependencies, and runtime policy.

## Text to Speech and Media Helpers

The media tool family includes image, PDF, and TTS helpers. Availability can
depend on provider config, optional dependencies, and runtime policy.

Use media helpers when the requested output is naturally a file or asset rather
than a plain text answer.

## Channel Delivery

Channels differ in file-size limits, threading behavior, and upload APIs. If a
channel cannot deliver an artifact directly, use the Web UI artifact card or
session export as the recovery surface.

For channel setup, see [`channels.md`](channels.md).

## Troubleshooting

If an artifact does not appear:

1. Check the chat or CLI output for artifact events.
2. Open the Web UI session and inspect artifact cards.
3. Export the session if you need durable evidence:

   ```sh
   opensquilla sessions export <session-key>
   ```

4. Run `opensquilla doctor` if a document or media dependency appears missing.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
