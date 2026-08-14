import { describe, expect, it } from 'vitest'

import { activeTaskWasDeletedWithProjectHistory } from './projectHistory'

describe('activeTaskWasDeletedWithProjectHistory', () => {
  it('detects the active task from the backend deletion result before list refresh', () => {
    expect(activeTaskWasDeletedWithProjectHistory({
      workspaceId: 'project-a',
      currentSessionKey: 'agent:main:webchat:just-created',
      sessions: [],
      deletedSessionKeys: ['agent:main:webchat:just-created'],
    })).toBe(true)
  })

  it('does not treat a still-provisional project draft as deleted history', () => {
    expect(activeTaskWasDeletedWithProjectHistory({
      workspaceId: 'project-a',
      currentSessionKey: '',
      sessions: [],
      deletedSessionKeys: [],
    })).toBe(false)
  })

  it('does not leave an unrelated active task', () => {
    expect(activeTaskWasDeletedWithProjectHistory({
      workspaceId: 'project-a',
      currentSessionKey: 'agent:main:webchat:ordinary',
      sessions: [{ key: 'agent:main:webchat:ordinary' }],
      deletedSessionKeys: ['agent:main:webchat:project'],
    })).toBe(false)
  })
})
