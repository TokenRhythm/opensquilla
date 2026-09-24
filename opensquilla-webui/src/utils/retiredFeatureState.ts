const RETIRED_KEYS = new Set([
  'opensquilla.chat.metaDraftOutbox:v1',
  'opensquilla.chat.metaDiscardOutbox:v1',
  'opensquilla.chat.hiddenControlOutbox:v1',
])
const RETIRED_PREFIXES = [
  'opensquilla.chat.metaSetupJob:',
  'opensquilla.chat.metaSetupLaunch:',
  'opensquilla.chat.metaSetupManual:',
]

/** Drop retired workflow recovery inputs; never replay them as ordinary chat. */
export function clearRetiredFeatureState(storage: Pick<Storage, 'length' | 'key' | 'removeItem'>): void {
  try {
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key && (RETIRED_KEYS.has(key) || RETIRED_PREFIXES.some(prefix => key.startsWith(prefix)))) {
        keys.push(key)
      }
    }
    for (const key of keys) storage.removeItem(key)
  } catch {
    // Restricted browser storage must not prevent the client from starting.
  }
}

export function clearRetiredBrowserFeatureState(browser: Pick<Window, 'localStorage' | 'sessionStorage'>): void {
  for (const storageName of ['localStorage', 'sessionStorage'] as const) {
    try {
      clearRetiredFeatureState(browser[storageName])
    } catch {
      // Access to either storage can be blocked independently.
    }
  }
}

/** Reserved IDs used only by the removed workflow control sender. */
export function isRetiredControlMessage(clientMessageId: unknown): boolean {
  return typeof clientMessageId === 'string' && clientMessageId.startsWith('hidden-control:')
}
