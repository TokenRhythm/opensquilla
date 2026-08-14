import { describe, expect, it } from 'vitest'
import { usageTaskDisplayName } from './taskDisplayName'

describe('usageTaskDisplayName', () => {
  it('prefers a task name carried by the usage row', () => {
    expect(usageTaskDisplayName(
      {
        sessionKey: 'agent:main:webchat:private-id',
        taskName: 'Prepare release notes',
      },
      new Map([['agent:main:webchat:private-id', 'Older mapped title']]),
      'Untitled task',
    )).toBe('Prepare release notes')
  })

  it('resolves a task title from the shared session directory', () => {
    expect(usageTaskDisplayName(
      { sessionKey: 'agent:main:webchat:private-id' },
      new Map([['agent:main:webchat:private-id', 'Review launch metrics']]),
      'Untitled task',
    )).toBe('Review launch metrics')
  })

  it('never exposes internal task identifiers as fallback labels', () => {
    expect(usageTaskDisplayName(
      {
        session: 'agent:main:webchat:private-id',
        title: 'agent:main:webchat:private-id',
      },
      new Map(),
      'Untitled task',
    )).toBe('Untitled task')
  })
})
