// Shared URL contract for assistant-authored Markdown and generated diagrams.
// Local/private resources must use their session-scoped resource adapters.
const CONTROL_CHAR_RE = /[\u0000-\u0020\u007f]/

export function isSafeMarkdownImageSource(value: string): boolean {
  const source = value.trim()
  if (CONTROL_CHAR_RE.test(source)) return false
  return /^https:\/\//i.test(source)
    || /^data:image\/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/.test(source)
}

export function isSafeMarkdownLinkSource(value: string): boolean {
  const href = value.trim()
  if (CONTROL_CHAR_RE.test(href)) return false
  return /^https?:\/\//i.test(href) || /^mailto:/i.test(href)
}

export function isInternalSvgReference(value: string): boolean {
  return /^#[A-Za-z_][\w:.-]*$/.test(value)
}
