import type { Component } from 'vue'
import type { Part } from '@/types/parts'
import TextPart from '@/components/chat/parts/TextPart.vue'
import ReasoningPart from '@/components/chat/parts/ReasoningPart.vue'
import InterruptPart from '@/components/chat/parts/InterruptPart.vue'
import PlanCard from '@/components/chat/PlanCard.vue'
import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'

/**
 * Tier 1: structural registry — one renderer per discriminated part type.
 *
 * `reasoning` and `text` render through the loop in AssistantMessage. `tool`,
 * `artifact`, and `source` map to `null`: tools still render through the
 * existing `ToolCallTimeline` (their richer group data is not flattened into
 * `parts[]` yet), artifacts render whole via `ChatArtifactList`, and `source`
 * parts never appear in `parts[]` (sources fold into `message.sources`). Keeping
 * every key makes the Record exhaustive over `Part['type']` so `vue-tsc` fails
 * the build if a new part type is added without a renderer decision.
 *
 * `interrupt` parts (approval / clarify requests that block a run mid-turn)
 * render inline through `InterruptPart`, which adapts the part onto the existing
 * approval/clarify cards.
 */
export const partRegistry: Record<Part['type'], Component | null> = {
  reasoning: ReasoningPart,
  text: TextPart,
  tool: null,
  artifact: null,
  source: null,
  interrupt: InterruptPart,
  plan: PlanCard,
}

/**
 * Tier 2: per-operation tool renderer registry. Empty today — every tool renders
 * through `ToolCallTimeline`. Later changes add operation-specific entries
 * (exec.approval, clarify). Lookup falls back to the timeline when unset. This is
 * a documented seam so later changes can split tool rendering by operation without
 * touching AssistantMessage; it is not called from the part loop yet.
 */
export const toolRegistry: Record<string, Component> = {}
export const fallbackToolPart: Component = ToolCallTimeline

export function resolveToolComponent(operationKey: string): Component {
  return toolRegistry[operationKey] ?? fallbackToolPart
}
