/** Keep a bounded tail without cutting a UTF-16 surrogate pair in half. */
export function boundedToolInputPreviewContent(value: string): { content: string; truncated: boolean } {
  let start = Math.max(0, value.length - 65_536)
  if (start > 0 && /[\uDC00-\uDFFF]/.test(value[start] || '')
    && /[\uD800-\uDBFF]/.test(value[start - 1] || '')) start += 1
  return { content: value.slice(start), truncated: start > 0 }
}
