import { describe, expect, it } from 'vitest'
import { sessionAgentIdentity, sessionStatusBadge } from './sessionDisplay'
import type { SessionItem } from '@/composables/useSessions'

function sessionItem(overrides: Partial<SessionItem>): SessionItem {
  return {
    key: 'agent:main:webchat:test',
    title: 'Test chat',
    subtitle: '',
    groupLabel: 'main',
    effectiveAgentId: 'main',
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    threadLabel: '',
    channelContext: null,
    status: 'killed',
    visualStatus: 'killed',
    runStatus: 'cancelled',
    runLabel: 'Stopped after 1s',
    messageCount: 1,
    updatedAt: 1000,
    interactive: true,
    forkedFromParent: false,
    contractGaps: [],
    raw: { key: 'agent:main:webchat:test' },
    ...overrides,
  }
}

describe('sessionStatusBadge', () => {
  it('shows the normalized stop label for cancelled sessions', () => {
    const badge = sessionStatusBadge(sessionItem({ runStatus: 'cancelled', runLabel: 'Stopped after 1s' }))

    expect(badge?.label).toBe('Stopped after 1s')
  })

  it('shows the normalized stop label for interrupted sessions', () => {
    const badge = sessionStatusBadge(sessionItem({
      runStatus: 'interrupted',
      runLabel: 'Output interrupted',
    }))

    expect(badge?.label).toBe('Output interrupted')
  })
})

describe('sessionAgentIdentity', () => {
  const agents = new Map([['main', 'Main agent']])

  it('does not report deletion while agents.list has not succeeded', () => {
    expect(sessionAgentIdentity('retired', agents, false)).toEqual({
      kind: 'raw',
      value: 'retired',
    })
  })

  it('reports a missing effective agent only after the catalog loaded', () => {
    expect(sessionAgentIdentity('retired', agents, true)).toEqual({
      kind: 'deleted',
      value: 'retired',
    })
    expect(sessionAgentIdentity('main', agents, true)).toEqual({
      kind: 'known',
      value: 'Main agent',
    })
  })
})
