import { getPlatform } from '@/platform'
import type { PlatformId } from '@/platform'
import type { RouteRecordRaw } from 'vue-router'
import type { IconName } from '@/utils/icons'
import i18n from '@/i18n'
import { desktopRoutes } from './desktopRoutes'
import { sharedRoutes } from './sharedRoutes'
import { webRoutes } from './webRoutes'

type NavigationSlot = 'primary' | 'bottom'

export interface NavigationItem {
  path: string
  title: string
  icon: IconName
}

const navRoutes = [
  ...sharedRoutes,
  ...webRoutes,
  ...desktopRoutes,
]

function routePlatforms(platforms: unknown): PlatformId[] {
  if (!Array.isArray(platforms)) return ['web', 'desktop']
  return platforms.filter((item): item is PlatformId => item === 'web' || item === 'desktop')
}

// Localize a nav row title from its route name token (e.g. `nav.sessions`),
// falling back to the English meta.title literal when no key exists. Called
// inside the useNavigation() computeds, so reading the reactive i18n locale here
// makes the rail/drawer/palette re-render on a language switch.
function navTitle(route: RouteRecordRaw): string {
  const explicitKey = route.meta?.navLabelKey
  if (explicitKey) {
    const translated = i18n.global.t(explicitKey)
    if (translated !== explicitKey) return translated
  }
  const name = typeof route.name === 'string' ? route.name : ''
  if (name) {
    const key = `nav.${name}`
    const translated = i18n.global.t(key)
    if (translated !== key) return translated
  }
  return String(route.meta?.title || route.name || route.path)
}

export function getNavigationItems(slot: NavigationSlot): NavigationItem[] {
  const platform = getPlatform()
  return navRoutes
    .filter((route) => route.meta?.nav === slot)
    .filter((route) => routePlatforms(route.meta?.platforms).includes(platform.id))
    .sort((a, b) => Number(a.meta?.navOrder || 0) - Number(b.meta?.navOrder || 0))
    .map((route) => ({
      path: route.path,
      title: navTitle(route),
      icon: (route.meta?.icon || 'home') as IconName,
    }))
}

// The flat, always-visible destinations shared by the desktop rail, mobile
// drawer, and command palette. Chat is excluded because the dedicated New-chat
// action owns that destination.
export function getWorkNavigationSection(): NavigationItem[] {
  const groupOf = new Map(
    navRoutes
      .filter((route) => route.meta?.nav === 'primary')
      .map((route) => [route.path, route.meta?.group ?? 'Operate']),
  )
  return getNavigationItems('primary').filter(
    (item) => groupOf.get(item.path) === 'Work' && item.path !== '/chat',
  )
}
