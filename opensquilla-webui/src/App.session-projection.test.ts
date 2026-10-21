import { computed, effectScope, nextTick, ref, watch } from 'vue'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import { describe, expect, it, vi } from 'vitest'
import { useSessions } from '@/composables/useSessions'
import { resolveOptimisticChatTitle } from '@/composables/chat/useChatSessionTitles'
import { normalizeAgentId } from '@/utils/chat/sessionKeys'
import { activeTaskWasDeletedWithProjectHistory } from '@/utils/projectHistory'
import appSource from './App.vue?raw'

const key = 'agent:main:webchat:local-created'
function extract(start: string, end: string) {
  const begin = appSource.indexOf(start)
  const finish = appSource.indexOf(end, begin)
  if (begin < 0 || finish < 0) throw new Error('App projection boundary changed')
  return transpileModule(appSource.slice(begin, finish), {
    compilerOptions: { target: ScriptTarget.ES2022 },
  }).outputText
}
// Run App's real projection, watches and deletion handler with Vue and useSessions.
const source = [
  extract('function removeLocalSessions(', 'function handleLocalSessionsDeleted('),
  extract('function syntheticChatSession(', 'const SIDEBAR_SESSION_ORDER_KEY'),
  extract('async function onProjectDeleteHistory(', 'async function onProjectRemove('),
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
  const params = {
    computed, watch, normalizeAgentId, resolveOptimisticChatTitle,
    allSessions: sessions.allSessions, localChatSessions, currentSessionKey,
    chatRouteHeaderSessionKey, chatRouteHeaderTitle,
    chatRouteHeaderOptimisticTitle: ref('Local title'),
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
      currentSessionKey.value = ''
      chatRouteHeaderSessionKey.value = ''
      chatRouteHeaderTitle.value = ''
    },
    appStore: { removePendingApprovalsForSessions }, dispatchLocalSessionsDeleted,
    APP_SESSION_SYNC_SOURCE: 'app', pushToast: vi.fn(), t: (value: string) => value,
    errorMessage: String,
  }
  const app = scope.run(() => new Function(...Object.keys(params), `${source};
    return { sidebarSessionItems, onProjectDeleteHistory }`)(...Object.values(params)))!
  const row = { key, title: 'Confirmed title', runStatus: 'idle', updatedAt: 10, workspaceId: 'p' }
  const page = (items: unknown[]) => ({ items, hasMore: false, nextCursor: null })
  return { scope, app, sessions, localChatSessions, currentSessionKey, listPage, row, page,
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
})
