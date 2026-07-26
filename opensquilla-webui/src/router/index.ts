import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw, RouteLocationNormalized } from 'vue-router'
import { getPlatform } from '@/platform'
import i18n from '@/i18n'
import { desktopRoutes } from './desktopRoutes'
import { sharedRoutes } from './sharedRoutes'
import { webRoutes } from './webRoutes'
import { captureContentScroll, contentScrollBehavior } from './scrollMemory'
import { saveLastRoute } from './lastRoute'
import { legacyChannelHashRedirect } from './legacyRedirects'

const basePath = (() => {
  const el = document.getElementById('opensquilla-data')
  const raw = el?.dataset.basePath || '/control'
  return raw.endsWith('/') ? raw : raw + '/'
})()

const platform = getPlatform()
const NotFoundView = () => import('@/views/NotFoundView.vue')

export const routes: RouteRecordRaw[] = [
  ...sharedRoutes,
  ...(platform.capabilities.hasWebConfig ? webRoutes : []),
  ...(platform.capabilities.hasDesktopOnboarding ? desktopRoutes : []),
  { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView, meta: { title: 'Not Found', platforms: ['web', 'desktop'] } },
]

export const router = createRouter({
  history: createWebHistory(basePath),
  routes,
  scrollBehavior: contentScrollBehavior,
})

// Capture the leaving route's content scroll offset so back/forward can restore it.
router.beforeEach((_to, from) => {
  captureContentScroll(from)
})

// Stale channel-setup bookmarks (#channel-… hashes) land on the workspace.
router.beforeEach((to) => legacyChannelHashRedirect(to) ?? true)

// Localize the document title from the route name token (e.g. `nav.sessions`),
// falling back to the English meta.title. `applyRouteTitle` is also re-run when
// the locale changes (App.vue watches the store) since afterEach does not
// re-fire without a navigation.
export function routeTitle(route: RouteLocationNormalized): string {
  const explicitKey = route.meta?.titleKey
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
  return (route.meta?.title as string) || 'OpenSquilla'
}

router.afterEach((to) => {
  document.title = `${routeTitle(to)} — OpenSquilla`
  // Remember the current view (path only) so the next launch reopens here.
  saveLastRoute(to.path)
})
