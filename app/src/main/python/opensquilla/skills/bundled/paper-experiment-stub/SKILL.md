---
name: paper-experiment-stub
description: "Demo-only stub experiment for meta-paper-write: generates a deterministic results.csv seeded by topic hash. Not real science."
description_zh: "meta-paper-write的仅演示用桩实验：根据主题哈希生成确定性的results.csv。并非真实科研。"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  {
    "platform": {
      "emoji": "🧪",
      "requires": { "anyBins": ["python", "python3"] }
    }
  }
entrypoint:
  command: python {baseDir}/scripts/gen_results.py
  args:
    - --topic
    - "{{ inputs.user_message }}"
    - --out
    - "paper/results.csv"
  parse: text
  timeout: 15
---

# paper-experiment-stub

Stub experiment generator for the `meta-paper-write` demo. Given a topic
phrase, produces a 20-row CSV with columns `x, y_baseline, y_ours`, seeded
deterministically from the SHA-256 of the topic so re-runs are stable.
