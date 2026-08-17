// Canonical list of Settings rail sections, kept in a standalone module so both
// the catalog composable and the route↔section mapping helpers can import it
// without forming an import cycle.

// `group` bins the canonical Settings destinations into labelled rail sections.
// Order matters for both navigation and the dirty-bar summary; AI configuration
// follows dependency order, while ordinary preferences precede safety and data.
export const SETTINGS_SECTIONS = [
  // Gateway is the recovery/operations entry point on every surface. Desktop
  // augments it with local runtime controls, but the rail destination is shared.
  { id: 'gateway', label: 'Gateway', icon: 'home', client: false, desktopOnly: false, group: null },
  // --- AI configuration: Model Service -> Model Routing ---
  { id: 'provider', label: 'Model Service', icon: 'agents', client: false, desktopOnly: false, group: 'ai' },
  { id: 'modelStrategy', label: 'Model Routing', icon: 'router', client: false, desktopOnly: false, group: 'ai' },
  { id: 'capabilities', label: 'Capabilities', icon: 'skills', client: false, desktopOnly: false, group: 'ai' },
  // --- Preferences: ordinary user-facing defaults and local UI settings ---
  { id: 'general', label: 'General', icon: 'settings', client: false, desktopOnly: false, group: 'preferences' },
  { id: 'interface', label: 'Interface', icon: 'monitor', client: true, desktopOnly: false, group: 'preferences' },
  { id: 'shortcuts', label: 'Shortcuts', icon: 'keyboard', client: true, desktopOnly: false, group: 'preferences' },
  // --- Safety and data: privacy, sandbox policy, and memory lifecycle ---
  { id: 'securityPrivacy', label: 'Security & Privacy', icon: 'shield', client: false, desktopOnly: false, group: 'safetyData' },
  { id: 'memory', label: 'Memory', icon: 'user', client: false, desktopOnly: false, group: 'safetyData' },
  // Advanced remains a quiet standalone destination at the bottom of the rail.
  { id: 'advanced', label: 'Advanced', icon: 'gauge', client: true, desktopOnly: false, group: null },
] as const

// Data maintenance is a nested Advanced destination rather than a first-level
// rail tab. Keep its stable route id here so existing deep links continue to
// resolve without making the destination prominent in Settings navigation.
export const NESTED_SETTINGS_SECTION_IDS = ['dataMigration', 'profileImport'] as const

export type SettingsRailSectionId = (typeof SETTINGS_SECTIONS)[number]['id']
export type NestedSettingsSectionId = (typeof NESTED_SETTINGS_SECTION_IDS)[number]
export type SettingsSectionId = SettingsRailSectionId | NestedSettingsSectionId
export type SettingsSectionGroup = Exclude<(typeof SETTINGS_SECTIONS)[number]['group'], null>
