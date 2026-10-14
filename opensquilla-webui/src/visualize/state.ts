// State belongs to a stable widget and calling session, not an immutable code
// revision. This lets edit_widget refresh the guest while retaining controls;
// bounded sessionStorage preserves refresh without sharing state across accounts.
const memory = new Map<string, unknown>()
const PREFIX = 'opensquilla.visualize.v1.'
const LIMIT = 48
const MAX_STATE = 16 * 1024
export function readVisualizeState(key: string): unknown {
  if (memory.has(key)) return memory.get(key)
  try { const raw = sessionStorage.getItem(PREFIX + key); return raw && raw.length <= MAX_STATE ? JSON.parse(raw) : undefined } catch { return undefined }
}
export function saveVisualizeState(key: string, state: unknown): void {
  try {
    const encoded = JSON.stringify(state)
    if (!encoded || encoded.length > MAX_STATE) return
    memory.delete(key); memory.set(key, JSON.parse(encoded))
    while (memory.size > LIMIT) memory.delete(memory.keys().next().value!)
    sessionStorage.setItem(PREFIX + key, encoded)
    const keys = Object.keys(sessionStorage).filter(value => value.startsWith(PREFIX))
    for (const old of keys.slice(0, Math.max(0, keys.length - LIMIT))) if (old !== PREFIX + key) sessionStorage.removeItem(old)
  } catch { /* Storage may be unavailable in a remote browser; memory remains bounded. */ }
}
