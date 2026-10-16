import { shallowRef } from 'vue'
import type { LiveToolInputPreview, ToolInputPreview } from '@/types/toolInputPreview'
import { createToolInputPreviewProjection } from '@/utils/chat/toolInputPreview'

/** Bounded, live-only drafts. The turn log and persisted history never own them. */
export function useToolInputPreviews() {
  const previews = shallowRef<LiveToolInputPreview[]>([])
  const projection = createToolInputPreviewProjection()

  function accept(preview: ToolInputPreview, activityOrder: number, existingOnly = false): boolean {
    const changed = projection.accept(preview, activityOrder, existingOnly)
    if (changed) previews.value = projection.previews
    return changed
  }

  function handoff(toolUseId: string) {
    projection.handoff(toolUseId)
    previews.value = projection.previews
  }

  function clear() {
    projection.clear()
    previews.value = projection.previews
  }
  return { previews, accept, handoff, clear }
}
