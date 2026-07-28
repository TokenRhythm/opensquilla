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
