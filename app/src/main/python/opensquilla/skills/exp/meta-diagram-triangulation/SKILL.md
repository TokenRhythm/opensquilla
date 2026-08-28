---
name: meta-diagram-triangulation
description: "Scan a target codebase path, classify the most informative diagram kind, then render it as BOTH a PlantUML source file AND a draw.io XML in parallel, and compose them into a single architecture doc. Use when writing an RFC or onboarding doc and you want a text-friendly (PlantUML) and an editable (drawio) view of the same architecture."
kind: meta
meta_priority: 55
always: false
triggers:
  - "diagram triangulation"
  - "triangulate diagrams"
  - "架构三视图"
  - "出双视图架构图"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: scan_repo
      kind: agent
      skill: history-explorer
      with:
        task: |
          Scan the target path identified in the user's invocation for
          architectural structure. Extract:
            * Top-level modules / packages under the target path
            * Inter-module import dependencies (who imports who)
            * Hotspot files (most-changed in the last 90 days)
            * Public surface: classes, functions, protocols exported via __init__.py

          User invocation (target path is somewhere in this string):
          {{ inputs.user_message | xml_escape | truncate(400) }}

          Reply with a structured summary, max 1500 chars:
            ## Target path
            <resolved absolute path>

            ## Modules (top-level)
            - <module/>: <one-line description>

            ## Dependencies
            <module> → <module>
            ...

            ## Hotspots
            - <file>: <commit count last 90d>

            ## Public surface
            - <ClassName>: <one-line role>
    - id: classify_kind
      kind: llm_classify
      depends_on: [scan_repo]
      output_choices:
        - class
        - sequence
        - component
        - deploy
        - flow
      with:
        task: |
          Based on this codebase scan, which diagram kind is most informative
          for an architecture doc? Pick ONE of:
            - class: data structures / OO hierarchy dominant
            - sequence: cross-module call flows dominant
            - component: module-level boxes + arrows dominant
            - deploy: infra / process layout dominant
            - flow: data pipeline / staged processing dominant

          Scan output:
          {{ outputs.scan_repo | truncate(1500) }}
    - id: render_plantuml
      kind: agent
      skill: sub-agent
      depends_on: [scan_repo, classify_kind]
      with:
        task: |
          Generate a PlantUML diagram source of kind `{{ outputs.classify_kind }}`
          from this codebase scan. Use idiomatic PlantUML syntax bracketed by
          `@startuml` ... `@enduml`. Aim for 10-20 boxes/arrows; do not over-render.

          Scan output:
          ---
          {{ outputs.scan_repo | truncate(2000) }}
          ---

          Write the source to: `{{ inputs.workspace_dir }}/diagrams/arch.puml`
          (create parent dir if missing; overwrite OK).

          Reply with the absolute output path on a single line, no preamble.
    - id: render_drawio
      kind: agent
      skill: sub-agent
      depends_on: [scan_repo, classify_kind]
      with:
        task: |
          Generate a draw.io XML diagram of kind `{{ outputs.classify_kind }}`
          from this codebase scan. Use valid draw.io XML:
            <mxfile><diagram><mxGraphModel><root>
              <mxCell id="0"/><mxCell id="1" parent="0"/>
              <mxCell id="N" value="..." style="..." vertex="1" parent="1">
                <mxGeometry .../>
              </mxCell>
              <mxCell ... edge="1" source="..." target="..." parent="1">
                <mxGeometry .../>
              </mxCell>
            </root></mxGraphModel></diagram></mxfile>

          Aim for 10-20 boxes/edges to mirror the PlantUML side; layout can be
          simple grid since the user is expected to re-arrange in draw.io.

          Scan output:
          ---
          {{ outputs.scan_repo | truncate(2000) }}
          ---

          Write the XML to: `{{ inputs.workspace_dir }}/diagrams/arch.drawio`
          (create parent dir if missing; overwrite OK).

          Reply with the absolute output path on a single line, no preamble.
    - id: compose_doc
      kind: agent
      skill: docx
      depends_on: [render_plantuml, render_drawio]
      with:
        task: |
          Compose an architecture document that references both diagram
          deliverables. The doc body should contain three sections:

          1. **Scope** — restate the user's invocation target.
             User invocation: {{ inputs.user_message | xml_escape | truncate(200) }}
          2. **Diagram kind** — `{{ outputs.classify_kind }}` (one paragraph
             justifying why this kind was chosen).
          3. **Deliverables** — two bullet items linking the files:
             - PlantUML source: {{ outputs.render_plantuml }}
             - draw.io file: {{ outputs.render_drawio }}

          Save to: `{{ inputs.workspace_dir }}/diagrams/arch.docx`. Reply
          with the absolute output path on a single line, no preamble.
    - id: persist
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [compose_doc]
      tool_args:
        path: "memory/architecture-snapshots.md"
        mode: append
        content: |
          === diagram triangulation ===
          invocation: {{ inputs.user_message | xml_escape | truncate(200) }}
          diagram_kind: {{ outputs.classify_kind }}
          plantuml: {{ outputs.render_plantuml | truncate(200) }}
          drawio: {{ outputs.render_drawio | truncate(200) }}
          docx: {{ outputs.compose_doc | truncate(200) }}
---

# Diagram Triangulation (Meta-Skill)

A **classifier + parallel render** meta-skill. After scanning a target
path, an `llm_classify` step picks the most informative diagram kind
(one of `class | sequence | component | deploy | flow`), then **two
independent render branches** synthesize PlantUML source and draw.io
XML for the same scan — running in parallel because they are
independent tools serving different downstream uses (git-friendly text
review vs. editable canvas).

## Trigger surface

Fire by saying `diagram triangulation` or one of the localized triggers listed
in the frontmatter, with the target path or module reference in the same turn.

## Fallback

If either render step fails, `compose_doc` should still produce the
docx referencing whichever render succeeded; manually re-run the
failed render via `sub-agent` with the same scan output as input.
