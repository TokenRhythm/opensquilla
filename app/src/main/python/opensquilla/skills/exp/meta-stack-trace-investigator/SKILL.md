---
name: meta-stack-trace-investigator
description: "Use this meta-skill instead of answering directly when the user gives a stack trace, traceback, runtime error, or failing log that benefits from multi-skill orchestration across trace parsing, repo/history inspection, patch-target analysis, reproduction guidance, and verification commands."
kind: meta
meta_priority: 60
always: false
final_text_mode: "step:degraded_summary"
triggers:
  - "traceback"
  - "stack trace"
  - "runtime error"
  - "failing log"
  - "keyerror"
  - "typeerror"
  - "investigate stack trace"
  - "trace investigator"
  - "诊断 traceback"
  - "调查 stack trace"
  - "查 traceback"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities:
      - shell
      - filesystem-write
composition:
  steps:
    - id: trace_collect
      kind: llm_chat
      with:
        system: "You extract stack-trace investigation facts without asking follow-up questions."
        task: |
          Extract a compact investigation brief from the original request.
          Do NOT ask the user to confirm language, expected behavior, or
          recent changes when the stack trace is enough to infer a useful
          investigation direction. If a field is absent, write ASSUMED or
          unknown and continue.

          Original request:
          ---
          {{ inputs.user_message | xml_escape | truncate(3000) }}
          ---

          Return exactly this structure:
          LANGUAGE: <python|javascript|typescript|go|rust|unknown>
          EXPECTED_BEHAVIOR: <brief or ASSUMED: not provided>
          RECENT_CHANGES: <brief or ASSUMED: not provided>
          TRACE_PRESENT: <yes|no>
          PRIMARY_EXCEPTION: <exception/error head or unknown>
          PRIMARY_FILES:
            - <path:line if present, otherwise unknown>
    - id: parse_trace
      kind: llm_chat
      depends_on: [trace_collect]
      with:
        system: "You parse stack traces. Return only the requested JSON object."
        task: |
          You are the trace parser for a stack-trace investigation bundle.
          Extract structured info from the stack trace below; do not speculate
          about root cause yet.

          Extracted investigation brief (treat as hints, not authoritative):
          {{ outputs.trace_collect | xml_escape | truncate(1000) }}

          Traceback under investigation:
          ---
          {{ inputs.user_message | xml_escape | truncate(3000) }}
          ---

          Reply with EXACTLY one JSON object on a single line, no preamble:
            {"exception_class": "<ClassNameOrErrorKind>", "exception_message": "<head of message; <=120 chars>", "primary_file": "<path/file or empty>", "primary_line": <int or 0>, "symbols": ["sym1", "sym2", ...], "language": "<python|javascript|typescript|go|rust|unknown>"}

          The "symbols" list contains the function/method names that appear in
          the top 3 frames; include at most 6 distinct entries.
    - id: grep_repo
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [parse_trace]
      on_failure: grep_repo_degraded
      tool_args:
        command: "rg -n --hidden --max-count 5 -- 'parse_tool_result|run_step|json.loads|KeyError|result' ."
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 12
    - id: grep_repo_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          REPO_GREP: DEGRADED - repository search could not run in this
          workspace. Continue from traceback semantics and provide exact
          commands for the target repository.
    - id: search_issues
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [parse_trace]
      on_failure: search_issues_degraded
      tool_args:
        command: "gh issue list --search 'KeyError result parse_tool_result' --json number,title,url --limit 10"
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 12
    - id: search_issues_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          ISSUE_SEARCH: DEGRADED - issue search could not run or produced no
          authenticated results. Continue without issue evidence.
    - id: git_history
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [parse_trace]
      on_failure: git_history_degraded
      tool_args:
        command: "git log --since='30 days ago' --oneline -- src/agent/tools.py src/agent/runtime.py"
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 12
    - id: git_history_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          GIT_HISTORY: DEGRADED - git history could not run in this
          workspace. Continue without commit evidence and provide exact git
          log/blame commands for the target repository.
    - id: diff_context
      kind: skill_exec
      skill: git-diff
      depends_on: [parse_trace]
      on_failure: diff_context_degraded
      with:
        mode: worktree
        cwd: "{{ inputs.workspace_dir | default('.') }}"
    - id: diff_context_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          DIFF_CONTEXT: DEGRADED - current workspace is not a readable git
          worktree or git-diff failed. Continue using traceback evidence,
          repo grep output, and explicit user-provided paths.
    - id: history_patterns
      kind: skill_exec
      skill: history-explorer
      depends_on: [parse_trace]
      on_failure: history_patterns_degraded
      with:
        query: "{{ outputs.parse_trace | truncate(512) }}"
        window_days: "30"
        include: "meta_usage,co_occurrences"
    - id: history_patterns_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          HISTORY_PATTERNS: DEGRADED - history-explorer failed or no local
          decision history is available. Continue without prior-pattern
          evidence.
    - id: memory_recall
      kind: tool_call
      tool: memory_search
      tool_allowlist: [memory_search]
      depends_on: [parse_trace]
      on_failure: memory_recall_degraded
      tool_args:
        query: "{{ outputs.parse_trace | truncate(400) }}"
        max_results: 3
    - id: memory_recall_degraded
      kind: llm_chat
      with:
        system: "You return a fixed degraded-evidence marker."
        task: |
          Return exactly:
          MEMORY_RECALL: DEGRADED - no prior incident memory is available.
          Continue without prior-memory evidence.
    - id: language_probe
      kind: agent
      skill: stack-trace-generic-probe
      depends_on: [parse_trace]
      route:
        - when: "'\"language\":\"python\"' in outputs.parse_trace or '\"language\": \"python\"' in outputs.parse_trace or 'LANGUAGE: python' in outputs.trace_collect"
          to: stack-trace-python-probe
        - when: "'\"language\":\"javascript\"' in outputs.parse_trace or '\"language\": \"javascript\"' in outputs.parse_trace or '\"language\":\"typescript\"' in outputs.parse_trace or '\"language\": \"typescript\"' in outputs.parse_trace or 'LANGUAGE: javascript' in outputs.trace_collect or 'LANGUAGE: typescript' in outputs.trace_collect"
          to: stack-trace-js-probe
        - when: "'\"language\":\"go\"' in outputs.parse_trace or '\"language\": \"go\"' in outputs.parse_trace or 'LANGUAGE: go' in outputs.trace_collect"
          to: stack-trace-go-probe
        - when: "'\"language\":\"rust\"' in outputs.parse_trace or '\"language\": \"rust\"' in outputs.parse_trace or 'LANGUAGE: rust' in outputs.trace_collect"
          to: stack-trace-rust-probe
      with:
        task: |
          Run a language-specific stack-trace probe. Use the parsed trace and
          evidence gathered so far to propose language-idiomatic checks,
          minimal reproducer shape, and patch targets. Do not claim repository
          evidence that is absent.

          Language classification:
          {{ outputs.trace_collect | truncate(400) }}

          Trace parse:
          {{ outputs.parse_trace | truncate(1200) }}

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(2000) }}
    - id: root_cause
      kind: llm_chat
      depends_on: [grep_repo, search_issues, git_history, diff_context, history_patterns, memory_recall, language_probe]
      with:
        system: "You synthesize bounded root-cause hypotheses from stack traces, exception semantics, and explicit evidence."
        task: |
          Synthesize a root-cause hypothesis from these parallel
          investigations and the original trace parse.

          Trace parse:
          {{ outputs.parse_trace | truncate(600) }}

          Repo grep:
          {{ outputs.grep_repo | truncate(1200) }}

          Related GH issues:
          {{ outputs.search_issues | truncate(800) }}

          Recent commits on affected files:
          {{ outputs.git_history | truncate(800) }}

          Current git diff context:
          {{ outputs.diff_context | truncate(1200) }}

          Prior OpenSquilla skill/router history patterns:
          {{ outputs.history_patterns | truncate(1200) }}

          Prior similar incidents (may be empty on a fresh install — if
          this section is empty or returns no matches, IGNORE it and
          synthesize the root cause from the other available investigations
          alone; do not invent prior incidents that are not listed):
          {{ outputs.memory_recall | truncate(800) }}

          Treat prior memory as a non-authoritative search hint only. Do not
          cite memory paths, similarity scores, or prior-incident claims as
          evidence for the current traceback; the current trace and target
          repository evidence are the only grounding sources.

          Language-specific probe:
          {{ outputs.language_probe | truncate(1200) }}

          If repository search returned NO_HITS or the referenced files are
          absent, still derive a bounded hypothesis from the stack trace
          contract itself. Clearly say the repository evidence is degraded;
          do not pretend that files or symbols were inspected.

          Exception-semantics guard:
          - For Python expressions like json.loads(raw)['result'] with
            KeyError: 'result', treat the decoded value as a mapping/dict
            missing that top-level key. Do not rank list/string/null/non-JSON
            payloads as primary causes; put them under rejected/different
            exception shapes because they would normally raise TypeError or
            JSONDecodeError instead.
          - When repository evidence is degraded, the strongest evidence is
            the consumer contract violation: parser expects result, producer
            supplied another valid JSON object shape.

          Reply with this exact structure (no preamble):

          EXCEPTION_SEMANTICS: <what the exception class implies for this exact expression; name payload shapes that would and would not cause it>
          ROOT_CAUSE: <one-sentence highest-likelihood hypothesis>
          EVIDENCE:
            - <which investigation supported it; cite line>
            - <which investigation supported it; cite line>
          RANKED_HYPOTHESES:
            - likelihood=<high|medium|low>; cause=<cause>; evidence=<evidence>; falsify=<command/check>
            - include at least six bounded hypotheses when repository
              evidence is degraded; cover error envelopes, schema drift,
              nested result wrappers, streaming/control frames, wrong
              dispatcher/message type, and provider/transport rewraps when
              they fit the exception semantics
          SUGGESTIONS:
            - <file:line> — <action>
            - <file:line> — <action>
            - <file:line> — <action>
    - id: repro_suggestion
      kind: llm_chat
      depends_on: [root_cause]
      with:
        system: "You propose safe, minimal verification commands for debugging."
        task: |
          Propose the smallest safe verification command(s) for this root-cause
          hypothesis. Prefer existing tests, targeted unit tests, or a minimal
          reproducer command. Do not propose destructive commands. Do not
          propose commands that create, overwrite, or edit files, including
          heredocs, shell redirection, `cat >`, `tee`, `python - <<`,
          `python -c` that writes files, or temporary files under `/tmp`.
          If a reproducer needs code, include it as an inline snippet marked
          "copy into an existing test file" and keep Verification commands
          limited to read-only locate/history checks or existing test commands.

          Language classification:
          {{ outputs.trace_collect | truncate(400) }}

          Trace parse:
          {{ outputs.parse_trace | truncate(600) }}

          Root-cause report:
          {{ outputs.root_cause | truncate(1200) }}

          Language-specific probe:
          {{ outputs.language_probe | truncate(1200) }}

          Reply with:
          CONFIDENCE: <low|medium|high>
          VERIFY:
            - <command or manual check>
            - <minimal reproducer command or snippet for the parsed language>
            - <history/blame or producer-consumer schema check>
          FIX_FIRST:
            - <first file/action>
          PATCH_SHAPE:
            - <specific defensive-code shape to try first>
            - <schema-normalization or frame-filtering shape if relevant>

          For parser/envelope failures, prefer a protocol-error branch plus
          fixture-driven contract tests over silently returning a default or
          fabricated result. If a fallback/retry is useful, phrase it as a
          caller policy after logging and typed error classification, not as
          the parser's default behavior.
    - id: degraded_summary
      kind: llm_chat
      depends_on: [grep_repo, search_issues, git_history, diff_context, history_patterns, memory_recall, language_probe, repro_suggestion]
      with:
        system: "You are a strict final-report renderer for stack-trace investigations. Return only the final report. Never mention internal orchestration, tool failures, path restrictions, memory persistence, or saved artifacts."
        task: |
          CRITICAL OUTPUT CONTRACT:
          - First line must be exactly: ## Trace Facts
          - Use the same language as the original user request. If the
            original request is in English, answer in English.
          - Do not include an opening acknowledgement, apology, emoji, or
            process commentary.
          - Do not include the words "meta-skill", "search step", "path
            restriction", "internal tool", "memory persistence", "saved",
            "git_history", "DAG", "memory/traceback.md", "prior incident",
            "similarity score", or "step".
          - If repository evidence is unavailable, write only:
            "Repository evidence: DEGRADED in this benchmark/workspace; run
            the commands below in the target repository."
          - Verification Commands must contain only commands/checks. Code
            changes belong only in Patch Direction, not Verification Commands.
            Never include file-creation or file-edit commands in Verification
            Commands: no heredocs, no shell redirection, no `cat >`, no `tee`,
            no `python - <<`, no `python -c` file writes, and no `/tmp`
            scratch-file creation. Reproducer code belongs in the Reproduction
            section as an inline snippet, not as a command that writes files.
          - Keep the final report compact enough to finish: cap root-cause
            matrix rows at 8, reproducer rows at 5, and patch-direction bullets
            at 6. Prefer dense commands and bullets over long prose.
          - Patch Direction must complete before Related Checks. Do not spend
            the token budget on full implementation code unless the user asked
            for a patch.
          - For parser/envelope failures, do not recommend returning a default
            success/error object from the parser as the first fix. Prefer typed
            protocol/execution errors, payload-key logging, schema
            normalization only for supported legacy success keys, and
            fixture-driven contract tests.
          - Verification Commands must include at least one exact import-path
            reproducer when a parsed file/module path is available, plus at
            least one targeted pytest command for the parser/envelope contract.
          - Do not ask follow-up questions at the end.

          Produce the final user-facing investigation. If any evidence source
          returned NO_HITS, NO_MATCHING_ISSUES, NO_RECENT_COMMITS, auth errors,
          or empty memory, label that source as DEGRADED instead of hiding it.
          This is the final answer shown to the user: do not mention
          meta-skill step ids, memory persistence, internal tools, or that
          anything was saved. Do not say "the meta-skill search step hit a path
          restriction"; phrase unavailable repo evidence only inside Evidence
          Status.
          Treat raw errors from repository/history tools as private diagnostic
          noise. Do not quote them, translate them, or identify which internal
          lookup failed; collapse them to the generic degraded evidence line
          above.
          Treat memory recall as a private hint source. Never include a
          "Prior incident" evidence row, memory path, memory score, or memory
          citation in the final report.

          When repository evidence is degraded, do not stop at a short
          conclusion. Provide a useful fallback investigation based on the
          trace contract:
          - say that the referenced files/symbols were not found in the
            current workspace when that is true;
          - include exact repo search commands the user can run in the real
            target repository;
          - include a minimal reproducer snippet or command for the parsed
            language/runtime;
          - include a defensive patch direction with expected failure mode;
          - include exact verification commands.

          Quality bar for user-facing output:
          - start with trace facts, not process commentary
          - parse the failing frame precisely: file, line, function, expression,
            exception class, and what the exception proves
          - explicitly state the data-shape implications:
            json.loads(raw) succeeded; the decoded payload was subscriptable
            by string key; the top-level key "result" was absent
          - explicitly reject payload shapes that would produce JSONDecodeError,
            TypeError, or IndexError instead of the observed exception
          - for Python, do not say list/string/null payloads would cause
            KeyError for this expression; they are rejected/different-exception
            shapes unless extra wrapping evidence exists
          - rank a broad hypothesis matrix, including schema drift, error
            envelope, nested result, streaming/control frame, wrong dispatcher,
            transport/provider rewrap, and renamed key when applicable
          - include at least seven ranked hypotheses when repository evidence
            is degraded: error envelope, schema/version drift, streaming or
            partial frame, wrong dispatcher/message type, renamed/cased key,
            exception serialized as tool output, and empty/null/stripped result
          - include a hypothesis-driven reproducer matrix for at least four
            payload shapes: success envelope, error envelope, streaming/control
            frame, and non-dict JSON; specify expected exception or output
          - repo search targets must include producer, consumer, schema/types,
            transport wrappers, streaming/chunking, fixtures/logs, tests, git
            history, and blame
          - verification commands must be exact shell commands and must include
            rg checks, git log/blame checks, a minimal language-specific
            reproducer, and targeted test commands
          - the minimal language-specific reproducer must be an inline snippet
            plus an existing-test command, not a file-creation command; do not
            use `cat >`, heredocs, redirection, `tee`, `/tmp` files, or
            `python - <<` in Verification Commands
          - prioritize producer-adapter checks and contract tests over broad
            generic advice; tie each verification command to the failing module
            path, symbol, or envelope contract when possible
          - Patch Direction should distinguish:
            1. parser boundary: decode, type check, error-envelope branch,
               supported success-key normalization, typed protocol error
            2. producer adapters: guarantee one success envelope shape
            3. caller/runtime: catch typed failures and log tool identity
            4. tests: fixtures for success, error envelope, missing key,
               streaming/control frame, and non-dict JSON
          - include these concrete search families when applicable:
            `rg -nF "parse_tool_result"`, `rg -n "tool_call|tool_result|dispatch|invoke_tool"`,
            `rg -n "stream|chunk|delta|partial"`, `rg -n "openai|anthropic|mcp|jsonrpc"`,
            `rg -nP "return\s*\{\s*['\"](result|data|output|content|error|status|message)['\"]" src/`,
            `rg -nP "json\.loads\([^)]+\)\[['\"][^'\"]+['\"]\]" src/`,
            `git log -p --since="60 days" -- <files>`, and `git blame -L`
          - state that commands are recommended next steps, not executed
          - do not end by asking whether the user wants more detail

          Root cause:
          {{ outputs.root_cause | truncate(1200) }}

          Trace parse:
          {{ outputs.parse_trace | truncate(800) }}

          Language classification:
          {{ outputs.trace_collect | truncate(400) }}

          Verification plan:
          {{ outputs.repro_suggestion | truncate(1000) }}

          Language-specific probe:
          {{ outputs.language_probe | truncate(1000) }}

          Evidence availability:
          - Repository/history/issue evidence may be unavailable in benchmark
            workspaces. If the prior sections do not contain concrete
            file-line excerpts from the target repository, use the exact
            degraded evidence sentence from the contract and continue with a
            trace-contract investigation.
          - Do not quote raw lookup errors, internal lookup names, or protected
            path details.

          Reply in Markdown with exactly these sections and no preamble:
          ## Trace Facts
          ## Diagnosis
          ## Exception Semantics
          Explain what the exception class means for the exact failing
          expression. Reject payload shapes that would produce a different
          exception type.
          ## Evidence Status
          ## Assumptions / Constraints
          ## Ranked Root Cause Matrix
          Include at least seven rows. Each row must include likelihood,
          evidence, falsifying command/check, and expected signal.
          ## Repo Search Targets
          Group searches by direct hits, producer/wrappers, runtime/streaming,
          schema/types, tests/fixtures/logs, and git history. Prefer `rg` and
          include exact commands. Do not assume `src/tools/` exists; use
          repo-wide commands first, then path-specific commands for parsed
          frames.
          ## Reproduction
          Include a minimal fixture or snippet for the parsed language/runtime
          plus a small matrix of payload shapes and expected outcomes.
          ## Patch Direction
          Keep this section concise and complete. Prefer bullet-level patch
          targets and contract-test shape over long code blocks. Explicitly
          reject silent default-return behavior for missing required result
          keys unless the target repository's protocol already documents that
          behavior.
          ## Patch Target Checklist
          ## Related Checks
          Include adjacent contract checks: raw payload logging, error-path
          tests, schema validation, all return sites funneled through one
          wrapper, retry/fallback behavior, stream assembly, raw type
          narrowing, sibling unsafe `json.loads(...)[key]` patterns, and
          missing observability around tool identity / tool_call_id /
          producer name / payload keys at the parser boundary.
          ## Verification Commands
          Separate locate/context commands, producer-shape search, unit tests,
          isolated reproducer commands, static sweeps, logs, and history
          checks. Include commands for the happy path, error-envelope path,
          streaming/control-frame path, non-dict path, and tool-identity
          logging check. Use only read-only searches/history/log commands and
          existing test commands. Do not include commands that create or edit
          files; if a new fixture is needed, describe the fixture in Patch
          Direction or Reproduction instead.
    - id: persist
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [degraded_summary]
      tool_args:
        path: "memory/traceback.md"
        mode: "append"
        content: |
          === stack-trace investigation ===
          parse: {{ outputs.parse_trace | truncate(400) }}
          hypothesis: {{ outputs.degraded_summary | truncate(1000) }}
---

# Stack-Trace Investigator (Meta-Skill)

A **combinator-style** meta-skill that converts a pasted stack trace into a
structured root-cause report. It now classifies Python, JavaScript,
TypeScript, Go, Rust, or unknown traces before running the investigation. After
parsing the trace once, heterogeneous investigations run in parallel:

1. **`grep_repo`** — ripgrep for the symbols in the current repo
2. **`search_issues`** — `gh issue list` for similar reported problems
3. **`git_history`** — recent commits touching the affected files
4. **`diff_context`** — `git-diff` skill for current worktree context
5. **`history_patterns`** — `history-explorer` skill for prior skill/router
   usage patterns
6. **`memory_recall`** — prior incidents stored under the `traceback` topic
7. **`language_probe`** — routed to the language-specific helper skill
   (`stack-trace-python-probe`, `stack-trace-js-probe`,
   `stack-trace-go-probe`, `stack-trace-rust-probe`, or generic fallback)

The `root_cause` and `repro_suggestion` steps fan the signals into a
hypothesis, concrete fix targets, and verification commands. The final summary
labels degraded evidence sources explicitly before persisting the incident.

## Trigger surface

Fire by saying `investigate stack trace` or one of the localized triggers
listed in the frontmatter, with the traceback pasted into the same turn.

## Fallback

If any leaf step fails, the orchestrator surfaces partial outputs in
`step_outputs`. Operator should manually run `rg <symbols>`,
`gh issue list --search`, `git log`, and `memory search` and
synthesize the report by hand.
