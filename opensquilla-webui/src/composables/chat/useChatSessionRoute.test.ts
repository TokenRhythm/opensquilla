// @vitest-environment happy-dom
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatSessionRoute } from './useChatSessionRoute'

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
})
