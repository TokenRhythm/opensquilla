# Methodology

## Stages and state

The skill is built around three stages and one persistent JSON file:

```
plan.py     →  plan.json (initial)
iterate.py  →  plan.json (mutated, one round per call)
compile.py  →  report.md (read-only)
```

The plan file is the contract. Anyone reading it should be able to resume
the work without further context.

## Plan schema

```python
class Source(BaseModel):
    url: str
    title: str = ""
    excerpt: str = ""
    relevance: float = 0.0
    fetched_at: str = ""

class SubQuestion(BaseModel):
    id: str                  # "sq-001", "sq-002", ...
    question: str            # filled by host LLM after plan.py runs
    target_sources: int = 1  # depth-driven default
    sources: list[Source] = []

class Plan(BaseModel):
    question: str
    depth: "overview" | "thorough" | "exhaustive"
    created_at: str
    subquestions: list[SubQuestion]
    rounds: int = 0
    done: bool = False
```

## Depth profiles

| depth | sub-questions | target sources / sub-question | typical use |
|---|---|---|---|
| overview | 3-5 | 1 | "give me a quick read on X" |
| thorough | 6-10 | 3 | "research report on X" (default) |
| exhaustive | 12-20 | 5+ | "literature review", high-stakes decision |

The script picks the lower bound of the sub-question range; the host LLM
expands or merges sub-questions as the topic justifies.

## Sub-question quality

A good sub-question:

1. Is answerable in 1-3 paragraphs.
2. Has a verifiable answer (not pure opinion).
3. Does not duplicate another sub-question.
4. Maps to a search-engine query without ambiguity.

When the LLM populates `subquestion.question` text, it should rewrite the
original user question into 6-10 of these for `thorough` depth. Bad
sub-questions ("Is X good?") become "What outcomes did X produce in the
past 12 months, and how do they compare to the stated goals?".

## Iteration loop

```
while not plan.done:
    fetches = iterate.print_fetches(plan)        # → host
    evidence = host.fetch_each(fetches)          # OpenSquilla web tools
    iterate.record(plan, evidence)               # ← back in
```

The host agent is responsible for:

- Sending each `subquestion_id` + `question` text to the search engine
- Reading the top results
- Scoring each according to `references/sources.md`
- Returning evidence whose `relevance >= 0.5` (lower-relevance sources may
  be recorded but should be flagged)

The skill's `iterate.py` does not enforce relevance thresholds — it
accumulates whatever the host hands back. Quality control is the host's
responsibility.

## Compile rules

The compile step:

1. Never invents URLs or quotes.
2. Numbers citations sequentially `[^N]` in order of appearance.
3. Lists every collected source in `## References` even if cited only once.
4. Calls out gaps in `## What this report does not cover` — never hides
   them.
5. Does not pass judgment on conflicting sources; it lists both with their
   citations and lets the reader decide.

## When to stop early

If `overall_coverage` plateaus across two consecutive rounds without
crossing 0.6, abort:

- The question may be too narrow (no public sources).
- The question may be too vague (no convergent sub-questions).
- Search engine reach may be insufficient.

Re-scope rather than force more rounds. Compile a partial report with the
gap section listing what was attempted.
