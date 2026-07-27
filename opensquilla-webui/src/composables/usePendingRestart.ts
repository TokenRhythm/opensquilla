import { computed, ref } from 'vue'

// Channel mutations (upsert / enable / disable / remove) only take effect
// after a gateway restart — the RPCs say so via `restartRequired: true`, but
// that truth used to live in a 5-second toast. This store makes it persistent
// and shared: both channel surfaces render the same pending set.
//
// Clearing is (a) observed-status reconciliation — a later channels.status
// snapshot proves the restart happened — or (b) manual per-entry dismissal.
// Known accepted limit: the set is per-browser (localStorage); a second
// operator's console self-heals via (a) once the restart actually lands.

export type PendingRestartAction = 'upsert' | 'enable' | 'disable' | 'remove'

export interface PendingRestartEntry {
  channel: string
  action: PendingRestartAction
  at: number
  /**
   * Whether the adapter was already loaded when the change was saved. An
   * upsert over a live adapter has no observable post-restart signal yet
   * (no boot id on channels.status), so it clears only manually.
   */
  wasLoaded?: boolean
}

interface StatusRow {
  name?: string | null
  status?: string | null
  enabled?: boolean | null
  capability_profile?: unknown
}

const STORAGE_KEY = 'opensquilla.channels.pendingRestart.v1'

function loadStored(): PendingRestartEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (entry): entry is PendingRestartEntry =>
        !!entry && typeof entry === 'object' && typeof entry.channel === 'string',
    )
  } catch {
    return []
  }
}

// Module-level singleton so every consumer (Settings dialog, /channels view)
// observes the same set within a session.
const entries = ref<PendingRestartEntry[]>(loadStored())

function persist(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries.value))
  } catch {
    // Storage may be unavailable (private mode); the in-memory set still works.
  }
}

function rowLoaded(row: StatusRow): boolean {
  if (row.capability_profile != null) return true
  const status = String(row.status ?? '')
  return status === 'connected' || status === 'restarting' || status === 'dead' || status === 'exhausted'
}

function cleared(entry: PendingRestartEntry, row: StatusRow | undefined): boolean {
  switch (entry.action) {
    case 'remove':
      // The config entry disappears immediately; the adapter lingers as a
      // configured:false runtime row until the restart unloads it.
      return row === undefined
    case 'disable':
      return row === undefined || (!rowLoaded(row) && row.enabled === false)
    case 'enable':
      return row !== undefined && rowLoaded(row)
    case 'upsert':
      // No observable signal for a config swap under a live adapter.
      return entry.wasLoaded ? false : row !== undefined && rowLoaded(row)
  }
}

export function usePendingRestart() {
  function record(channel: string, action: PendingRestartAction, opts?: { wasLoaded?: boolean }): void {
    const name = channel.trim()
    if (!name) return
    entries.value = [
      ...entries.value.filter(entry => entry.channel !== name),
      { channel: name, action, at: Date.now(), wasLoaded: opts?.wasLoaded },
    ]
    persist()
  }

  function dismiss(channel: string): void {
    entries.value = entries.value.filter(entry => entry.channel !== channel)
    persist()
  }

  /**
   * Reconcile against a fresh channels.status snapshot. Pass the RAW rows,
   * including configured:false runtime-only entries — filtering first would
   * clear `remove` while the adapter is still delivering.
   */
  function reconcile(rows: StatusRow[]): void {
    if (entries.value.length === 0) return
    const byName = new Map(rows.map(row => [String(row.name ?? ''), row]))
    const keep = entries.value.filter(entry => !cleared(entry, byName.get(entry.channel)))
    if (keep.length !== entries.value.length) {
      entries.value = keep
      persist()
    }
  }

  function isPending(channel: string): boolean {
    return entries.value.some(entry => entry.channel === channel)
  }

  const pending = computed(() => entries.value)
  const count = computed(() => entries.value.length)

  return { record, dismiss, reconcile, isPending, pending, count }
}

/** Test hook: reset the module-level store between cases. */
export function _resetPendingRestartForTests(): void {
  entries.value = []
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}
