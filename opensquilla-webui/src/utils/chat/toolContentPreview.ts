/** Rendering budgets are independent of the retained historical body. */
export const TOOL_CONTENT_PREVIEW_MAX_BYTES = 256 * 1024
export const TOOL_CONTENT_PREVIEW_MAX_LINES = 1000
export const TOOL_CONTENT_PREVIEW_MAX_LINE_BYTES = 8 * 1024

export interface ToolContentPreview { content: string; truncated: boolean }

interface PreviewBudget { bytes: number; lines: number }

/** Inspect only the bounded prefix; never split a surrogate pair or CRLF. */
export function boundedTextPreview(content: string): ToolContentPreview {
  return scanPreview(content, { bytes: 0, lines: 0 })
}

/** Multiple sections share one card budget without joining their full bodies. */
export function boundedTextPreviews(contents: readonly string[]): { previews: ToolContentPreview[]; truncated: boolean } {
  const budget = { bytes: 0, lines: 0 }
  let truncated = false
  const previews = contents.map(content => {
    const preview = truncated ? { content: '', truncated: content.length > 0 } : scanPreview(content, budget)
    truncated ||= preview.truncated
    return preview
  })
  return { previews, truncated }
}

function scanPreview(content: string, budget: PreviewBudget): ToolContentPreview {
  let offset = 0
  let lineBytes = 0
  while (offset < content.length && budget.lines < TOOL_CONTENT_PREVIEW_MAX_LINES) {
    const point = content.codePointAt(offset)!
    const crlf = point === 13 && content.charCodeAt(offset + 1) === 10
    const newline = point === 10 || point === 13
    const width = crlf || point > 0xffff ? 2 : 1
    const size = crlf ? 2 : point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4
    if (budget.bytes + size > TOOL_CONTENT_PREVIEW_MAX_BYTES
      || lineBytes + size > TOOL_CONTENT_PREVIEW_MAX_LINE_BYTES) break
    budget.bytes += size
    offset += width
    if (newline) { budget.lines += 1; lineBytes = 0 } else lineBytes += size
  }
  if (lineBytes > 0) budget.lines += 1
  return { content: content.slice(0, offset), truncated: offset < content.length }
}
