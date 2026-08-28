---
name: meta-security-review-bundle
description: "Compose three independent security gates over a candidate operation — policy/governance review, secret/credential scan, and audit-log emit — then arbitrate the verdicts with a strict priority rule (governance DENY > scanner WARN > ALLOW). Use when reviewing a proposed code change, script, or environment manipulation for safety."
kind: meta
meta_priority: 75
always: false
triggers:
  - "security review"
  - "安全审查"
  - "审查这段代码"
  - "review for safety"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: high
    capabilities:
      - filesystem-read
      - network-read
      - subprocess
composition:
  steps:
    - id: policy_review
      kind: agent
      skill: sub-agent
      with:
        task: |
          You are the *policy reviewer* for a security-review bundle.
          Apply only the rules below; do not invent additional concerns.

          Operation under review:
          ---
          {{ inputs.user_message | xml_escape | truncate(800) }}
          ---

          Rules (in priority order):
            1. DENY if the operation would modify `/etc/`, `/boot/`, system
               systemd units, `~/.ssh/authorized_keys`, or `/etc/sudoers`.
            2. DENY if the operation would exfiltrate credentials
               (read `.env`, `~/.aws/credentials`, `~/.ssh/id_*`, etc.)
               to a remote destination.
            3. DENY if the operation would disable or bypass sandbox /
               selinux / apparmor controls.
            4. Otherwise ALLOW.

          Reply with EXACTLY one line, no preamble:
            DENY: <one-sentence reason>
            ALLOW: ok
    - id: secret_scan
      kind: agent
      skill: sub-agent
      with:
        task: |
          You are the *secret scanner* for a security-review bundle. You
          do NOT judge intent — only detect credential-shaped strings.

          Input:
          ---
          {{ inputs.user_message | xml_escape | truncate(800) }}
          ---

          Look for:
            * API keys (sk-…, ghp_…, AKIA…, AIza…, …)
            * JWT / OAuth bearer tokens
            * Private keys (-----BEGIN … PRIVATE KEY-----)
            * Database connection URIs with embedded passwords
            * Plaintext passwords next to obvious labels (pwd=, password:)

          Reply with EXACTLY one line, no preamble:
            WARN: <count> <one-line summary of kinds detected>
            CLEAR: no secrets found
    - id: arbitrate
      kind: agent
      skill: sub-agent
      depends_on: [policy_review, secret_scan]
      with:
        task: |
          Three independent security gates ran on this operation:

          - policy_review: {{ outputs.policy_review }}
          - secret_scan:   {{ outputs.secret_scan }}

          Apply the arbitration rule STRICTLY in this priority order
          (higher wins; do NOT mix or soften):

            1. If policy_review begins with "DENY" → final verdict is DENY.
               Pass through the policy reviewer's reason verbatim.
            2. Else if secret_scan begins with "WARN" → final verdict is WARN.
               Pass through the scanner's summary verbatim and require
               explicit user acknowledgement before proceeding.
            3. Else (policy_review ALLOW and secret_scan CLEAR) → ALLOW.

          Reply with EXACTLY this structure on the first line, then
          additional lines as needed:

            DENY: <policy reason>
            WARN: <scanner summary; user must confirm>
            ALLOW: cleared by both gates
    - id: audit_emit
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [arbitrate]
      tool_args:
        path: "memory/security-review.md"
        mode: append
        content: |
          === security review audit ===
          operation: {{ inputs.user_message | xml_escape | truncate(400) }}
          policy_review: {{ outputs.policy_review | truncate(200) }}
          secret_scan: {{ outputs.secret_scan | truncate(200) }}
          verdict: {{ outputs.arbitrate | truncate(400) }}
---

# Security Review Bundle (Combinator Meta-Skill)

A **combinator-style** meta-skill: three independent gates run in
parallel over the candidate operation, then a fourth step arbitrates
the verdicts with a strict priority rule. The fifth step emits an
audit record so the run is recallable later.

This bundle is the OpenSquilla equivalent of pptx slide 7's combinator
pattern: multiple rule sets active simultaneously, with the arbitration rule
explicit in the SKILL.md rather than implicit in the LLM's good judgement.

## Arbitration rule

The arbitrate step encodes the priority `policy > scanner > allow`
verbatim in its task prompt. The rule is **not** soft-suggested
("consider whether…"); it's an enforceable check (`startswith("DENY")`).
This follows the pptx slide 7 recommendation to combine extensive scenario
testing with an explicit non-negotiable-rule fallback sentence.

## Fallback

If any of the three primary gates fails (sub-agent error, timeout,
empty deliverable), the orchestrator's existing failure cascade
produces a structured failure payload. Operators should review the
partial verdicts in `step_outputs` and decide manually.

## Use sparingly

This pattern multiplies token cost by N (number of gates) for a
single user turn. Don't reach for the combinator unless multiple
independent rule sets *genuinely must* both apply — otherwise prefer
an orchestrator with a single, well-defined sequence.
