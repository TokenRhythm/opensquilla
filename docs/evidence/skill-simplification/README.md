# Skill simplification A/B evidence

This directory contains reproducible, synthetic evidence for the Skill catalog
simplification. No real conversation, credential, private key, reference-repository
path, or host-absolute path is present in the versioned artifacts.

## Identity and method

- Baseline: `27ca02ca4596f8f24fe79ab603f068cbec6ab858`.
- Candidate: `058a7675a24177f513ca9262b1d0db96578c1e58`, created with `git commit-tree`
  over a temporary index so the user's HEAD and index were not moved.
- Both benchmark worktrees were clean.
- Provider/model: `tokenrhythm` / `deepseek-v4-pro-0813`.
- Tool schema: 58 definitions in the same order; canonical digest
  `fc6227e41e64a69cea4c8251d7554efb520dd17ceb746efce11fbb644b290d97`.
- Sampling: provider defaults for temperature/top-p, concurrency 1, output limit
  256 tokens (32 for calibration).
- Dataset: 166 logical synthetic requests: tokenizer calibration, warm-up, five
  ABBA/BAAB micro groups, five independent ten-turn continuity sessions, and
  fourteen paired multi-task/migration cases.
- Eligibility uses a recorded synthetic full-capability profile so catalog shape
  is compared independently of host-installed binaries and credentials. Runtime
  eligibility remains fail-closed in product code.
- Cold cache means turn 1 of each continuity session; warm cache means turns 2-10
  with identical fixed history on both sides.

`raw.json` is the source of truth. `raw.csv` and `summary.json` can be regenerated:

```bash
uv run python scripts/skill_simplification_ab.py summarize \
  --raw docs/evidence/skill-simplification/raw.json
```

The checked-in summary and CSV were byte-compared with an independent regeneration.

## Headline measurements

| Metric | Baseline | Candidate | Change |
| --- | ---: | ---: | ---: |
| Model-visible bundled entries | 48 | 11 | -77.08% |
| Complete catalog characters | 21,675 | 6,059 | -72.05% |
| Prompt catalog characters | 6,777 | 4,509 | -33.47% |
| Provider-measured catalog tokens | 1,771 | 1,121 | -36.70% |
| Tool-schema tokens | 11,343 | 11,343 | 0% |
| Multi-task mean input tokens | 13,309.429 | 12,665.714 | -4.84% |
| Warm continuity cache-read ratio | 20.6704% | 20.4031% | -0.2673 pp |
| Multi-task route accuracy | 64.2857% | 85.7143% | +21.4286 pp |
| Multi-task task success | 57.1429% | 78.5714% | +21.4285 pp |
| Multi-task median end-to-end latency | 16,546.555 ms | 8,551.444 ms | -48.32% |

The unchanged tool schema dominates input size, so the required 30% mean total-input
reduction was not achieved even though catalog tokens fell by 36.70%. With 11,343
tool-schema tokens inside an approximately 13.3k-token baseline request, that total-input
gate cannot be reached by Skill-catalog reduction alone while the schema is fixed.

## Acceptance gates

The overall result is **FAIL** because every hard gate must pass.

| Gate | Result | Evidence |
| --- | --- | --- |
| Mean input tokens reduced by at least 30% | FAIL | Multi-task mean fell 4.84%. |
| No systematic per-task input growth | PASS | No task category showed systematic reverse growth. |
| Warm provider cache-read ratio not lower | FAIL | 20.6704% to 20.4031%. |
| Warm uncached input median lower | PASS | 13,311 to 11,538 tokens. |
| Prefix stability 100% | PASS | 100% on both sides. |
| Reinjection waste zero | PASS | 0 on both sides. |
| Avoidable break rate zero | PASS | 0 on both sides. |
| Median end-to-end latency no worse than 5% | PASS | Multi-task median improved 48.32%. |
| Non-retired task success not lower | PASS | Candidate did not regress the fixed non-retired set. |
| No truncated samples | FAIL | 10 total: baseline 7, candidate 3. |
| Visible-word median within ±10% | FAIL / unassessable | Both medians are 0 because tool-call samples have no visible text; the ratio is undefined and the summarizer fails closed. |

Provider instability was retained: across versioned requests, baseline recorded
14 timeout attempts and 2 5xx attempts; candidate recorded 18 timeout attempts and
4 5xx attempts. Candidate continuity had two terminal failures. Successful-only and
end-to-end latency are reported separately in `summary.json`.

## Verification record

- Unmodified baseline focused suite: 278 passed.
- Changed-test regression suite: 827 passed, 5 skipped; new projection suite:
  12 passed.
- Stable Meta and recovery suite: 190 passed, 3 skipped.
- WebUI: baseline 4,609 passed (4,594 plus 15 worker-timeout reruns); candidate
  4,609 passed. Candidate production build, architecture, security, theme, and
  distribution verification passed.
- Full Python run on the clean pre-fix candidate snapshot: 24,299 passed, 257
  skipped, 30 failed. The final snapshot reran all 30 failure nodes: 92 passed and
  the remaining 9 exactly matched baseline failures (four macOS process-group
  cases, three missing-Docker experiment cases, one BSD `sed -i` case, and one
  pre-existing TUI truncation case).
- Ruff passed; mypy passed across 972 source files.
- Windows contracts and CI shard planning passed statically. No Windows or Linux
  host was available for native execution; macOS arm64 was measured directly.

The PR was subsequently rebased onto the then-current `main`. Integration
verification on that tree passed 326 focused Python tests (1 skipped), Ruff,
mypy across 1,051 source files, 5,128 WebUI unit tests, the WebUI transport
architecture gates, and the production build. The Provider measurements above
remain bound to the recorded candidate SHA; they were not relabeled as
post-rebase measurements.

Reference research was read-only and informed the separation of loading,
selection, invocation, prompt-cache layout, workspace lifecycle, and Gateway
protocol boundaries. The implementation is independent and has no runtime
dependency on those repositories.
