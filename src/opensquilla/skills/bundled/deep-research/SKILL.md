---
name: deep-research
description: "Multi-round research with explicit methodology, evidence tracking, and citation-tagged synthesis. Trigger on 'deep dive', 'research report', 'literature review', 'investigate X across sources', 'multi-round investigation'. Distinct from the `summarize` skill, which is a single-pass condensation; this skill maintains a state file across iterations, tracks coverage, and produces a long-form report with per-claim citations. Three execution stages: plan (scope into sub-questions), iterate (record evidence per round), compile (synthesize report). The skill itself does not fetch the web — it tells the host agent which fetches to perform via OpenSquilla's existing web tools, and records what comes back."
description_zh: "具有明确方法论、证据追踪和带引用标注综合的多轮研究。触发词如'深入研究'、'研究报告'、'文献综述'、'跨来源调查X'、'多轮调查'。区别于单轮浓缩的 summarize 技能：本技能跨迭代维护状态文件、追踪覆盖度，并生成带逐条引用的长篇报告，分为规划、迭代、汇编三个阶段。技能本身不抓取网页，而是通过OpenSquilla现有的网页工具告知主机代理执行抓取并记录结果。"
homepage: ""
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/in-depth-research
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "🔬",
      },
  }
---

# deep-research

Investigate a question by walking it through three explicit stages with a
persisted state file. Use this when a single-pass `summarize` would lose too
much, or when the user asks for a "research report" / "literature review".
The host agent does the web fetching; this skill structures the work and
keeps a paper trail.

## Decide if this is the right tool

| Need | Use |
|---|---|
| One-line summary of an article | `summarize` |
| Multi-round investigation with citations | this skill |
| Quick lookup, single source | direct web search |
| Continuous monitoring of a topic | a digest/cron skill |

## Stages

```
Scope → Plan → Iterate (×N) → Compile → Deliver
```

State persists in a single JSON file you pass between stages. The file is
the contract; if you can describe the file, you can resume the research at
any point.

---

## Stage 1: Plan

```bash
python {baseDir}/scripts/plan.py \
    --question "How did Manus differentiate from competing AI agents in 2025?" \
    --depth thorough \
    --out plan.json
```

`--depth` choices:

- `overview` — 3-5 sub-questions, target 1 source per sub-question
- `thorough` — 6-10 sub-questions, target 2-3 sources per sub-question
- `exhaustive` — 12-20 sub-questions, target 5+ sources per sub-question

The plan is a `pydantic` model serialized to JSON; see
[references/methodology.md](references/methodology.md) for the schema and
the system-review approach the depth choices implement.

---

## Stage 2: Iterate

Each round: read the plan, decide which sub-questions need attention,
print the fetch list for the host agent to execute, and (after the agent
returns results) record evidence back into the plan.

```bash
# Show the host what to fetch this round
python {baseDir}/scripts/iterate.py --plan plan.json --round 1 --print-fetches

# After the host fetches, record results back
python {baseDir}/scripts/iterate.py --plan plan.json --round 1 \
    --record evidence_round_1.json
```

`evidence_round_1.json`:

```json
[
  {
    "subquestion_id": "sq-002",
    "url": "https://...",
    "title": "...",
    "excerpt": "...",
    "relevance": 0.85,
    "fetched_at": "2026-05-06T10:14:00Z"
  }
]
```

The script updates per-sub-question coverage estimates. When all
sub-questions reach the depth-target coverage, the plan's `done` flag flips
to `true` and the iteration loop terminates.

See [references/sources.md](references/sources.md) for the 5-axis source
evaluation (Authority, Recency, Evidence, Bias, Corroboration) you should
apply when judging relevance.

---

## Stage 3: Compile

```bash
python {baseDir}/scripts/compile.py --plan plan.json --out report.md
```

Output is markdown with:

1. Executive summary (5-8 lines)
2. Methodology block (depth, rounds, source count)
3. Per-sub-question section with embedded citations `[^N]`
4. References block listing every source with URL + fetched_at + relevance
5. "What this report does not cover" — explicit gaps from low-coverage
   sub-questions

Citations link to the references block. The compile step never invents
sources — every `[^N]` in the body must correspond to an entry recorded in
stage 2.

---

## Boundaries

- This skill does not fetch the web itself. It is a methodology + state
  manager. Pair it with the host agent's web search/fetch tools.
- It does not resolve contradictions among sources automatically. The
  compile step will note conflicting evidence in the report; the user
  decides which side wins.
- It is not a fact-checker. Source quality scoring is heuristic; treat the
  output as a starting point, not a verdict.
- For ongoing monitoring (daily digests, RSS-style updates) build a cron
  skill that calls this one with a fresh question each cycle.

---

## Differentiation from `summarize`

`summarize` takes one document and produces a shorter version. This skill
takes one question and produces a researched report drawing on many
documents, with explicit evidence tracking. They share no trigger words by
design — `summarize` triggers on "summarize", "shorten", "tl;dr"; this
skill triggers on "research", "investigate", "literature review", "deep
dive".
