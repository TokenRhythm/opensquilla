---
name: http-fetch
description: "Fetch a URL via HTTP/HTTPS and return the response body as text. Lightweight entrypoint replacement for `sub-agent` steps whose only job is a single GET/POST. Supports GET (default), POST/PUT/DELETE with a stdin-piped body, configurable timeout, and a max-bytes cap — no LLM agent loop, no custom-header injection (request goes out with urllib defaults). Use for simple data-fetch steps in meta-skill DAGs; for crawling, JS-rendered pages, or complex auth chains use sub-agent + scrapling instead."
description_zh: "通过HTTP/HTTPS抓取URL并以文本返回响应体。作为仅执行单次GET/POST的 sub-agent 步骤的轻量替代入口。支持GET（默认）、带stdin管道请求体的POST/PUT/DELETE、可配置超时和最大字节上限，无LLM代理循环、无自定义请求头注入。适合meta-skill DAG中简单的数据抓取步骤；爬取、JS渲染页面或复杂认证链请改用 sub-agent + scrapling。"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/http_fetch.py
  args:
    - --url
    - "{{ with.url }}"
    - --method
    - "{{ with.method | default('GET') }}"
    - --timeout
    - "{{ with.timeout | default(30) }}"
    - --max-bytes
    - "{{ with.max_bytes | default(2000000) }}"
  stdin: "{{ with.body | default('') }}"
  parse: text
  timeout: 60
---

# http-fetch (sub-skill)

Direct shell wrapper for a single HTTP request. Replaces
``sub-agent`` sub-Agent steps that just GET/POST a URL — order-of-
magnitude faster (no LLM round-trip, no tool surface, no iteration
loop).

## Inputs (``with:``)

| key         | required | default                | notes                                                          |
|-------------|----------|------------------------|----------------------------------------------------------------|
| `url`       | yes      | —                      | absolute http(s) URL                                           |
| `method`    | no       | `GET`                  | `GET` / `POST` / `PUT` / `DELETE` (case-insensitive)           |
| `body`      | no       | `''`                   | request body, piped via stdin (for POST/PUT); empty = no body  |
| `timeout`   | no       | `30`                   | request timeout in seconds                                     |
| `max_bytes` | no       | `2000000`              | response body cap; larger payloads truncated + suffixed `…`    |

## Output

- Success: response body on stdout (UTF-8 decoded, truncated to
  ``max_bytes`` if larger; lossy decode replaces invalid bytes).
- Non-2xx response: exit 1, stderr ``HTTP <code>: <reason> <body[:200]>``;
  stdout still carries the body for callers that want to inspect.
- Network / DNS / timeout failure: exit 2, stderr cause.

## When NOT to use

- Crawling multiple pages → use ``scrapling`` (via ``sub-agent``).
- JS-rendered pages → use ``sub-agent`` + browser tools.
- OAuth dance / multi-step auth → use ``sub-agent``.
- Streaming responses → not supported (we buffer + return).

## Fallback

If this skill is unavailable, callers should spawn ``sub-agent``
with a curl/requests task — same result, ~10× the latency and a
non-deterministic tool loop.
