import { describe, expect, it } from 'vitest'
import { createTerminalWorkbenchItem } from './terminalItems'

describe('terminal Workbench items', () => {
  it('is session-scoped and renderer-owned so remote WebUI needs no native surface', () => {
    const first = createTerminalWorkbenchItem('session-a')
    const second = createTerminalWorkbenchItem('session-a')
    expect(first).toMatchObject({ kind: 'terminal', scope: { type: 'session', id: 'session-a' }, hostKind: 'dom', retention: 'keep-alive' })
    expect(first.id).toBe(second.id)
    expect(first.payload).toEqual({ sessionKey: 'session-a' })
  })
})
