---
name: meta-codereview-current-diff
description: "Read the current uncommitted diff, run three independent reviewers (safety + tests-coverage + style) in parallel, then arbitrate a single BLOCK / BLOCK_WITH_OVERRIDE / PASS_WITH_NOTES verdict. Use before commit when you want a multi-perspective second-opinion instead of a single-reviewer agent loop."
kind: meta
meta_priority: 65
always: false
final_text_mode: "step:arbitrate"
triggers:
  - "multi-reviewer diff"
  - "codereview my diff"
  - "三路 review"
  - "审查当前 diff"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: read_diff
      kind: skill_exec
      skill: git-diff
      with:
        mode: cached_fallback_worktree
        cwd: "{{ inputs.workspace_dir | default('.') }}"
    - id: review_safety
      kind: llm_chat
      depends_on: [read_diff]
      with:
        system: "You are a precise code safety reviewer. Reply with exactly one requested verdict line."
        task: |
          You are the *safety reviewer* for a 3-reviewer codereview bundle.
          Apply ONLY the rules below; do not invent additional concerns.

          Diff under review:
          ---
          {{ outputs.read_diff | truncate(8000) }}
          ---

          Rules (in priority order):
            1. CRITICAL if the diff introduces SQL injection (string
               concatenation into SQL, unparameterized queries via .format
               or f-strings into execute/query).
            2. CRITICAL if the diff introduces shell-injection
               (subprocess with shell=True + user input, or os.system on
               unvalidated strings).
            3. CRITICAL if the diff hardcodes a credential (sk-…, ghp_…,
               AKIA…, private key, password=… string literal).
            4. WARNING if the diff templates user-controlled strings into
               tool args / prompts WITHOUT `xml_escape` (G1.6 contract).
            5. CLEAR if none of the above apply.

          Reply with EXACTLY one line, no preamble:
            CRITICAL: <one-sentence reason>
            WARNING: <one-sentence reason>
            CLEAR: no safety concerns found
    - id: review_tests
      kind: llm_chat
      depends_on: [read_diff]
      with:
        system: "You are a precise test-coverage reviewer. Reply with exactly one requested verdict line."
        task: |
          You are the *test-coverage reviewer* for a 3-reviewer codereview
          bundle. Apply ONLY the rules below.

          Diff under review:
          ---
          {{ outputs.read_diff | truncate(8000) }}
          ---

          Rules:
            1. MISSING_TESTS if the diff adds a new public function / class /
               protocol under `src/` and does NOT add a corresponding test
               under `tests/`.
            2. MISSING_TESTS if the diff is a bug fix (commit message style
               `fix(...):`) and does NOT add a regression test.
            3. PASS if refactor-only (no behaviour change), even without
               new tests.
            4. PASS if the diff is doc-only or config-only.

          Reply with EXACTLY one line, no preamble:
            MISSING_TESTS: <which functions/classes lack tests>
            PASS: tests adequate (or n/a)
    - id: review_style
      kind: llm_chat
      depends_on: [read_diff]
      with:
        system: "You are a precise style reviewer. Reply with exactly one requested verdict line."
        task: |
          You are the *style / idiom reviewer* for a 3-reviewer codereview
          bundle. Look for project-specific anti-patterns.

          Diff under review:
          ---
          {{ outputs.read_diff | truncate(8000) }}
          ---

          Anti-patterns to flag (do not invent others):
            - Functions > 80 source lines
            - Conditional nesting > 4 deep
            - Magic numeric literals without a named constant
            - Stale TODO/FIXME comments left in shipped code
            - Bare `except:` clauses (no exception class)
            - `print()` calls instead of structlog `log.info(...)`

          Reply with EXACTLY one line, no preamble:
            ANTIPATTERNS: <one-line list with line refs>
            CLEAN: no style issues found
    - id: arbitrate
      kind: llm_chat
      depends_on: [review_safety, review_tests, review_style]
      with:
        system: "You arbitrate code review verdicts. Follow the priority rules exactly."
        task: |
          Three reviewers ran on the diff:
            - Safety: {{ outputs.review_safety }}
            - Tests:  {{ outputs.review_tests }}
            - Style:  {{ outputs.review_style }}

          Apply STRICTLY (higher rule wins; do NOT mix or soften):
            1. If Safety starts with "CRITICAL" → final verdict BLOCK.
               Pass through the safety reviewer's reason verbatim.
            2. Else if Safety starts with "WARNING" OR Tests starts with
               "MISSING_TESTS" → final verdict BLOCK_WITH_OVERRIDE
               (user may proceed with explicit acknowledgement; pass the
               warning(s) verbatim).
            3. Else → PASS_WITH_NOTES. If Style starts with "ANTIPATTERNS",
               append the style notes; if all three are clean, write
               "clean" instead.

          Reply with EXACTLY this structure on the first line, then
          additional lines as needed:
            BLOCK: <safety critical reason>
            BLOCK_WITH_OVERRIDE: <warning(s); user must confirm>
            PASS_WITH_NOTES: <style notes; or "clean">
---

# Codereview of Current Diff (Meta-Skill)

A **combinator-style** meta-skill that runs three independent
reviewers in parallel over the currently-uncommitted diff, then
arbitrates a single verdict with a strict priority rule.

This is the same combinator + arbitrate pattern as
`meta-security-review-bundle`, applied to a code-review domain. The
three reviewers each carry a tight, project-specific rubric — safety
focuses on injection / credentials / G1.6 contract; tests focuses on
the "new public surface ⇒ corresponding test" expectation; style
flags only a fixed list of antipatterns so it doesn't drift into
free-form opinions.

## Trigger surface

Fire by saying `multi-reviewer diff`, `codereview my diff`, or one of the
localized triggers listed in the frontmatter. The diff is read from the working
tree (`git diff --cached HEAD`, falling back to `git diff HEAD` if nothing is
staged).

## Fallback

If `read_diff` returns `NO_DIFF`, the downstream reviewers should
reply with their respective clean verdicts ("CLEAR" / "PASS" /
"CLEAN") and arbitrate emits `PASS_WITH_NOTES: clean`. If a reviewer
LLM step fails, the orchestrator's partial outputs are visible in
`step_outputs`; operator should manually re-run the failed reviewer.
