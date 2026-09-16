# Stream-decode goldens

These files freeze the exact `StreamEvent` sequence each provider adapter
decodes from its wire format today, so an adapter refactor can be proven
behavior-preserving. Driven by `tests/test_provider/test_stream_goldens.py`.

## Layout

Each adapter directory pairs a **wire fixture** (the literal bytes a mocked
upstream serves) with a **golden** (`<case>.events.json`, the serialized
decoded event sequence):

| Directory          | Adapter                   | Wire format                     |
| ------------------ | ------------------------- | ------------------------------- |
| `openai_compat/`   | `OpenAIProvider`          | Chat Completions SSE (`.sse`)   |
| `anthropic/`       | `AnthropicProvider`       | Messages SSE (`.sse`)           |
| `ollama/`          | `OllamaProvider`          | `/api/chat` JSONL (`.jsonl`)    |
| `openai_responses/`| `OpenAIResponsesProvider` | non-streaming JSON (`.json`)    |

Goldens are JSON lists of `{"type": "<EventClassName>", ...all fields...}`,
rendered with `json.dumps(..., indent=2, sort_keys=True)`. The `kind`
discriminator is dropped in favor of the class name.
`provider_replay` is serialized as its full JSON object, including native
reasoning details; adapters without captured replay state keep the field `null`.

## Regenerating

```sh
OPENSQUILLA_REGEN_GOLDENS=1 uv run pytest tests/test_provider/test_stream_goldens.py -q
uv run pytest tests/test_provider/test_stream_goldens.py -q   # must pass byte-identically
```

## Freeze policy

- The default run **byte-compares** goldens; any diff is a provider-decode
  behavior change. Regenerate only when the change is intentional, and review
  the golden diff in the PR as carefully as the code diff.
- The inventory test pins the exact file set in this directory: adding,
  renaming, or orphaning a fixture/golden without updating the case table in
  the test module fails.
- Fixtures must stay **synthetic**: dummy model ids, `sk-test-000`-style keys,
  invented prompts and token counts. Never paste real provider transcripts.
- Tests must stay **offline**: transcripts are served via
  `httpx.MockTransport`; no fixture may require credentials or network.
