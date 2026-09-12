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

Use **Annotate** on an open page to attach selections and instructions to an
ordinary chat message. The Agent chooses whether to inspect files, edit them,
or use the Desktop browser tool on the existing preview. An annotation does
not prescribe a sequence of tools. See the
[HTML annotation and editing guide](features/prompt-annotation-editing.md) for
working-file behavior, browser availability, and upgrade compatibility.

### Workbench resources and editable Documents

The resource Workbench presents several related kinds of content in the same
right-hand panel:

- an attachment is an immutable session input;
- a Document has normal working files, a current version, immutable historical
  Revisions, and audited changes;
- a deliverable is an immutable published snapshot;
- a preview is temporary host state that displays those resources.

Opening supported HTML for editing imports a copy into a Document. Repeated
or response-lost imports resolve the same durable receipt, and editing the
copy never changes the source attachment. A generated HTML project that has
been published can retain its exact workspace source as the Document's
working files, so later edits continue the same document.

Working files include the HTML entry and its saved local resources. Preview
reflects those files; a successfully completed turn saves a version when the
bundle changed. Failed or cancelled turns retain working files for later work
without automatically saving a completion version. An explicit publication
that already succeeded remains in history. Restoring a version restores its
owned resource bundle while preserving unrelated workspace files. Publishing
fixes a selected Revision into a deliverable that later edits cannot change.

The editable entry must be supported, NUL-free UTF-8 HTML within the format's
size limits; saved CSS, JavaScript, images, and other local bundle resources
stay with it. This does not make Office formats editable: they remain
available for discovery and download, with preview and edit support reported
per format. Remote URLs and arbitrary workspace paths are not accepted as
attachment imports. Generated workspace sources are bound only through their
verified publication provenance; automatic upload promotion is not used.

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

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
