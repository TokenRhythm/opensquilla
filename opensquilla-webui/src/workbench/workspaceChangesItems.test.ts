// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  WORKSPACE_CHANGES_OPEN_EVENT,
  createWorkspaceChangesWorkbenchItem,
  requestWorkspaceChangesOpen,
  workspaceChangesWorkbenchItemId,
  workspaceIdFromWorkbenchItem,
  type WorkspaceChangesOpenEventDetail,
} from './workspaceChangesItems'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('workspace changes workbench item', () => {
  it('opens one workspace-scoped diff panel per project', () => {
    const item = createWorkspaceChangesWorkbenchItem({
      workspaceId: 'workspace-1',
      workspaceName: 'Project A',
    })

    expect(item).not.toBeNull()
    expect(item).toMatchObject({
      id: workspaceChangesWorkbenchItemId('workspace-1'),
      kind: 'diff',
      title: 'Project A',
      // Workspace scope keeps the review open while sessions switch.
      scope: { type: 'workspace', id: 'workspace-1' },
      hostKind: 'dom',
      retention: 'keep-alive',
    })
    expect(workspaceIdFromWorkbenchItem(item!)).toBe('workspace-1')
  })

  it('falls back to the workspace id when no name is known', () => {
    const item = createWorkspaceChangesWorkbenchItem({ workspaceId: ' workspace-1 ' })

    expect(item?.title).toBe('workspace-1')
    expect(item?.payload.workspaceId).toBe('workspace-1')
  })

  it('refuses to build an item without a workspace', () => {
    expect(createWorkspaceChangesWorkbenchItem({ workspaceId: '   ' })).toBeNull()
    expect(workspaceIdFromWorkbenchItem({
      id: 'x',
      kind: 'diff',
      title: 'x',
      scope: { type: 'app' },
      hostKind: 'dom',
      retention: 'keep-alive',
      payload: {},
    })).toBe('')
  })

  it('ignores items of another panel kind', () => {
    expect(workspaceIdFromWorkbenchItem({
      id: 'x',
      kind: 'browser',
      title: 'x',
      scope: { type: 'app' },
      hostKind: 'dom',
      retention: 'keep-alive',
      payload: { workspaceId: 'workspace-1' },
    })).toBe('')
  })

  it('broadcasts an open request with a trimmed workspace id', () => {
    const received: WorkspaceChangesOpenEventDetail[] = []
    const listener = (event: Event) => {
      received.push((event as CustomEvent<WorkspaceChangesOpenEventDetail>).detail)
    }
    window.addEventListener(WORKSPACE_CHANGES_OPEN_EVENT, listener)
    try {
      expect(requestWorkspaceChangesOpen({
        workspaceId: ' workspace-1 ',
        workspaceName: 'Project A',
      })).toBe(true)
    } finally {
      window.removeEventListener(WORKSPACE_CHANGES_OPEN_EVENT, listener)
    }

    expect(received).toEqual([{ workspaceId: 'workspace-1', workspaceName: 'Project A' }])
  })

  it('does not broadcast without a workspace', () => {
    const listener = vi.fn()
    window.addEventListener(WORKSPACE_CHANGES_OPEN_EVENT, listener)
    try {
      expect(requestWorkspaceChangesOpen({ workspaceId: '  ', workspaceName: '' })).toBe(false)
    } finally {
      window.removeEventListener(WORKSPACE_CHANGES_OPEN_EVENT, listener)
    }

    expect(listener).not.toHaveBeenCalled()
  })
})
