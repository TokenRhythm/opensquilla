import { describe, expect, it } from 'vitest'
import { usageTaskDisplayName } from './taskDisplayName'
import { usageSession } from '@/testing/usage.test-helper'

describe('usageTaskDisplayName', () => {
  it('keeps a parent generation title separate from the reusable navigation key', () => {
    const row = usageSession({ sessionKey: 'reused-key', taskGroupId: 'old-task',
      sessionKind: 'root', sessionId: 'root', sessionEpoch: 0, title: 'Historical parent' })
    const currentNames = new Map([['reused-key', 'New generation title']])
    expect(usageTaskDisplayName(row, currentNames, 'Untitled task')).toBe('Historical parent')
    expect(usageTaskDisplayName({ ...row, title: '' }, currentNames, 'Untitled task')).toBe('Untitled task')
  })

  it('prefers a task name carried by the usage row', () => {
    expect(usageTaskDisplayName(
      usageSession({
        sessionKey: 'agent:main:webchat:private-id',
        taskName: 'Prepare release notes',
      }),
      new Map([['agent:main:webchat:private-id', 'Older mapped title']]),
      'Untitled task',
    )).toBe('Prepare release notes')
  })

  it('resolves a task title from the shared session directory', () => {
    expect(usageTaskDisplayName(
      usageSession({ sessionKey: 'agent:main:webchat:private-id' }),
      new Map([['agent:main:webchat:private-id', 'Review launch metrics']]),
      'Untitled task',
    )).toBe('Review launch metrics')
  })

  it('never exposes internal task identifiers as fallback labels', () => {
    expect(usageTaskDisplayName(
      usageSession({
        session: 'agent:main:webchat:private-id',
        title: 'agent:main:webchat:private-id',
      }),
      new Map(),
      'Untitled task',
    )).toBe('Untitled task')
  })
})
