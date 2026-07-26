import type { RouteRecordRaw } from 'vue-router'
import { detectPlatformId } from '@/platform/capabilities'
import { readLastRoute } from './lastRoute'

const ChatView = () => import('@/views/ChatView.vue')
const CronView = () => import('@/views/CronView.vue')
const AgentsView = () => import('@/views/AgentsView.vue')
const SessionsView = () => import('@/views/SessionsView.vue')
const ChangelogView = () => import('@/views/ChangelogView.vue')
const OverviewHubView = () => import('@/views/OverviewHubView.vue')
const LogsView = () => import('@/views/LogsView.vue')
const SkillsChannelsHubView = () => import('@/views/SkillsChannelsHubView.vue')

export function defaultRootRedirect(): string {
  if (detectPlatformId() === 'desktop') return '/chat'
  const saved = readLastRoute()
  if (saved) return saved
  const isMobile = window.matchMedia('(max-width: 768px)').matches
  return isMobile ? '/chat' : '/sessions'
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
  { path: '/chat',      name: 'chat',      component: ChatView,      meta: { title: 'Chat', group: 'Work', icon: 'chat', nav: 'primary', navOrder: 10, platforms: ['web', 'desktop'] } },
  // Draft state: a clean composer with no session key until the first send.
  { path: '/chat/new',  name: 'chat-new',  component: ChatView,      meta: { title: 'Chat', group: 'Work', icon: 'chat', platforms: ['web', 'desktop'] } },
  { path: '/sessions',  name: 'sessions',  component: SessionsView,  meta: { title: 'Sessions', group: 'Work', icon: 'sessions', nav: 'primary', navOrder: 20, platforms: ['web', 'desktop'], keepAlive: true } },
  // Status and Usage share the Overview destination. Runtime logs remain a
  // kept-alive diagnostic deep link rather than a peer navigation tab.
  { path: '/overview',  name: 'overview',  component: OverviewHubView, meta: { title: 'Status', titleKey: 'nav.status', group: 'Work', icon: 'home', nav: 'primary', navOrder: 30, navLabelKey: 'nav.overview', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'overview-hub' } },
  { path: '/usage',     name: 'usage',     component: OverviewHubView, meta: { title: 'Usage', icon: 'usage', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'overview-hub' } },
  { path: '/logs',      name: 'logs',      component: LogsView, meta: { title: 'Logs', icon: 'logs', platforms: ['web', 'desktop'], keepAlive: true } },
  // Approvals retired as a front-end destination: the pending queue resolves
  // inline in the chat transcript (ApprovalCard) and via the topbar interrupt
  // pill. The old deep link redirects to Sessions so bookmarks and the pill
  // degrade gracefully
  // (openBlockedApprovalSession() routes straight to the blocked chat first).
  { path: '/approvals', redirect: '/sessions' },
  // Agent administration remains available as an advanced deep link, but is
  // intentionally absent from primary navigation and cold-start restoration.
  { path: '/agents',    name: 'agents',    component: AgentsView,    meta: { title: 'Agents', icon: 'agents', platforms: ['web', 'desktop'], keepAlive: true } },
  // Skills and Channels form one primary destination. /skills remains the
  // rail target while both canonical routes share the same kept-alive hub.
  { path: '/skills',    name: 'skills',    component: SkillsChannelsHubView, meta: { title: 'Skills', group: 'Work', icon: 'skills', nav: 'primary', navOrder: 40, navLabelKey: 'nav.skillsChannels', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'skills-channels-hub' } },
  { path: '/channels',  name: 'channels',  component: SkillsChannelsHubView, meta: { title: 'Channels', icon: 'channels', platforms: ['web', 'desktop'], keepAlive: true, viewKey: 'skills-channels-hub' } },
  { path: '/cron',      name: 'cron',      component: CronView,      meta: { title: 'Cron', group: 'Work', icon: 'cron', nav: 'primary', navOrder: 50, platforms: ['web', 'desktop'], keepAlive: true } },
  // Editorial surface (read, not operated): the first route to opt into an
  // Axis-B expressive skin. Not in the primary nav — reached by URL / links.
  { path: '/changelog', name: 'changelog', component: ChangelogView, meta: { title: 'Changelog', platforms: ['web', 'desktop'], skin: 'out-of-register' } },
  // Readiness/doctor moved inline into Overview; the old deep link stays valid.
  { path: '/health',    redirect: '/overview' },
]
