// @vitest-environment happy-dom
import { effectScope, nextTick, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import {
  shouldCanonicalizeInitialDraftRoute,
  useChatSessionRoute,
} from './useChatSessionRoute'
import { RECENT_DRAFT_SESSION_KEY, useChatDraftPersistence } from './useChatDraftPersistence'

const { routeMock, routerMock } = vi.hoisted(() => ({
  routeMock: {
    path: '/chat/new',
    query: {} as Record<string, string>,
  },
  routerMock: {
    push: vi.fn(() => Promise.resolve()),
    replace: vi.fn((_target: unknown): Promise<unknown> => Promise.resolve()),
  },
}))

vi.mock('vue-router', async importOriginal => ({
  ...await importOriginal<typeof import('vue-router')>(),
  useRoute: () => routeMock,
  useRouter: () => routerMock,
}))

describe('useChatSessionRoute', () => {
  beforeEach(() => {
    routeMock.path = '/chat/new'
    routeMock.query = {}
    routerMock.push.mockClear()
    routerMock.replace.mockReset().mockResolvedValue(undefined)
    localStorage.clear()
  })

  it('uses an explicit Agent deep link for the provisional session key', () => {
    routeMock.query = { agent: 'research' }
    const route = useChatSessionRoute(ref(''))

    expect(route.draftAgentId()).toBe('research')
    expect(route.resolveInitialSession()).toMatchObject({
      sessionKey: expect.stringMatching(/^agent:research:webchat:[a-z0-9]+$/),
      hasUrlSession: false,
      draft: true,
    })
  })

  it('defaults an ordinary draft to the main Agent', () => {
    const route = useChatSessionRoute(ref(''))

    expect(route.draftAgentId()).toBe('main')
    expect(route.resolveInitialSession().sessionKey).toMatch(/^agent:main:webchat:[a-z0-9]+$/)
  })

  it('mints an owned guest key before the first live subscription', () => {
    routeMock.query = { agent: 'research' }
    const route = useChatSessionRoute(ref(''), () => 'a'.repeat(64))
    expect(route.resolveInitialSession()).toMatchObject({
      sessionKey: expect.stringMatching(/^agent:research:webchat:guest:a{64}:[a-z0-9]+$/),
      draft: true,
      recoveredDraft: false,
    })
  })

  it('binds a pre-Hello fresh draft once while preserving text typed before the watcher flush', async () => {
    const ownerId = ref<string | null>(null)
    const sessionKey = ref('')
    const inputText = ref('')
    const route = useChatSessionRoute(sessionKey, () => ownerId.value)
    sessionKey.value = route.resolveInitialSession().sessionKey
    const originalKey = sessionKey.value
    const scope = effectScope()
    const persistence = scope.run(() => useChatDraftPersistence({ sessionKey, inputText }))!
    try {
      inputText.value = 'Draft before Hello'
      await nextTick()
      ownerId.value = 'b'.repeat(64)
      expect(route.rebindFreshDraftSession(persistence.rebindCurrentDraft)).toBe(true)
      inputText.value += ', still typing'
      await nextTick()
      const suffix = originalKey.slice(originalKey.lastIndexOf(':') + 1)
      expect(sessionKey.value).toBe(`agent:main:webchat:guest:${ownerId.value}:${suffix}`)
      expect(inputText.value).toBe('Draft before Hello, still typing')
      expect(localStorage.getItem(`opensquilla.chat.draft:${originalKey}`)).toBeNull()
      expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBe(sessionKey.value)
      expect(route.rebindFreshDraftSession(persistence.rebindCurrentDraft)).toBe(false)
      ownerId.value = null // Becoming an owner does not rename a guest session.
      expect(route.rebindFreshDraftSession(persistence.rebindCurrentDraft)).toBe(false)
      expect(routerMock.replace).not.toHaveBeenCalled()
    } finally { scope.stop() }
  })

  it.each([
    'agent:main:webchat:owner-history',
    `agent:main:webchat:guest:${'c'.repeat(64)}:foreign-history`,
  ])('does not rewrite an explicit history URL: %s', key => {
    routeMock.path = '/chat'
    routeMock.query = { session: key }
    const sessionKey = ref('')
    const route = useChatSessionRoute(sessionKey, () => 'a'.repeat(64))
    sessionKey.value = route.resolveInitialSession().sessionKey
    const rebind = vi.fn()
    expect(route.rebindFreshDraftSession(rebind)).toBe(false)
    expect(sessionKey.value).toBe(key)
    expect(rebind).not.toHaveBeenCalled()
  })

  it.each([false, true])('does not rewrite a recovered draft, active=%s', active => {
    const key = 'agent:main:webchat:recovered-owner-draft'
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'Keep this owner draft')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    if (active) localStorage.setItem('opensquilla_active_session', key)
    const sessionKey = ref('')
    const route = useChatSessionRoute(sessionKey, () => 'a'.repeat(64))
    const initial = route.resolveInitialSession()
    expect(initial.recoveredDraft).toBe(true)
    sessionKey.value = initial.sessionKey
    expect(route.rebindFreshDraftSession(next => { sessionKey.value = next })).toBe(false)
    expect(sessionKey.value).toBe(key)
  })

  it.each(['accepted', 'delivery-started'] as const)(
    'never rebinds a fresh key after %s', boundary => {
      const ownerId = ref<string | null>(null)
      const sessionKey = ref('')
      const route = useChatSessionRoute(sessionKey, () => ownerId.value)
      sessionKey.value = route.resolveInitialSession().sessionKey
      const key = sessionKey.value
      if (boundary === 'accepted') route.persistSession(key, { updateRoute: false })
      else route.forgetFreshDraftSession()
      ownerId.value = 'a'.repeat(64)
      expect(route.rebindFreshDraftSession(next => { sessionKey.value = next })).toBe(false)
      expect(sessionKey.value).toBe(key)
    },
  )

  it('recovers a provisional new-task draft on a cold /chat/new entry', () => {
    const key = 'agent:main:webchat:cold-draft'
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'unfinished task')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    const route = useChatSessionRoute(ref(''))

    expect(route.resolveInitialSession()).toEqual({
      sessionKey: key,
      hasUrlSession: false,
      draft: true,
      recoveredDraft: true,
    })
  })

  it('recovers an existing active session draft as a routable session', () => {
    const key = 'agent:main:webchat:existing'
    localStorage.setItem('opensquilla_active_session', key)
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'unfinished reply')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    const route = useChatSessionRoute(ref(''))

    expect(route.resolveInitialSession()).toEqual({
      sessionKey: key,
      hasUrlSession: false,
      draft: false,
      recoveredDraft: true,
    })
  })

  it('does not recover a previous draft for an explicit new task', () => {
    const key = 'agent:main:webchat:previous'
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'do not restore')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    const route = useChatSessionRoute(ref(''))

    const initial = route.resolveInitialSession({ recoverDraft: false })

    expect(initial.sessionKey).not.toBe(key)
    expect(initial).toMatchObject({ draft: true, recoveredDraft: false })
  })

  it('does not recover a draft after it was sent or cleared', () => {
    const key = 'agent:main:webchat:sent'
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    const route = useChatSessionRoute(ref(''))

    const initial = route.resolveInitialSession()

    expect(initial.sessionKey).not.toBe(key)
    expect(initial.recoveredDraft).toBe(false)
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()
  })

  it('does not let Agent or project draft entries recover an unrelated recent draft', () => {
    const key = 'agent:main:webchat:previous'
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'do not restore')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
    routeMock.query = { agent: 'research' }
    const route = useChatSessionRoute(ref(''))

    expect(route.resolveInitialSession()).toMatchObject({
      sessionKey: expect.stringMatching(/^agent:research:webchat:/),
      recoveredDraft: false,
    })

    routeMock.query = { agent: 'main', project: 'project-a' }
    expect(route.resolveInitialSession()).toMatchObject({
      sessionKey: expect.stringMatching(/^agent:main:webchat:/),
      recoveredDraft: false,
    })
  })

  it('recovers only the route-scoped draft for the same Agent and project', () => {
    const key = 'agent:main:webchat:project-draft'
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'project draft')
    routeMock.query = { agent: 'main', project: 'project-a' }
    const route = useChatSessionRoute(ref(''))

    expect(route.resolveInitialSession({
      scopedDraft: {
        sessionKey: key,
        agentId: 'main',
        projectId: 'project-a',
      },
    })).toMatchObject({
      sessionKey: key,
      draft: true,
      recoveredDraft: true,
    })

    const wrongProject = route.resolveInitialSession({
      scopedDraft: {
        sessionKey: key,
        agentId: 'main',
        projectId: 'project-b',
      },
    })
    expect(wrongProject.sessionKey).not.toBe(key)
    expect(wrongProject.recoveredDraft).toBe(false)

    const wrongAgent = route.resolveInitialSession({
      scopedDraft: {
        sessionKey: key,
        agentId: 'research',
        projectId: 'project-a',
      },
    })
    expect(wrongAgent.sessionKey).not.toBe(key)
    expect(wrongAgent.recoveredDraft).toBe(false)
  })

  it('ignores a route-scoped draft key with no saved text', () => {
    routeMock.query = { agent: 'main' }
    const route = useChatSessionRoute(ref(''))

    const initial = route.resolveInitialSession({
      scopedDraft: {
        sessionKey: 'agent:main:webchat:missing',
        agentId: 'main',
        projectId: '',
      },
    })
    expect(initial.sessionKey).not.toBe('agent:main:webchat:missing')
    expect(initial.recoveredDraft).toBe(false)
  })

  it('keeps only the project id in a project draft route and can return to a default draft', () => {
    routeMock.query = { agent: 'main', project: 'project-a' }
    const route = useChatSessionRoute(ref(''))

    expect(route.readProjectFromUrl()).toBe('project-a')
    route.goToDraft({ replace: true })
    expect(routerMock.replace).toHaveBeenCalledWith({
      path: '/chat/new',
      query: { agent: 'main', project: 'project-a' },
    })

    route.goToDraft({ projectId: null, replace: true })
    expect(routerMock.replace).toHaveBeenLastCalledWith({
      path: '/chat/new',
      query: { agent: 'main' },
    })
  })

  it('never canonicalizes a slow initial draft after the user leaves Chat', () => {
    expect(shouldCanonicalizeInitialDraftRoute({
      disposed: false,
      initialFullPath: '/chat/new',
      currentFullPath: '/settings',
      currentPathIsDraft: false,
      hasLegacyNewChatQuery: false,
    })).toBe(false)

    expect(shouldCanonicalizeInitialDraftRoute({
      disposed: false,
      initialFullPath: '/chat',
      currentFullPath: '/chat',
      currentPathIsDraft: false,
      hasLegacyNewChatQuery: false,
    })).toBe(true)
  })

  it('rebinds the current draft route with its recovery identity in the same navigation', async () => {
    const key = 'agent:main:webchat:keep-draft'
    const sessionKey = ref(key)
    routeMock.query = { agent: 'main' }
    localStorage.setItem(`opensquilla.chat.draft:${key}`, 'keep this prompt')
    routerMock.replace.mockImplementationOnce(async (target: any) => {
      routeMock.path = target.path
      routeMock.query = target.query
    })
    const route = useChatSessionRoute(sessionKey)

    await expect(route.replaceDraftProject('project-a')).resolves.toBe(true)

    expect(sessionKey.value).toBe(key)
    expect(routerMock.replace).toHaveBeenCalledWith({
      path: '/chat/new',
      query: { agent: 'main', project: 'project-a' },
      state: { draftSessionKey: key, draftAgentId: 'main', draftProjectId: 'project-a' },
    })
    expect(route.resolveInitialSession({ scopedDraft: {
      sessionKey: key, agentId: 'main', projectId: 'project-a',
    } })).toMatchObject({ sessionKey: key, recoveredDraft: true, draft: true })
  })

  it('keeps the original draft route when project navigation fails', async () => {
    routeMock.query = { agent: 'main', project: 'project-a' }
    const sessionKey = ref('agent:main:webchat:keep-draft')
    routerMock.replace.mockRejectedValueOnce(new Error('navigation aborted'))
    const route = useChatSessionRoute(sessionKey)

    await expect(route.replaceDraftProject(null)).resolves.toBe(false)

    expect(routeMock.query).toEqual({ agent: 'main', project: 'project-a' })
    expect(sessionKey.value).toBe('agent:main:webchat:keep-draft')
  })

  it('does not commit an aborted navigation reported as a resolved failure', async () => {
    routeMock.query = { agent: 'main', project: 'project-a' }
    routerMock.replace.mockResolvedValueOnce({ type: 4 })
    const route = useChatSessionRoute(ref('agent:main:webchat:keep-draft'))

    await expect(route.replaceDraftProject(null)).resolves.toBe(false)
    expect(route.readProjectFromUrl()).toBe('project-a')
  })

  it('accepts selecting the same project and refreshes its recovery scope', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/chat/new', component: { template: '<div />' } }],
    })
    const target = '/chat/new?agent=main&project=project-a'
    await router.push(target)
    const duplicate = await router.replace(target)
    routerMock.replace.mockResolvedValueOnce(duplicate)
    routeMock.query = { agent: 'main', project: 'project-a' }
    const key = 'agent:main:webchat:keep-draft'
    const route = useChatSessionRoute(ref(key))

    await expect(route.replaceDraftProject('project-a')).resolves.toBe(true)
    expect(window.history.state).toMatchObject({
      draftSessionKey: key, draftAgentId: 'main', draftProjectId: 'project-a',
    })
  })

  it('clears the draft project and its recovery scope without changing its key', async () => {
    const key = 'agent:main:webchat:keep-draft'
    routeMock.query = { agent: 'main', project: 'project-a' }
    routerMock.replace.mockImplementationOnce(async (target: any) => {
      routeMock.path = target.path
      routeMock.query = target.query
    })
    const route = useChatSessionRoute(ref(key))

    await expect(route.replaceDraftProject(null)).resolves.toBe(true)
    expect(routerMock.replace).toHaveBeenCalledWith({
      path: '/chat/new', query: { agent: 'main' },
      state: { draftSessionKey: key, draftAgentId: 'main', draftProjectId: '' },
    })
  })

  it('does not commit a project route after another draft replaces the source', async () => {
    const sessionKey = ref('agent:main:webchat:source')
    let finish!: () => void
    routerMock.replace.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
    const route = useChatSessionRoute(sessionKey)
    const navigation = route.replaceDraftProject('project-a')
    sessionKey.value = 'agent:main:webchat:replacement'
    finish()

    await expect(navigation).resolves.toBe(false)
    expect(sessionKey.value).toBe('agent:main:webchat:replacement')
  })
})
