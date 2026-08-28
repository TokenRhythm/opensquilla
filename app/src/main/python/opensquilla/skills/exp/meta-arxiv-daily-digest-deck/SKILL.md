---
name: meta-arxiv-daily-digest-deck
description: "Fetch the day's top arXiv submissions in a chosen category, write a structured per-paper digest, render the digest as a PPTX deck (one slide per paper), and persist the digest to long-term memory. Use for a daily 'arxiv morning briefing' — manual fire or cron-scheduled."
kind: meta
meta_priority: 55
always: false
triggers:
  - "arxiv daily digest"
  - "arxiv digest deck"
  - "arxiv 日报"
  - "arxiv 晨刊"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: fetch_arxiv
      kind: agent
      skill: sub-agent
      with:
        task: |
          Fetch the latest papers from arXiv's public API.

          Default query: cs.CL OR cs.AI, sorted by submittedDate descending,
          max_results=6. If the user's invocation contains a category override,
          honor it (look for `cs.XX` patterns in the message).

          User invocation:
          {{ inputs.user_message | xml_escape | truncate(200) }}

          URL template: `http://export.arxiv.org/api/query?search_query=<query>&sortBy=submittedDate&sortOrder=descending&max_results=6`

          Use the available HTTP fetch tool. Parse the Atom XML response and
          extract for each entry: id (with version), title, summary (the abstract),
          authors (up to 5), pdf URL.

          Reply with ONLY a JSON array on one line, no preamble:
            [{"id":"...", "title":"...", "abstract":"...", "authors":[...], "pdf_url":"..."}, ...]

          If the fetch fails (HTTP error, parse error, empty result), reply with
          exactly: FETCH_FAILED: <one-line cause>
    - id: digest_papers
      kind: agent
      skill: summarize
      depends_on: [fetch_arxiv]
      with:
        task: |
          Write a structured digest for each paper. Skip if upstream is
          FETCH_FAILED — reply with `DIGEST_SKIPPED` in that case.

          Papers JSON from upstream:
          ---
          {{ outputs.fetch_arxiv | truncate(10000) }}
          ---

          For each paper, output this exact block (separate papers with a line of `---`):

          ## <Paper Title>
          **Authors**: <first 3 author names, then et al. if more>
          **arXiv ID**: <id>
          **Core claim** (1 sentence): <claim>
          **Method summary** (2-3 sentences): <how>
          **Key numbers** (if present): <metric: value, metric: value>
          **Why it matters** (1 sentence): <relevance>
    - id: render_deck
      kind: agent
      skill: pptx
      depends_on: [digest_papers]
      with:
        task: |
          Render the per-paper digest below into a PPTX deck. One slide per
          paper. Slide title = paper title. Slide body = the rest of that
          paper's digest block. Title slide first with text
          `arXiv Daily Digest — {{ inputs.user_message | xml_escape | truncate(80) }}`.

          Digest body:
          ---
          {{ outputs.digest_papers | truncate(12000) }}
          ---

          Save to: `{{ inputs.workspace_dir }}/arxiv-daily/digest.pptx`
          (overwrite OK; cron mode keeps only the latest, manual fire uses
          the same name and the user can rename after if needed). Create
          the `arxiv-daily/` subdirectory if missing.

          Reply with the absolute output path on a single line, no preamble.

          If the digest is `DIGEST_SKIPPED`, reply: `RENDER_SKIPPED`.
    - id: persist
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [digest_papers, render_deck]
      tool_args:
        path: "memory/arxiv-daily.md"
        mode: append
        content: |
          === arxiv-daily digest ===
          invocation: {{ inputs.user_message | xml_escape | truncate(200) }}
          deck: {{ outputs.render_deck | truncate(200) }}
          digest:
          {{ outputs.digest_papers | truncate(8000) }}
---

# arXiv Daily Digest Deck (Meta-Skill)

A near-pure **`tool_call` + linear DAG** meta-skill: fetch arXiv via
the public Atom API, write a per-paper structured digest, render it
into a PPTX deck, and persist the digest into long-term memory under
the `arxiv-daily` topic.

## Trigger surface

Fire manually with the English trigger `arxiv daily digest` or one of the
localized triggers listed in the frontmatter. Category override: include a
`cs.XX` token anywhere in the invocation.

## Fallback

If `fetch_arxiv` fails (network or parse), the downstream steps
short-circuit by emitting `_SKIPPED` markers; no half-baked PPTX or
memory note is written. Operator can retry by re-invoking, or
manually run `curl 'http://export.arxiv.org/api/query?…'`.
