import type { ThemeManifest } from './types'
import type { IconName } from '@/utils/icons'

// Drop-a-folder registration: every `src/themes/<id>/manifest.ts` is picked up
// automatically — adding a theme is a new folder, never an edit here. A
// manifest.ts must hold metadata + lazy thunks only (no heavy static imports),
// so eager-globbing the manifests never pulls fonts/CSS into the entry chunk.
const modules = import.meta.glob<{ default: ThemeManifest }>('./*/manifest.ts', {
  eager: true,
})

export const THEMES: Record<string, ThemeManifest> = Object.fromEntries(
  Object.values(modules).map((m) => [m.default.id, m.default]),
)

const all = Object.values(THEMES)

// Picker order: light, dark, then custom themes (alphabetical), with `system`
// appended by themePickerOptions().
const THEME_RANK: Record<string, number> = { light: 0, dark: 1 }

/** Value themes offered in the global appearance switcher (stable order). */
export const selectableValueThemes: ThemeManifest[] = all
  .filter((m) => m.kind === 'value' && m.capabilities.userSelectable)
  .sort(
    (a, b) =>
      (THEME_RANK[a.id] ?? 2) - (THEME_RANK[b.id] ?? 2) || a.id.localeCompare(b.id),
  )

export const getManifest = (id: string): ThemeManifest | undefined => THEMES[id]

/** Is `id` a registered value theme? (used to validate a persisted choice) */
export const isValueThemeId = (id: string): boolean =>
  THEMES[id]?.kind === 'value'

// Renamed theme ids → their current canonical id. A persisted choice written
// under an old id must keep working after a rename, so this map is applied
// wherever the stored value is read/validated (before isValueThemeId) — and the
// normalized id is written back on the next persist. Extend this, never rename a
// live id without an entry here.
export const LEGACY_THEME_IDS: Record<string, string> = {
  nord: 'arctic',
  phosphor: 'crt-green',
}

/** Map a possibly-legacy persisted theme id to its current canonical id; passes
 *  through 'system' and any id that is not a known legacy alias. */
export const normalizeThemeId = (id: string): string => LEGACY_THEME_IDS[id] ?? id

// Built-in modes whose labels live under the translated `chrome.themeMode.*`
// keys; every other (custom) theme shows its manifest `name` as a proper-noun
// literal, so a new theme needs no per-locale key.
const BUILTIN_MODES = new Set(['light', 'dark', 'system'])

export interface ThemePickerOption {
  /** 'system' or a value-theme id — the value written to the app store. */
  mode: string
  /** Icon-set name for the option. */
  icon: IconName
  /** i18n key for built-in modes (light/dark/system). */
  labelKey?: string
  /** Literal display name for custom themes (proper noun, untranslated). */
  label?: string
}

export interface ThemePickerOptionsArgs {
  /** 'basic' → only the built-in modes (Light / Dark) + System, for the compact
   *  topbar menu. 'all' → every selectable value theme + System, for Settings →
   *  Appearance. Defaults to 'all'. */
  scope?: 'basic' | 'all'
}

/** Options for an appearance picker: selectable value themes + `system`, in a
 *  stable order, filtered by `scope`.
 *
 *  The topbar menu and Settings → Appearance DELIBERATELY differ: the topbar
 *  requests `scope: 'basic'` (Light / Dark / System only — plus a "More themes…"
 *  action wired in App.vue that opens Settings → Appearance), while Settings
 *  requests `scope: 'all'` and lists every registered value theme. Both derive
 *  from this one builder, so the rows they share can never drift and a new theme
 *  folder still appears in the full list automatically. */
export function themePickerOptions(
  { scope = 'all' }: ThemePickerOptionsArgs = {},
): ThemePickerOption[] {
  const source =
    scope === 'basic'
      ? selectableValueThemes.filter((m) => BUILTIN_MODES.has(m.id))
      : selectableValueThemes
  const values: ThemePickerOption[] = source.map((m) => ({
    mode: m.id,
    icon: m.icon ?? (m.capabilities.colorScheme === 'light' ? 'sun' : 'moon'),
    labelKey: BUILTIN_MODES.has(m.id) ? `chrome.themeMode.${m.id}` : undefined,
    label: BUILTIN_MODES.has(m.id) ? undefined : m.name,
  }))
  return [...values, { mode: 'system', icon: 'monitor', labelKey: 'chrome.themeMode.system' }]
}
