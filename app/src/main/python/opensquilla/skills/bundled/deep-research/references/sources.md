# Source evaluation rubric

For each candidate source the host LLM should score five axes before
deciding whether to record it as evidence. The five-axis score is **not**
serialized into the plan file; what is serialized is the resulting
`relevance` (a single 0.0–1.0 float) plus the human-readable excerpt.

## The five axes

### 1. Authority

Who wrote this? Their credentials? The publishing venue's reputation?
Score higher for primary sources (government data, peer-reviewed papers,
SEC filings, official documentation), lower for aggregator sites and
SEO-bait blog posts.

### 2. Recency

When was it published or last updated? For domains where the answer changes
quickly (LLMs, AI agents, web frameworks, regulations), favor sources from
the last 6-12 months. For stable domains (mathematics, established history),
recency matters less — but a 2010 source about a 2025 topic is suspect.

### 3. Evidence

Does the source cite primary data, or does it assert? A claim with a chart
sourced to an SEC filing scores higher than the same claim made
unsupported. Look for footnotes, hyperlinks to data, methodology sections.

### 4. Bias

Does the publisher have a stake in the answer? An AI lab's blog post about
their own product is structurally biased; their post comparing to a rival
even more so. Mark biased sources as evidence anyway — they show what the
biased party says — but lower their relevance.

### 5. Corroboration

Do other independent sources confirm? The strongest evidence is a claim
attested by 2+ independent primary sources. The weakest is a claim made by
one source that everyone else cites verbatim (the "single point of failure"
of the open web).

## Mapping axes to the relevance score

A rough heuristic:

```
score = 0.35 * authority
      + 0.20 * recency
      + 0.20 * evidence
      + 0.10 * (1 - bias_severity)
      + 0.15 * corroboration
```

Each input axis is 0.0–1.0. The output `relevance` you write into the plan
file should be this composite.

A practical bar:

| relevance | meaning |
|---|---|
| ≥ 0.8 | strong; cite confidently |
| 0.6 – 0.8 | useful; cite with attribution to limitations |
| 0.4 – 0.6 | weak; cite only when stronger evidence is unavailable |
| < 0.4 | reject — record as a deadend, do not include in the report |

## Working with conflicting sources

If two ≥0.6-relevance sources contradict:

1. Record both with their relevance scores.
2. In the compile step, note the conflict explicitly:
   `Source A reports X [^N1]; Source B reports Y [^N2].`
3. Do not pick a winner unless one source clearly subsumes the other (e.g.,
   a primary source vs. a secondary source citing it).

## Anti-patterns

- **Citation laundering**: do not quote a low-authority source citing a
  high-authority one; cite the high-authority source directly.
- **Snowballing**: one round, one search query per sub-question. Resist
  the temptation to chase tangents.
- **Paywall via pretext**: if the source is paywalled, record it with
  `relevance: 0` and a note rather than fabricating excerpts from the
  abstract or social-media discussion.
