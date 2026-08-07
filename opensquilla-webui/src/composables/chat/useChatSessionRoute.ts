import type { Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  agentIdFromSessionKey,
  canonicalSessionKey,
  webchatSessionKey,
} from '@/utils/chat/sessionKeys'
import { recordSessionNavigationDiag } from '@/utils/chat/sessionNavigationDiag'

const ACTIVE_SESSION_STORAGE_KEY = 'opensquilla_active_session'
const DRAFT_CHAT_PATH = '/chat/new'

export interface PersistSessionOptions {
  updateRoute?: boolean
  source?: string
}

export interface InitialDraftCanonicalizationState {
  disposed: boolean
  initialFullPath: string
  currentFullPath: string
  currentPathIsDraft: boolean
  hasLegacyNewChatQuery: boolean
}

export function shouldCanonicalizeInitialDraftRoute(
  state: InitialDraftCanonicalizationState,
): boolean {
  return (
    !state.disposed
    && state.currentFullPath === state.initialFullPath
    && (!state.currentPathIsDraft || state.hasLegacyNewChatQuery)
  )
}

function routeStringParam(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function writeStoredSession(key: string) {
  try {
    localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, key)
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}

export function useChatSessionRoute(sessionKey: Ref<string>) {
  const route = useRoute()
  const router = useRouter()

  function persistSession(key: string, options: PersistSessionOptions = {}) {
    const previous = sessionKey.value
    const next = canonicalSessionKey(key)
    const routeSession = readSessionFromUrl()
    recordSessionNavigationDiag(options.source || 'persistSession', {
      from: previous,
      to: next,
      routeSession,
      reason: options.updateRoute === false ? 'state_only' : 'route_replace',
    })

    sessionKey.value = next
    writeStoredSession(sessionKey.value)
    if (options.updateRoute === false) return
    if (readSessionFromUrl() === sessionKey.value) return
    router.replace({ path: '/chat', query: { session: sessionKey.value } }).catch(() => {})
  }

  function isDraftRoute(): boolean {
    return route.path === DRAFT_CHAT_PATH
  }

  function hasLegacyNewChatQuery(): boolean {
    return route.query.newChat === '1' || route.query.new === '1'
  }

  function readSessionFromUrl(): string {
    return routeStringParam(route.query.session)
  }

  function readAgentFromUrl(): string {
    return routeStringParam(route.query.agent)
  }

  function readProjectFromUrl(): string {
    return routeStringParam(route.query.project)
  }

  function draftAgentId(): string {
    return readAgentFromUrl() || 'main'
  }

  function goToDraft(options: {
    agentId?: string
    projectId?: string | null
    replace?: boolean
  } = {}) {
    const agent = options.agentId || readAgentFromUrl()
    const project = options.projectId === undefined
      ? readProjectFromUrl()
      : options.projectId || ''
    const target = {
      path: DRAFT_CHAT_PATH,
      query: {
        ...(agent ? { agent } : {}),
        ...(project ? { project } : {}),
      },
    }
    const navigation = options.replace ? router.replace(target) : router.push(target)
    navigation.catch(() => {})
  }

  function createSessionKey(agentId?: string): string {
    const agent = agentId || agentIdFromSessionKey(sessionKey.value)
    return webchatSessionKey(agent, Math.random().toString(36).slice(2, 10))
  }

  function resolveInitialSession(): { sessionKey: string; hasUrlSession: boolean; draft: boolean } {
    const urlSession = readSessionFromUrl()
    if (urlSession) {
      return { sessionKey: canonicalSessionKey(urlSession), hasUrlSession: true, draft: false }
    }
    // No explicit session in the URL: open a clean draft instead of silently
    // restoring a previous session.
    return { sessionKey: createSessionKey(draftAgentId()), hasUrlSession: false, draft: true }
  }

  return {
    route,
    createSessionKey,
    draftAgentId,
    goToDraft,
    hasLegacyNewChatQuery,
    isDraftRoute,
    persistSession,
    readAgentFromUrl,
    readProjectFromUrl,
    readSessionFromUrl,
    resolveInitialSession,
  }
}
