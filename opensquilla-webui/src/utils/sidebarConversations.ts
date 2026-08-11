import type { SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

const RAW_SESSION_KEY_PATTERN = /\bagent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

export function looksLikeRawSessionId(value: string): boolean {
  // A human title such as "Cron: Daily report" is valid. Only scrub compact,
  // whitespace-free canonical keys from the sidebar.
  return RAW_SESSION_KEY_PATTERN.test(value) || UUID_PATTERN.test(value) || /^(?:agent|cron):[^\s]+$/i.test(value)
}

export function shouldShowAgentFilterBadge(
  family: SidebarSectionFamily,
  row: Pick<SidebarSectionRow, 'sessionKind' | 'depth'>,
): boolean {
  return family === 'chats' && row.sessionKind !== 'task' && row.depth === 0
}
