import { ref } from 'vue'
import {
  NESTED_SETTINGS_SECTION_IDS,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from '@/composables/setup/settingsSections'

const DEFAULT_SECTION: SettingsSectionId = 'provider'
const SECTION_ALIASES: Record<string, SettingsSectionId> = {
  router: 'modelStrategy',
  ensemble: 'modelStrategy',
  chatModel: 'provider',
}

function sectionIdFor(value: unknown): SettingsSectionId | null {
  if (typeof value !== 'string') return null
  const canonical = SETTINGS_SECTIONS.find(s => s.id === value)
  if (canonical) return canonical.id
  const nested = NESTED_SETTINGS_SECTION_IDS.find(id => id === value)
  if (nested) return nested
  return SECTION_ALIASES[value] || null
}

export function sectionFromRouteParam(param: unknown): SettingsSectionId {
  return sectionIdFor(param) || DEFAULT_SECTION
}

export function isKnownSectionParam(param: unknown): boolean {
  return sectionIdFor(param) !== null
}

/**
 * Parse a `#provider-<id>` deep-link hash into the provider id it names.
 * Returns '' for anything else ('' hash, other anchors, bare '#provider-').
 */
export function parseProviderHash(hash: unknown): string {
  if (typeof hash !== 'string') return ''
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const prefix = 'provider-'
  if (!raw.startsWith(prefix)) return ''
  const id = raw.slice(prefix.length).trim()
  if (!id) return ''
  try {
    return decodeURIComponent(id)
  } catch {
    return id
  }
}

export function useSettingsSection(initialSection: string) {
  const section = ref(initialSection)

  function setSection(next: string) {
    if (!next || next === section.value) return
    section.value = next
  }

  return { section, setSection }
}
