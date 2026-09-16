const LEGACY_DOCUMENT_TOOLS = new Set([
  'document_inspect',
  'document_read',
  'document_locate',
  'document_apply',
  'document_patch',
  'document_browser_inspect',
  'document_browser_act',
  'document_browser_screenshot',
  'document_browser_reload',
  'document_finish',
])

/** Read-only recognition for safely displaying persisted, retired tool results. */
export function isLegacyDocumentTool(name: string | undefined): boolean {
  const normalized = String(name || '').trim().toLowerCase()
  if (normalized === 'document.read' || normalized === 'document.update') return true
  const basename = normalized.split(/\.|\/|:|__/).pop() || ''
  return LEGACY_DOCUMENT_TOOLS.has(basename)
}
