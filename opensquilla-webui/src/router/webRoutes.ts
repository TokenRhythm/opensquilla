import type { RouteRecordRaw } from 'vue-router'

const SettingsView = () => import('@/views/web/SettingsView.vue')

// Settings is a deep-linkable overlay route that renders the settings dialog
// over the default view. `/settings/:section` selects a rail section; the legacy
// `/config` and `/setup` deep links redirect into it (`/setup` lands on the
// first not-ready section via the `auto` sentinel param).
//
// Registered behind capabilities.hasWebConfig, which is now true on BOTH
// platforms — the desktop's local gateway serves the same Control UI RPC, so it
// shares this dialog (plus a desktop-only Runtime section). desktopRoutes no
// longer defines `/settings`, so there is no collision.
export const webRoutes: RouteRecordRaw[] = [
  { path: '/settings', name: 'settings', component: SettingsView, meta: { title: 'Settings', titleKey: 'nav.settings', icon: 'settings', platforms: ['web', 'desktop'], viewKey: 'settings', routeTransition: 'none' } },
  { path: '/settings/:section', name: 'settings-section', component: SettingsView, meta: { title: 'Settings', titleKey: 'nav.settings', icon: 'settings', platforms: ['web', 'desktop'], viewKey: 'settings', routeTransition: 'none' } },
  { path: '/config', redirect: '/settings' },
  { path: '/setup',  redirect: '/settings/auto' },
]
