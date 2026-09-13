# Local Desktop inline-preview fixture

This is a deterministic, **free, loopback-only Ollama-compatible fixture**, not
a general language model. It does not read/write the test workspace itself,
forward requests, possess provider credentials, or simulate Gateway RPC/tool
results. The real Gateway must advertise and execute every requested tool.

Start from any directory:

```sh
node /path/to/opensquilla/desktop/electron/scripts/fixtures/workspace-inline-preview/provider.mjs 0
```

The first JSON line gives `baseUrl`, model and exact prompts. Configure a fresh
Desktop profile to use this Ollama base URL and model
`opensquilla-inline-preview-fixture`, explicitly enabling `supports_vision`
and `supports_tools` for that model. Do not change production permissions or
another profile's provider settings. Use ordinary single-model routing.

In the actual Desktop app, send these exact current-turn messages:

1. `创建正文链接测试网页`: real `write_file` writes HTML and external CSS,
   then `open_workspace_preview` registers the dedicated directory. The answer
   contains the inline-code path `inline-preview/index.html`.
2. Select the heading in the native preview, annotate `把标题改成紫色` and send.
   Real `read_file`, `edit_file` and preview reopening change the external CSS.
   This prompt also works as ordinary text, but that is **not** screenshot or
   native-annotation acceptance evidence.
3. `测试无文件名回复`: reopens the existing Document and deliberately omits its
   filename from the answer to exercise the lightweight fallback link.

Each accepted request logs the scenario, step, `imageCount`, `annotationCount`
and next tool. Image counts describe actual model-bound `images` arrays; no
image bytes or arbitrary message content are logged. Annotation acceptance
requires checking nonzero image/annotation counts in addition to actual UI,
file bytes, versions and publication records. The fixture strips only known
production timestamp/runtime/attachment wrappers and validates page-context
annotations. Unknown requests or unsuccessful tool receipts return HTTP 422.

Provider-only tests (their synthetic receipts are **not** end-to-end evidence):

```sh
node --test desktop/electron/scripts/fixtures/workspace-inline-preview/provider.test.mjs
```
