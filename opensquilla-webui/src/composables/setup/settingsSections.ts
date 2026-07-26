// Canonical list of Settings rail sections, kept in a standalone module so both
// the catalog composable and the route↔section mapping helpers can import it
// without forming an import cycle.

// `group` bins the flat rail into labelled sections in the dialog. It is purely
// presentational: section ids, routes, panels, readiness, and save behaviour are
// unchanged — the rail is only reordered and captioned. Order matters because the
// rail (and the dirty-bar summary) reads top-to-bottom; it now mirrors the setup
// dependency order (Model Service → Model Routing → Capabilities) so the coupled
// panels sit adjacent instead of being split by Behavior/Privacy.
export const SETTINGS_SECTIONS = [
  // --- Gateway: host/connection state, renders before config loads ---
  // Connection carries a live status dot (driven by the gateway socket state,
  // not readiness RPC) so it works before any config loads. It applies on
  // Connect and never enters the dirty bar, so it is excluded from save/discard.
  { id: 'connection', label: 'Connection', icon: 'home', client: false, desktopOnly: false, group: 'gateway' },
  // Runtime is desktop-only: the owned local gateway's status, log, restart, and
  // update controls. It is client-like (no readiness/RPC state, never dirty) and hidden on
  // web, where the host does not own a gateway process.
  { id: 'runtime', label: 'Runtime', icon: 'monitor', client: true, desktopOnly: true, group: 'gateway' },
  // --- AI configuration: Model Service -> Model Routing ---
  { id: 'provider', label: 'Model Service', icon: 'agents', client: false, desktopOnly: false, group: 'ai' },
  { id: 'modelStrategy', label: 'Model Routing', icon: 'router', client: false, desktopOnly: false, group: 'ai' },
  { id: 'capabilities', label: 'Capabilities', icon: 'skills', client: false, desktopOnly: false, group: 'capabilities' },
  // --- Preferences: assistant behaviour + local app settings ---
  { id: 'behavior', label: 'Behavior', icon: 'chat', client: false, desktopOnly: false, group: 'preferences' },
  { id: 'privacy', label: 'Privacy', icon: 'shield', client: false, desktopOnly: false, group: 'preferences' },
  // Client-only sections carry no readiness/RPC state: they edit local browser
  // preferences that apply instantly and never enter the dirty bar. The status
  // dot is suppressed for them in the rail.
  { id: 'appearance', label: 'Appearance', icon: 'monitor', client: true, desktopOnly: false, group: 'preferences' },
  { id: 'keyboard', label: 'Keyboard', icon: 'keyboard', client: true, desktopOnly: false, group: 'preferences' },
  { id: 'advanced', label: 'Advanced', icon: 'gauge', client: true, desktopOnly: false, group: 'preferences' },
] as const

// Data maintenance is a nested Advanced destination rather than a first-level
// rail tab. Keep its stable route id here so existing deep links continue to
// resolve without making the destination prominent in Settings navigation.
export const NESTED_SETTINGS_SECTION_IDS = ['dataMigration'] as const

export type SettingsRailSectionId = (typeof SETTINGS_SECTIONS)[number]['id']
export type NestedSettingsSectionId = (typeof NESTED_SETTINGS_SECTION_IDS)[number]
export type SettingsSectionId = SettingsRailSectionId | NestedSettingsSectionId
export type SettingsSectionGroup = (typeof SETTINGS_SECTIONS)[number]['group']
