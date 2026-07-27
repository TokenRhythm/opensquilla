---
name: skill-creator-smoke-test
description: "Internal tool (not user-invocable). Called by meta-skill-creator as a DAG step (kind: agent) to run G3 (positive smoke) and G4 (negative smoke) gates against a candidate meta-skill SKILL.md. Cross-vendor: fixture-generation LLM != classifier LLM. Returns JSON."
description_zh: "内部工具（不可由用户直接调用）。由meta-skill-creator作为DAG步骤（kind: agent）调用，针对候选元技能SKILL.md运行G3（正向冒烟）和G4（负向冒烟）闸门。跨厂商：fixture生成LLM≠分类器LLM。返回JSON。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Skill Creator Smoke Test

When invoked as a `kind: agent, skill: skill-creator-smoke-test` step, this sub-agent:

1. Receives `skill_md`, `fixture_gen_model`, `classifier_model` from the parent step's `with:`
2. Calls `simulate_meta_resolution` with a positive fixture (LLM-generated using `fixture_gen_model`)
3. Calls `simulate_meta_resolution` with a negative fixture (LLM-generated, cross-domain)
4. Returns `{"G3": {...}, "G4": {...}}` JSON

If `OPENROUTER_API_KEY` is absent OR either `fixture_gen_model`/`classifier_model` is unconfigured, the sub-agent falls back to the deterministic fixture generator and pins `classifier_model="stub"`. In both cases the smoke step still emits G3/G4 records, but with a `degraded: true` flag.

## Fallback

If the sub-agent fails entirely, output `{"G3": {"passed": false, "reason": "smoke unavailable"}, "G4": {"passed": false, "reason": "smoke unavailable"}}`.
