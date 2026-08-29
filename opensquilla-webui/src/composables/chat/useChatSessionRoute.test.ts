// @vitest-environment happy-dom
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  shouldCanonicalizeInitialDraftRoute,
  useChatSessionRoute,
} from './useChatSessionRoute'
import { RECENT_DRAFT_SESSION_KEY } from './useChatDraftPersistence'

const { routeMock, routerMock } = vi.hoisted(() => ({
  routeMock: {
    path: '/chat/new',
    query: {} as Record<string, string>,
  },
  routerMock: {
    push: vi.fn(() => Promise.resolve()),
    replace: vi.fn(() => Promise.resolve()),
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => routerMock,
}))

describe('useChatSessionRoute', () => {
  beforeEach(() => {
    routeMock.path = '/chat/new'
    routeMock.query = {}
    routerMock.push.mockClear()
    routerMock.replace.mockClear()
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
})
