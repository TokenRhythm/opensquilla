import { ref } from 'vue'
import {
  NESTED_SETTINGS_SECTION_IDS,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from '@/composables/setup/settingsSections'

const DEFAULT_SECTION: SettingsSectionId = 'provider'
export interface SettingsSectionAlias {
  section: SettingsSectionId
  hash?: string
}

const SECTION_ALIASES: Record<string, SettingsSectionAlias> = {
  connection: { section: 'gateway', hash: '#connection' },
  runtime: { section: 'gateway', hash: '#runtime' },
  behavior: { section: 'general' },
  appearance: { section: 'interface' },
  keyboard: { section: 'shortcuts' },
  privacy: { section: 'securityPrivacy', hash: '#privacy' },
  sandbox: { section: 'securityPrivacy', hash: '#sandbox' },
  router: { section: 'modelStrategy' },
  ensemble: { section: 'modelStrategy' },
  chatModel: { section: 'provider' },
  // Profile import used to be a child route below the Memory overview. Keep
  // old bookmarks working while canonicalizing them to the first-level
  // Memory & Export destination.
  profileImport: { section: 'memory' },
}

export function settingsSectionAliasFor(value: unknown): SettingsSectionAlias | null {
  if (typeof value !== 'string') return null
  return SECTION_ALIASES[value] || null
}

function sectionIdFor(value: unknown): SettingsSectionId | null {
  if (typeof value !== 'string') return null
  const canonical = SETTINGS_SECTIONS.find(s => s.id === value)
  if (canonical) return canonical.id
  const nested = NESTED_SETTINGS_SECTION_IDS.find(id => id === value)
  if (nested) return nested
  return settingsSectionAliasFor(value)?.section || null
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
