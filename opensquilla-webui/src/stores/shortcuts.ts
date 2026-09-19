import { ref } from 'vue'
import { defineStore } from 'pinia'
import i18n from '@/i18n'
import { bindingsEqual, type Binding } from '@/utils/keychord'

// Global chord shortcuts the operator can enable / disable / rebind from the
// Keyboard settings section. Only app-level "go anywhere" chords live here;
// structural keys (Escape) and contextual editor keys (composer Enter/history)
// are deliberately excluded — they are not user-configurable.
//
// Persistence mirrors the theme preference: a browser-local localStorage value,
// applied instantly, never part of the settings dirty bar.

export type ShortcutId =
  | 'command-palette'
  | 'new-chat'
  | 'toggle-sidebar'
  | 'toggle-workbench'

export interface ShortcutDef {
  id: ShortcutId
  label: string
  description: string
  defaultBinding: Binding
  /** Default enabled state for a fresh browser. */
  defaultEnabled: boolean
}

export interface ShortcutState {
  binding: Binding | null
  enabled: boolean
}

// `primary` = Cmd on macOS / Ctrl elsewhere (see utils/keychord).
// `label` / `description` are getters that resolve the current locale on each
// read, so consumers (the Keyboard settings panel) re-render on a language
// switch without the def list itself becoming reactive state.
export const SHORTCUT_DEFS: readonly ShortcutDef[] = [
  {
    id: 'toggle-sidebar',
    get label() { return i18n.global.t('shared.shortcuts.toggleSidebar.label') },
    get description() { return i18n.global.t('shared.shortcuts.toggleSidebar.description') },
    defaultBinding: { primary: true, key: 'b' },
    defaultEnabled: true,
  },
  {
    id: 'toggle-workbench',
    get label() { return i18n.global.t('shared.shortcuts.toggleWorkbench.label') },
    get description() { return i18n.global.t('shared.shortcuts.toggleWorkbench.description') },
    defaultBinding: { primary: true, key: 'j' },
    defaultEnabled: true,
  },
  {
    id: 'command-palette',
    get label() { return i18n.global.t('shared.shortcuts.commandPalette.label') },
    get description() { return i18n.global.t('shared.shortcuts.commandPalette.description') },
    defaultBinding: { primary: true, key: 'k' },
    defaultEnabled: true,
  },
  {
    id: 'new-chat',
    get label() { return i18n.global.t('shared.shortcuts.newChat.label') },
    get description() { return i18n.global.t('shared.shortcuts.newChat.description') },
    defaultBinding: { primary: true, shift: true, key: 'k' },
    // Off out of the box: the chord previously fired unconditionally; operators
    // opt back in here rather than having it bound for them.
    defaultEnabled: false,
  },
]

const STORAGE_KEY = 'opensquilla.shortcuts'

function defaultState(): Record<ShortcutId, ShortcutState> {
  const out = {} as Record<ShortcutId, ShortcutState>
  for (const def of SHORTCUT_DEFS) {
    out[def.id] = { binding: { ...def.defaultBinding }, enabled: def.defaultEnabled }
  }
  return out
}

function isBinding(value: unknown): value is Binding {
  return !!value && typeof value === 'object' && typeof (value as Binding).key === 'string'
}

// Hydrate over the defaults so a partial / older persisted blob keeps working
// and newly-added shortcuts pick up their defaults.
function hydrate(): Record<ShortcutId, ShortcutState> {
  const state = defaultState()
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return state
    const parsed = JSON.parse(raw) as Partial<Record<ShortcutId, Partial<ShortcutState>>>
    for (const def of SHORTCUT_DEFS) {
      const saved = parsed?.[def.id]
      if (!saved) continue
      if (typeof saved.enabled === 'boolean') state[def.id].enabled = saved.enabled
      if (saved.binding === null) state[def.id].binding = null
      else if (isBinding(saved.binding)) state[def.id].binding = { ...saved.binding }
    }
  } catch {
    // Corrupt or unavailable storage falls back to defaults.
  }
  return state
}

export const useShortcutsStore = defineStore('shortcuts', () => {
  const states = ref<Record<ShortcutId, ShortcutState>>(hydrate())

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(states.value))
    } catch {
      // ignore (private mode / quota)
    }
  }

  /** The binding to honour at runtime: null when the shortcut is disabled. */
  function effectiveBinding(id: ShortcutId): Binding | null {
    const s = states.value[id]
    return s && s.enabled ? s.binding : null
  }

  function setEnabled(id: ShortcutId, enabled: boolean) {
    states.value[id].enabled = enabled
    persist()
  }

  function setBinding(id: ShortcutId, binding: Binding | null) {
    states.value[id].binding = binding ? { ...binding } : null
    persist()
  }

  function resetBinding(id: ShortcutId) {
    const def = SHORTCUT_DEFS.find(d => d.id === id)
    if (!def) return
    states.value[id].binding = { ...def.defaultBinding }
    persist()
  }

  /**
   * The id of an enabled shortcut (other than `exceptId`) whose effective
   * binding equals `binding`, or null if none — used by the rebind UI to reject
   * a chord already taken by another shortcut.
   */
  function findConflict(binding: Binding, exceptId: ShortcutId): ShortcutId | null {
    for (const def of SHORTCUT_DEFS) {
      if (def.id === exceptId) continue
      if (bindingsEqual(effectiveBinding(def.id), binding)) return def.id
    }
    return null
  }

  return { states, effectiveBinding, setEnabled, setBinding, resetBinding, findConflict }
})
