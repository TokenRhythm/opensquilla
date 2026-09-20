import type { Ref } from 'vue'
import { isNavigationFailure, NavigationFailureType, useRoute, useRouter } from 'vue-router'
import {
  recentDraftSessionKey,
  recoverableDraftSessionKey,
} from '@/composables/chat/useChatDraftPersistence'
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

export interface ResolveInitialSessionOptions {
  recoverDraft?: boolean
  scopedDraft?: ScopedDraftHistoryState | null
}

export interface ScopedDraftHistoryState {
  sessionKey: string
  agentId: string
  projectId: string
  hasAttachments?: boolean
}

export interface InitialSessionResolution {
  sessionKey: string
  hasUrlSession: boolean
  draft: boolean
  recoveredDraft: boolean
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

function readStoredSession(): string {
  try {
    const key = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY) || ''
    return key ? canonicalSessionKey(key) : ''
  } catch {
    return ''
  }
}

export function useChatSessionRoute(
  sessionKey: Ref<string>,
  guestSessionOwnerId: () => string | null = () => null,
) {
  const route = useRoute()
  const router = useRouter()
  // Only keys minted in this mounted view can change namespace before a send.
  // A recovered draft or explicit URL may refer to durable owner history.
  let freshDraftSessionKey = ''

  function forgetFreshDraftSession(key = sessionKey.value) {
    if (freshDraftSessionKey === key) freshDraftSessionKey = ''
  }

  function inGuestNamespace(key: string): string {
    const ownerId = guestSessionOwnerId()
    if (!ownerId || !/^[0-9a-f]{64}$/.test(ownerId)) return key
    const suffix = key.slice(key.lastIndexOf(':') + 1)
    return webchatSessionKey(agentIdFromSessionKey(key), `guest:${ownerId}:${suffix}`)
  }

  function rebindFreshDraftSession(rebind: (key: string) => void): boolean {
    const key = sessionKey.value
    if (!key || key !== freshDraftSessionKey || readSessionFromUrl()) return false
    const next = inGuestNamespace(key)
    if (next === key) return false
    rebind(next)
    freshDraftSessionKey = sessionKey.value === next ? next : ''
    return sessionKey.value === next
  }

  function persistSession(key: string, options: PersistSessionOptions = {}) {
    forgetFreshDraftSession()
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

  /** Change only this draft's project, including its reload recovery scope. */
  async function replaceDraftProject(projectId: string | null): Promise<boolean> {
    if (!isDraftRoute() || !sessionKey.value) return false
    const key = sessionKey.value
    const agentId = draftAgentId()
    const project = projectId || ''
    const state = { draftSessionKey: key, draftAgentId: agentId, draftProjectId: project }
    try {
      const failure = await router.replace({
        path: DRAFT_CHAT_PATH,
        query: { agent: agentId, ...(project ? { project } : {}) },
        state,
      })
      const duplicate = isNavigationFailure(failure, NavigationFailureType.duplicated)
      if (
        (failure && !duplicate)
        || sessionKey.value !== key
        || !isDraftRoute()
        || draftAgentId() !== agentId
        || readProjectFromUrl() !== project
      ) return false
      // Vue Router skips history state on a duplicate navigation. The route
      // already matches, but an untouched draft may not have a recovery scope.
      if (duplicate) window.history.replaceState({ ...window.history.state, ...state }, '')
      return true
    } catch {
      return false
    }
  }

  function createSessionKey(agentId?: string): string {
    const agent = agentId || agentIdFromSessionKey(sessionKey.value)
    freshDraftSessionKey = inGuestNamespace(
      webchatSessionKey(agent, Math.random().toString(36).slice(2, 10)),
    )
    return freshDraftSessionKey
  }

  function resolveInitialSession(
    options: ResolveInitialSessionOptions = {},
  ): InitialSessionResolution {
    const urlSession = readSessionFromUrl()
    if (urlSession) {
      freshDraftSessionKey = ''
      return {
        sessionKey: canonicalSessionKey(urlSession),
        hasUrlSession: true,
        draft: false,
        recoveredDraft: false,
      }
    }
    const mayRecoverDraft = options.recoverDraft !== false && isDraftRoute()
    if (mayRecoverDraft) {
      const scopedDraft = options.scopedDraft
      const scopedSessionKey = scopedDraft
        && scopedDraft.agentId === draftAgentId()
        && scopedDraft.projectId === readProjectFromUrl()
        && agentIdFromSessionKey(scopedDraft.sessionKey) === draftAgentId()
        ? recoverableDraftSessionKey(scopedDraft.sessionKey, scopedDraft.hasAttachments === true)
        : ''
      const mayRecoverRecentDraft = !readAgentFromUrl()
        && !readProjectFromUrl()
        && !hasLegacyNewChatQuery()
      const recoveredSessionKey = scopedSessionKey
        || (mayRecoverRecentDraft ? recentDraftSessionKey() : '')
      if (recoveredSessionKey) {
        freshDraftSessionKey = ''
        return {
          sessionKey: recoveredSessionKey,
          hasUrlSession: false,
          draft: readStoredSession() !== recoveredSessionKey,
          recoveredDraft: true,
        }
      }
    }
    // No explicit session in the URL: open a clean draft instead of silently
    // restoring a previous session.
    return {
      sessionKey: createSessionKey(draftAgentId()),
      hasUrlSession: false,
      draft: true,
      recoveredDraft: false,
    }
  }

  return {
    route,
    createSessionKey,
    forgetFreshDraftSession,
    rebindFreshDraftSession,
    draftAgentId,
    goToDraft,
    replaceDraftProject,
    hasLegacyNewChatQuery,
    isDraftRoute,
    persistSession,
    readAgentFromUrl,
    readProjectFromUrl,
    readSessionFromUrl,
    resolveInitialSession,
  }
}
