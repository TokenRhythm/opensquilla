---
name: meta-migration-assistant
description: "Use this meta-skill instead of answering directly when the user needs a concrete migration plan that benefits from multi-skill orchestration across migration classification, authoritative guide lookup, optional repo diff inspection, and step-by-step validation planning."
kind: meta
meta_priority: 50
always: false
final_text_mode: "step:write_plan"
triggers:
  - "migration plan"
  - "migration checklist"
  - "practical migration checklist"
  - "migrate from"
  - "migrate"
  - "upgrade from"
  - "CommonJS to native ESM"
  - "CommonJS to ESM"
  - "CJS to ESM"
  - "native ESM"
  - "rollout risks"
  - "升级指南"
  - "迁移方案"
  - "迁移步骤"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: migration_intake
      kind: llm_chat
      with:
        system: "You extract migration request boundaries and decide whether clarification is required."
        task: |
          Extract the requested migration source, target, version context, and
          repository scope from the user request. Set NEEDS_CLARIFICATION: yes
          only when the source or target stack is absent or the request is too
          generic to classify safely.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1400) }}

          Return exactly:
          SOURCE_STACK: <source stack/version, or MISSING_SOURCE_STACK>
          TARGET_STACK: <target stack/version, or MISSING_TARGET_STACK>
          VERSION_CONTEXT: <version/runtime/package context, or unknown>
          REPO_SCOPE: <current diff|current branch|named repo|not specified>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <source_stack|target_stack|none>
          CLARIFY_REASON: <one concise reason, or none>
    - id: migration_clarify
      kind: user_input
      depends_on: [migration_intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.migration_intake"
      clarify:
        mode: form
        intro: |
          迁移目标还不够明确。请补齐源技术栈和目标技术栈，我再生成可执行迁移清单。
        nl_extract: true
        fields:
          - name: source_stack
            type: string
            required: true
            prompt: "源技术栈/版本 / Source stack or version"
            max_chars: 160
          - name: target_stack
            type: string
            required: true
            prompt: "目标技术栈/版本 / Target stack or version"
            max_chars: 160
          - name: version_context
            type: string
            prompt: "运行时、框架或包版本 / Runtime, framework, or package versions"
            max_chars: 240
          - name: repo_scope
            type: string
            prompt: "仓库范围 / Repository scope"
            max_chars: 200
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: classify
      kind: llm_classify
      depends_on: [migration_intake, migration_clarify]
      output_choices:
        - PY2_TO_PY3
        - VUE2_TO_VUE3
        - REACT_CLASS_TO_HOOKS
        - OPENAI_V0_TO_V1
        - CJS_TO_ESM
        - OTHER
      with:
        text: |
          User said: {{ inputs.user_message | xml_escape | truncate(1400) }}

          Migration intake:
          {{ outputs.migration_intake | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('migration_clarify', {}) | tojson }}

          Identify the migration kind.
          Ignore benchmark wrappers, timestamps, locale hints, and generic
          "return inline" instructions. The actual user request may appear
          after benchmark constraints; classify from the explicit source and
          target migration words, not from the preamble.

          Decision rules:
          - PY2_TO_PY3        → mentions Python 2 → 3 / py2 → py3
          - VUE2_TO_VUE3      → mentions Vue 2 → Vue 3 / options → composition
          - REACT_CLASS_TO_HOOKS → React class component → hooks
          - OPENAI_V0_TO_V1   → openai SDK v0 → v1, ChatCompletion → chat.completions.create
          - CJS_TO_ESM        → any mention of CommonJS, CJS, native ESM,
            require(), module.exports, exports.*, import/export migration,
            "from CommonJS to native ESM", or "CJS to ESM"
          - OTHER             → any other migration request

          If the text contains "CommonJS" and "native ESM", return exactly
          CJS_TO_ESM even if other benchmark/context words appear first.
    - id: fetch_guide
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [classify, migration_clarify]
      with:
        max_results: 8
        engines:
          - duckduckgo
          - brave
        query: |
          Authoritative migration guide for the user's actual migration.
          Classifier verdict: {{ outputs.classify }}.
          Migration intake:
          {{ outputs.migration_intake | truncate(1000) }}
          Clarification answers:
          {{ inputs.get('collected', {}).get('migration_clarify', {}) | tojson }}
          If the request mentions CommonJS, CJS, native ESM, require(),
          module.exports, or import/export migration, search specifically for
          current CommonJS to native ES Modules migration guidance covering
          package.json type/exports, extension rules, directory imports,
          JSON imports/import attributes, createRequire interop, TypeScript
          NodeNext, test runners, dual publish, and consumer compatibility.
          Ignore benchmark preambles, timestamps, and unrelated local context.

          User request:
          {{ inputs.user_message | xml_escape | truncate(500) }}
    - id: repo_context
      kind: skill_exec
      skill: git-diff
      depends_on: [classify]
      when: |
        (
          'current diff' in (inputs.user_message | lower)
          or 'this diff' in (inputs.user_message | lower)
          or 'current branch' in (inputs.user_message | lower)
          or 'pull request' in (inputs.user_message | lower)
          or 'merge request' in (inputs.user_message | lower)
          or 'already changed' in (inputs.user_message | lower)
          or 'worktree' in (inputs.user_message | lower)
          or '当前 diff' in inputs.user_message
          or '当前分支' in inputs.user_message
        )
      with:
        mode: cached_fallback_worktree
    - id: write_plan
      kind: llm_chat
      depends_on: [classify, migration_clarify, fetch_guide, repo_context]
      with:
        system: |
          You write migration checklists. Answer the user's requested
          migration only. Do not change the migration domain based on locale,
          timezone, unrelated repository diffs, or general environment
          context. If the request mentions CommonJS, CJS, native ESM,
          require/module.exports, or import/export migration, the answer must
          be exclusively about CommonJS to native ESM. Return Markdown directly;
          do not wrap the entire answer in a fenced code block.
        task: |
          Migration kind: {{ outputs.classify }}
          Migration intake:
          {{ outputs.migration_intake | truncate(1200) }}
          Clarification answers:
          {{ inputs.get('collected', {}).get('migration_clarify', {}) | tojson }}
          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Hard override before writing:
          - If the user request contains CommonJS, CJS, native ESM,
            require/module.exports, or import/export migration wording, set
            EFFECTIVE_KIND=CJS_TO_ESM.
          - If EFFECTIVE_KIND=CJS_TO_ESM, the Summary heading and every
            section must be about CommonJS to native ES Modules. Do not write
            about timezone, cloud, Python, Vue, React hooks, OpenAI SDK, or
            any unrelated migration.
          - If classifier output conflicts with explicit source/target words
            in the user request, ignore the classifier output.

          Authoritative guide excerpt:
          {{ outputs.fetch_guide | truncate(2000) }}

          Optional repository diff context:
          {{ outputs.repo_context | truncate(3000) }}

          Task lock:
          - The user's requested migration kind is authoritative. Do not let
            repository diff context or unrelated local changes redefine the
            migration. If the user asks for CommonJS/CJS to native ESM, the
            final answer must be a CommonJS-to-ESM migration checklist.
          - Apply this final-layer classifier override: if the user request
            contains CommonJS, CJS, native ESM, require/module.exports, or
            import/export migration wording, treat the effective migration
            kind as CJS_TO_ESM even if `outputs.classify` says OTHER.
          - Do not expose classifier labels such as OTHER, CJS_TO_ESM,
            VUE2_TO_VUE3, or internal routing decisions in the final answer.
          - Repository context is optional and only evidentiary. If it does
            not explicitly show files relevant to the classified migration,
            ignore it and say repository files were not verified.
          - Do not invent repo-specific files, package names, scripts, or
            changes. Use grep commands for discovery instead.
          - Do not include write/commit commands such as `git add`,
            `git commit`, `eslint --fix`, or codemods inside validation
            sections. Validation commands are read-only.
          - Benchmark/no-write constraint: the final answer must not include
            file creation or mutation commands in validation or smoke-test
            sections. Forbidden examples include heredocs, shell redirection
            (`>`, `>>`), `cat >`, `tee`, `touch`, `mkdir`, `cp`, `mv`,
            `rm`, `npm init`, config generators, codemods, and `python -c`
            / `node -e` snippets that write files. If a smoke test normally
            needs a temporary fixture, describe it as a manual implementation
            step, not as a validation command.
          - Do not use unverified concrete entrypoint paths such as
            `index.js`, `server.js`, or package-specific test commands unless
            they came from repository evidence. Use discovery commands first
            and phrase path-specific commands as templates.
          - If the authoritative guide excerpt is about a different migration
            domain, ignore it completely. Do not mention that it was ignored.
          - Do not wrap the whole response in ```markdown fences.

          Produce a concrete migration checklist as Markdown with these sections:
          ## Summary
          ## Evidence boundary
          ## Breaking changes
          ## Step-by-step
          ## Repository discovery checklist
          ## Files likely affected (grep patterns the user can run)
          ## Validation (tests/checks to confirm the migration)
          ## Rollout and rollback

          Quality contract:
          - Keep the answer dense and complete in one response. Target
            1,200-1,800 words; prefer compact tables and bullets over long
            explanations or large config snippets.
          - If repository context is empty, unreadable, or generic, say so
            clearly; do not invent files or package names.
          - If repository context is present but about another migration,
            ignore it and explicitly state that it is unrelated to the user's
            requested migration.
          - Prefer `rg` commands for grep patterns and make them copyable.
          - For CJS_TO_ESM discovery, include these command families:
            `node -v`, `npm pkg get type main exports scripts`,
            `git grep -nE "require\(|module\.exports|exports\."`,
            `git grep -nE "require\([^'\"\`]+\)"`,
            `git grep -nE "from ['\"]\./[^'\"]+['\"]|import\(['\"]\./[^'\"]+['\"]\)"`,
            `git grep -nE "\b(__dirname|__filename)\b"`,
            `git grep -nE "require\.(resolve|cache|main|extensions)"`,
            `git grep -nE "require\(['\"][^'\"]+\.json['\"]\)"`,
            `git ls-files '*.cjs' '*.mjs'`, and
            `git ls-files '**/package.json' | xargs grep -l '"main"\|"type"\|"exports"'`.
          - Validation should be hypothesis-driven: include separate checks
            for happy path package load, dynamic require replacement,
            extension/index-resolution failures, JSON import behavior,
            test-runner ESM behavior, and downstream consumer import/require
            smoke tests.
          - Express validation commands as read-only probes against existing
            repository files. For example, use `node --check <existing-file>`,
            `node --input-type=module -e "import('package-name-or-path')"`,
            `npm test -- --runInBand` only when scripts exist, and
            `git grep`/`npm pkg get` discovery before any path-specific
            command. Never ask the user to create `tmp-smoke.*` files.
          - Include package/publication validation commands when relevant:
            `npm pack --dry-run`, `npx publint`, and
            `npx @arethetypeswrong/cli --pack .` for published packages with
            TypeScript types. Label these as optional if the package is not
            published.
          - For rollout, include decision points: ESM-only versus dual publish,
            semver-major trigger, consumer compatibility scan, canary/internal
            package release, package-tarball inspection, rollback to previous
            package version, and keeping `.cjs` escape hatches for tooling.
          - For CJS_TO_ESM, cover at minimum: `type: module`, extension rules,
            directory `index.js` imports, `__dirname`/`__filename`, JSON
            imports, CJS interop/default export shape changes, `exports` map
            subpath whitelisting and precedence, TypeScript `NodeNext`,
            Jest/Vitest or node:test, dual-package hazards, downstream
            consumer smoke tests, release sequencing, and rollback.
          - Do not say that `exports` deprecates `main`; say that `exports` takes precedence for runtimes that support it and can restrict deep imports.
          - Mark all validation commands as commands for the user to run; do
            not imply they were executed.
          - Include version caveats for Node/tooling-specific behavior.
          - For JSON imports, avoid claiming one universal syntax. Say that
            behavior differs by Node version/tooling; verify the target
            runtime's current JSON-module/import-attributes support, and keep
            `createRequire(import.meta.url)` as the conservative fallback when
            needed.
          - Avoid invented loader placeholders such as
            `node --loader <your-loader>` unless the repository evidence
            shows an existing loader. Prefer concrete discovery commands.
          - Avoid write-implying validation examples such as `eslint --fix`;
            validation should be read-only unless clearly labeled as a
            separate codemod/fix step.
          - Avoid file-creation or config-generation examples unless they are
            explicitly labeled as implementation work, not validation. Keep
            the checklist concise and decision-oriented instead of filling it
            with large generic config snippets.
          - Do not include brittle placeholder commands such as
            `node --loader <your-loader>` or `node --check <existing-file>`;
            write discovery-first templates with clear placeholders only where
            unavoidable.
          - Avoid obsolete Node flags such as `--experimental-modules`; prefer
            current LTS-compatible commands and call out when a command is
            version-specific.
---

# Migration Assistant (Meta-Skill)

Take a "help me migrate X → Y" request and produce a concrete, runnable
checklist. The pipeline does four things:

1. **classify** the migration kind via an LLM tag (one of six tokens).
2. **fetch_guide** the most authoritative source for THAT migration:

   | Classifier verdict          | Best source            | Routed skill          |
   |-----------------------------|------------------------|-----------------------|
   | `OPENAI_V0_TO_V1`           | repo release notes     | `github`              |
   | `PY2_TO_PY3`                | framework migration doc| `multi-search-engine` |
   | `VUE2_TO_VUE3`              | framework migration doc| `multi-search-engine` |
   | `REACT_CLASS_TO_HOOKS`      | framework migration doc| `multi-search-engine` |
   | `CJS_TO_ESM`                | (fuzzy, synthesize)    | `multi-search-engine` |
   | `OTHER` (default)           | (synthesize)           | `deep-research`       |

3. **repo_context** optionally inspects the current repo diff only when the
   prompt indicates that local repository context should shape the migration.

4. **write_plan** uses a constrained `llm_chat` renderer so explicit source
   and target terms in the user request remain authoritative even when
   repository context or retrieved guide text is noisy.

## Fallback

If the orchestration fails: ask the user to specify the migration tag
manually, run the matching skill yourself, then write the checklist.
