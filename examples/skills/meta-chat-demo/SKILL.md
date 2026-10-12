---
name: meta-chat-demo
description: Perform two bounded text-only model calls with explicit text handoff.
kind: meta
final_text_mode: "step:polish"
composition:
  steps:
    - id: draft
      kind: llm_chat
      with:
        system: "Write one concise sentence using only the supplied facts. Return only the sentence."
        task: "{{ inputs.user_message }}"
    - id: polish
      kind: llm_chat
      depends_on: [draft]
      with:
        system: "Polish the supplied draft without adding facts. Return only the polished sentence."
        task: "Keep the meaning of the original request: {{ inputs.user_message }}"
        draft: "{{ outputs.draft }}"
---

# Text-only model steps

Each step invokes the configured model once without tools or an Agent loop.
`polish` waits for `draft` and receives its response as named context. Set an
explicit model configuration; output wording depends on that model.
