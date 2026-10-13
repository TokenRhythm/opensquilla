import { shallowRef } from 'vue'
import type { LiveToolInputPreview, ToolInputPreview } from '@/types/toolInputPreview'
import { boundedToolInputPreviewContent } from '@/utils/chat/toolInputPreview'

/** Bounded, live-only drafts. The turn log and persisted history never own them. */
export function useToolInputPreviews() {
  const previews = shallowRef<LiveToolInputPreview[]>([])
  const accepted = new Map<string, ToolInputPreview>()

  function accept(preview: ToolInputPreview, activityOrder: number, existingOnly = false): boolean {
    if (!Number.isSafeInteger(activityOrder) || activityOrder <= 0) return false
    const previous = accepted.get(preview.previewId)
    if (existingOnly && !previous) return false
    if (previous && (previous.attemptId !== preview.attemptId
      || previous.generationEpoch !== preview.generationEpoch || previous.toolUseId !== preview.toolUseId
      || previous.revision >= preview.revision || previous.state !== 'generating')) return false
    if (!previous && accepted.size >= 64) return false
    const bounded = boundedToolInputPreviewContent(preview.content)
    preview = { ...preview, content: bounded.content, truncated: preview.truncated || bounded.truncated }
    accepted.set(preview.previewId, preview)
    const current = previews.value.find(item => item.previewId === preview.previewId)
    if (preview.state === 'discarded') {
      previews.value = previews.value.filter(item => item.previewId !== preview.previewId)
    } else if (preview.state === 'committed') {
      // Keep the row until tool-start takes it over; never invent a draft from
      // a terminal-only replay, and never erase its text during the hand-off.
      if (current) previews.value = previews.value.map(item => item.previewId === preview.previewId
        ? { ...item, revision: preview.revision, state: preview.state } : item)
    } else {
      const next: LiveToolInputPreview = {
        ...preview,
        activityOrder: current?.activityOrder ?? activityOrder,
      }
      previews.value = current
        ? previews.value.map(item => item.previewId === preview.previewId ? next : item)
        : [...previews.value, next]
    }
    return true
  }

  function handoff(toolUseId: string) {
    if (!previews.value.some(preview => preview.toolUseId === toolUseId)) return
    for (const preview of previews.value) {
      if (preview.toolUseId === toolUseId) accepted.set(preview.previewId, { ...preview, state: 'committed' })
    }
    previews.value = previews.value.filter(preview => preview.toolUseId !== toolUseId)
  }

  function clear() {
    previews.value = []
    accepted.clear()
  }
  return { previews, accept, handoff, clear }
}
