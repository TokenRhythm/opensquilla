import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import { describe, expect, it, vi } from 'vitest'
import { useSessions } from '@/composables/useSessions'
import { resolveOptimisticChatTitle } from '@/composables/chat/useChatSessionTitles'
import { normalizeAgentId } from '@/utils/chat/sessionKeys'
import { activeTaskWasDeletedWithProjectHistory } from '@/utils/projectHistory'
import appSource from './App.vue?raw'

const key = 'agent:main:webchat:local-created'
const freshDraftKey = 'agent:main:webchat:fresh-draft'
function extract(start: string, end: string) {
  const begin = appSource.indexOf(start)
  const finish = appSource.indexOf(end, begin)
  if (begin < 0 || finish < 0) throw new Error('App projection boundary changed')
  return transpileModule(appSource.slice(begin, finish), {
    compilerOptions: { target: ScriptTarget.ES2022 },
  }).outputText
}
// Run App's real projection, watches and deletion handler with Vue and useSessions.
// The deleted-key ledger declaration must be extracted too: App.vue declares it
// before the optimistic ledger watch (which runs with `immediate: true`).
const source = [
  extract('const DELETED_SESSION_KEY_MEMORY_LIMIT', '// Pending optimistic renames'),
  extract('function removeLocalSessions(', 'async function deleteSessions('),
  extract('function syntheticChatSession(', 'const SIDEBAR_SESSION_ORDER_KEY'),
  extract('async function onProjectDeleteHistory(', 'async function onProjectRemove('),
  extract('async function deleteSessions(', 'function openBlockedApprovalSession('),
].join('\n')

function setup() {
  const scope = effectScope()
  const listPage = vi.fn()
  const sessions = useSessions({ listPage, count: vi.fn(), resolve: vi.fn(), search: vi.fn() })
  const localChatSessions = ref<Record<string, unknown>>({})
  const currentSessionKey = ref(key)
  const chatRouteHeaderSessionKey = ref(key)
  const chatRouteHeaderTitle = ref('Local title')
  const deleted = [key]
  const dispatchLocalSessionsDeleted = vi.fn()
  const removePendingApprovalsForSessions = vi.fn()
  const sessionLifecycle = { remove: vi.fn(async () => ({ deleted: [] as string[] })) }
  const params = {
    computed, watch, reactive, normalizeAgentId, resolveOptimisticChatTitle,
    allSessions: sessions.allSessions, localChatSessions, currentSessionKey,
    chatRouteHeaderSessionKey, chatRouteHeaderTitle,
    chatRouteHeaderOptimisticTitle: ref('Local title'),
    sessionLifecycle, localSessionsDeletedDetail: vi.fn(() => null),
    scheduleSessionRefresh: vi.fn(),
    freshTaskDraft: {
      materializedWorkspaceBySession: ref({}), confirmMaterializedProjectTask: vi.fn(),
    },
    projectWorkspaces: {
      byId: ref(new Map()), deleteWorkspaceHistory: vi.fn(async () => ({ deletedSessionKeys: deleted })),
    },
    activeProjectDraftId: ref(''), activeProjectDraftKey: ref(''),
    gatewayAccess: { canManageProjectWorkspaces: true }, activeTaskWasDeletedWithProjectHistory,
    sessionTaskAttention: { removeMany: vi.fn() }, loadSessions: sessions.loadSessions,
    openDefaultDraft: () => {
      // Deleting the current conversation hands the chat header to the fresh
      // draft synchronously (requestFreshTask → startDraftSession) while the
      // route change to /chat/new lands asynchronously afterwards.
      chatRouteHeaderSessionKey.value = freshDraftKey
      return Promise.resolve().then(() => {
        currentSessionKey.value = ''
      })
    },
    appStore: { removePendingApprovalsForSessions }, dispatchLocalSessionsDeleted,
    APP_SESSION_SYNC_SOURCE: 'app', pushToast: vi.fn(), t: (value: string) => value,
    errorMessage: String,
  }
  const app = scope.run(() => new Function(...Object.keys(params), `${source};
    return { sidebarSessionItems, onProjectDeleteHistory, onDeleteSession, rememberDeletedSessions }`)(...Object.values(params)))!
  const row = { key, title: 'Confirmed title', runStatus: 'idle', updatedAt: 10, workspaceId: 'p' }
  const page = (items: unknown[]) => ({ items, hasMore: false, nextCursor: null })
  return { scope, app, sessions, localChatSessions, currentSessionKey, chatRouteHeaderSessionKey,
    sessionLifecycle, listPage, row, page,
    dispatchLocalSessionsDeleted, removePendingApprovalsForSessions }
}

describe('App optimistic session projection lifetime', () => {
  it('retires a confirmed provisional row so a later deletion snapshot cannot resurrect it', async () => {
    const h = setup()
    try {
      expect(h.localChatSessions.value[key]).toBeDefined()
      h.listPage.mockResolvedValue(h.page([h.row]))
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.localChatSessions.value[key]).toBeUndefined()
      expect(h.app.sidebarSessionItems.value[0].title).toBe('Confirmed title')
      h.currentSessionKey.value = ''
      h.listPage.mockResolvedValue(h.page([]))
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.app.sidebarSessionItems.value).toEqual([])
    } finally { h.scope.stop() }
  })

  it('removes explicitly deleted project tasks even before the directory has confirmed them', async () => {
    const h = setup()
    try {
      expect(h.localChatSessions.value[key]).toBeDefined()
      h.listPage.mockResolvedValue(h.page([]))
      await h.app.onProjectDeleteHistory('p')
      await nextTick()
      expect(h.currentSessionKey.value).toBe('')
      expect(h.localChatSessions.value[key]).toBeUndefined()
      expect(h.app.sidebarSessionItems.value).toEqual([])
      expect(h.dispatchLocalSessionsDeleted).toHaveBeenCalledWith(new Set([key]), 'app')
      expect(h.removePendingApprovalsForSessions).toHaveBeenCalledWith(new Set([key]))
    } finally { h.scope.stop() }
  })

  it('preserves an unconfirmed local task absent from a partial directory snapshot', async () => {
    const h = setup()
    try {
      h.listPage.mockResolvedValue({ items: [], hasMore: true, nextCursor: 'next-page' })
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.localChatSessions.value[key]).toBeDefined()
      expect(h.app.sidebarSessionItems.value.map((row: { key: string }) => row.key)).toEqual([key])
    } finally { h.scope.stop() }
  })

  it('does not resurrect the deleted current conversation as a current-task row', async () => {
    const h = setup()
    try {
      h.listPage.mockResolvedValue(h.page([h.row]))
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.app.sidebarSessionItems.value.map((row: { key: string }) => row.key)).toEqual([key])
      h.listPage.mockResolvedValue(h.page([]))
      h.sessionLifecycle.remove.mockResolvedValue({ deleted: [key] })
      await h.app.onDeleteSession(key)
      await nextTick()
      expect(h.localChatSessions.value[key]).toBeUndefined()
      expect(h.app.sidebarSessionItems.value).toEqual([])
    } finally { h.scope.stop() }
  })

  it('drops the current-task fallback when the open session is deleted from another surface', async () => {
    const h = setup()
    try {
      h.listPage.mockResolvedValue(h.page([h.row]))
      await h.sessions.loadSessions()
      await nextTick()
      // External deletion (Sessions page / CLI): the key is marked deleted
      // while the route still points at it and no navigation happens.
      h.listPage.mockResolvedValue(h.page([]))
      h.app.rememberDeletedSessions(new Set([key]))
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.app.sidebarSessionItems.value).toEqual([])
    } finally { h.scope.stop() }
  })

  it('keeps the resolved optimistic title when the chat surface moves on to a fresh draft', async () => {
    const h = setup()
    try {
      h.listPage.mockResolvedValue(h.page([]))
      await h.sessions.loadSessions()
      await nextTick()
      expect(h.localChatSessions.value[key]).toMatchObject({ title: 'Local title' })
      // Starting a new task re-registers the header with the fresh draft key
      // before the route leaves the conversation that was just created.
      h.chatRouteHeaderSessionKey.value = freshDraftKey
      await nextTick()
      expect(h.localChatSessions.value[key]).toMatchObject({ title: 'Local title' })
    } finally { h.scope.stop() }
  })
})
