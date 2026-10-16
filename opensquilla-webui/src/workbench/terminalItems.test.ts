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

  it('keeps UI scope separate from the standalone execution target', () => {
    const item = createTerminalWorkbenchItem('draft-a', { kind: 'default-agent', agentId: 'main' })
    const project = createTerminalWorkbenchItem('draft-a', { kind: 'project', workspaceId: 'project-a' })
    expect(item.scope).toEqual({ type: 'session', id: 'draft-a' })
    expect(item.payload).toEqual({ target: { kind: 'default-agent', agentId: 'main' } })
    expect(project.id).not.toBe(item.id)
    expect(createTerminalWorkbenchItem('draft-b', { kind: 'default-agent', agentId: 'main' }).id).not.toBe(item.id)
  })
})
