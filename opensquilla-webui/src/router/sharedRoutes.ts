import type { RouteRecordRaw } from 'vue-router'
import { detectPlatformId } from '@/platform/capabilities'
import { readLastRoute } from './lastRoute'

const ChatView = () => import('@/views/ChatView.vue')
const CronView = () => import('@/views/CronView.vue')
const UsageView = () => import('@/views/UsageView.vue')
const SkillsChannelsHubView = () => import('@/views/SkillsChannelsHubView.vue')

export function defaultRootRedirect(): string {
  if (detectPlatformId() === 'desktop') return '/chat'
  const saved = readLastRoute()
  if (saved) return saved
  // Chat on every form factor: Sessions is no longer a navigation destination,
  // so landing there would open on a page the sidebar cannot get back to.
  return '/chat'
}

export const sharedRoutes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: () => {
      // Desktop app cold starts should feel like opening an assistant: land on
      // Chat, not the session ledger. Browser builds still restore the last
      // stable view when available, with the existing responsive fallback.
      return defaultRootRedirect()
    },
  },
  { path: '/chat',      name: 'chat',      component: ChatView,      meta: { title: 'Chat', group: 'Work', icon: 'chat', nav: 'primary', navOrder: 10, platforms: ['web', 'desktop'], viewKey: 'chat' } },
  // Draft state: a clean composer with no session key until the first send.
  { path: '/chat/new',  name: 'chat-new',  component: ChatView,      meta: { title: 'Chat', group: 'Work', icon: 'chat', platforms: ['web', 'desktop'], viewKey: 'chat' } },
  // Legacy deep link: sessions are managed from the sidebar and chat.
  { path: '/sessions', redirect: '/chat' },
  { path: '/usage',     name: 'usage',     component: UsageView, meta: { title: 'Usage', group: 'Work', icon: 'usage', nav: 'primary', navOrder: 60, navLabelKey: 'nav.viewUsage', platforms: ['web', 'desktop'], keepAlive: true } },
  // Preserve old bookmarks without loading the retired diagnostic pages.
  { path: '/overview', redirect: '/usage' },
  // Keep link-token/query parameters while replacing the retired page's hash.
  { path: '/logs', redirect: { path: '/settings/gateway', hash: '#logs' } },
  // Approvals resolve inline in the chat transcript and via the topbar pill.
  { path: '/approvals', redirect: '/chat' },
  // Skills and Channels form one primary destination. /skills remains the
  // rail target while both canonical routes share the same kept-alive hub.
  { path: '/skills',    name: 'skills',    component: SkillsChannelsHubView, meta: { title: 'Skills', group: 'Work', icon: 'skills', nav: 'primary', navOrder: 40, navLabelKey: 'nav.skillsChannels', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'skills-channels-hub' } },
  { path: '/channels',  name: 'channels',  component: SkillsChannelsHubView, meta: { title: 'Channels', icon: 'channels', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'skills-channels-hub' } },
  { path: '/cron',      name: 'cron',      component: CronView,      meta: { title: 'Cron', group: 'Work', icon: 'cron', nav: 'primary', navOrder: 50, platforms: ['web', 'desktop'], keepAlive: true } },
  { path: '/health', redirect: '/usage' },
]
