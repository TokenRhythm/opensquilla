import type { LiveToolInputPreview, ToolInputPreview } from '@/types/toolInputPreview'

/** Keep a bounded tail without cutting a UTF-16 surrogate pair in half. */
export function boundedToolInputPreviewContent(value: string): { content: string; truncated: boolean } {
  let start = Math.max(0, value.length - 65_536)
  if (start > 0 && /[\uDC00-\uDFFF]/.test(value[start] || '')
    && /[\uD800-\uDBFF]/.test(value[start - 1] || '')) start += 1
  return { content: value.slice(start), truncated: start > 0 }
}

/** Shared ephemeral projection. Drafts are never tool executions or durable history. */
export function createToolInputPreviewProjection() {
  let previews: LiveToolInputPreview[] = []
  const accepted = new Map<string, ToolInputPreview>()
  function accept(preview: ToolInputPreview, activityOrder: number, existingOnly = false): boolean {
    if (!Number.isSafeInteger(activityOrder) || activityOrder <= 0) return false
    const previous = accepted.get(preview.previewId)
    if (existingOnly && !previous) return false
    if (previous && (previous.attemptId !== preview.attemptId
      || previous.generationEpoch !== preview.generationEpoch || previous.toolUseId !== preview.toolUseId
      || previous.toolName !== preview.toolName
      || previous.revision >= preview.revision || previous.state !== 'generating')) return false
    if (!previous && accepted.size >= 64) return false
    const bounded = boundedToolInputPreviewContent(preview.content)
    preview = { ...preview, content: bounded.content, truncated: preview.truncated || bounded.truncated }
    accepted.set(preview.previewId, preview)
    const current = previews.find(item => item.previewId === preview.previewId)
    if (preview.state === 'discarded') {
      previews = previews.filter(item => item.previewId !== preview.previewId)
    } else if (preview.state === 'committed') {
      // Keep existing text until tool-start takes over. Terminal-only replay
      // may retire an existing draft, but may never create one.
      if (current) previews = previews.map(item => item.previewId === preview.previewId
        ? { ...item, revision: preview.revision, state: preview.state } : item)
    } else {
      const next = { ...preview, activityOrder: current?.activityOrder ?? activityOrder }
      previews = current ? previews.map(item => item.previewId === preview.previewId ? next : item) : [...previews, next]
    }
    return true
  }
  function handoff(toolUseId: string) {
    for (const preview of previews) {
      if (preview.toolUseId === toolUseId) accepted.set(preview.previewId, { ...preview, state: 'committed' })
    }
    previews = previews.filter(preview => preview.toolUseId !== toolUseId)
  }
  function clear() { previews = []; accepted.clear() }
  return { get previews() { return previews }, accept, handoff, clear }
}
