import { describe, expect, it } from 'vitest'
import {
  createDraftProjectHydrationGuard,
  useActiveProjectWorkspace,
  type ActiveProjectWorkspaceSnapshot,
} from './useActiveProjectWorkspace'

function project(
  id: string,
  available: boolean,
): ActiveProjectWorkspaceSnapshot {
  return {
    id,
    name: `Project ${id}`,
    path: `/repos/${id}`,
    available,
    removed: false,
  }
}

describe('useActiveProjectWorkspace', () => {
  it('invalidates an older draft-project hydration when navigation advances', () => {
    const guard = createDraftProjectHydrationGuard()
    const projectA = guard.begin()
    const projectB = guard.begin()

    expect(guard.isCurrent(projectA)).toBe(false)
    expect(guard.isCurrent(projectB)).toBe(true)
    guard.invalidate()
    expect(guard.isCurrent(projectB)).toBe(false)
  })

  it('clears pending binding after acceptance but keeps the active snapshot', () => {
    const state = useActiveProjectWorkspace()
    state.beginProjectDraft(project('p1', true))

    expect(state.pendingWorkspaceId.value).toBe('p1')
    state.acceptPendingBinding('p1')

    expect(state.pendingWorkspaceId.value).toBeNull()
    expect(state.activeWorkspace.value?.id).toBe('p1')
    expect(state.status.value).toBe('ready')
  })

  it('blocks resolving, unavailable, removed, unknown, and failed states', () => {
    const resolving = useActiveProjectWorkspace()
    resolving.beginSessionResolution('session-a')
    expect(resolving.status.value).toBe('resolving')
    expect(resolving.sendBlockedReason.value).toBe('resolving')

    const unavailable = useActiveProjectWorkspace()
    unavailable.beginProjectDraft(project('p2', false))
    expect(unavailable.status.value).toBe('unavailable')
    expect(unavailable.sendBlockedReason.value).toBe('unavailable')

    const removed = useActiveProjectWorkspace()
    removed.beginProjectDraft({ ...project('p3', false), removed: true })
    expect(removed.status.value).toBe('removed')
    expect(removed.sendBlockedReason.value).toBe('removed')

    const failed = useActiveProjectWorkspace()
    const generation = failed.beginSessionResolution('session-b')
    failed.failSessionResolution('session-b', generation)
    expect(failed.status.value).toBe('error')
    expect(failed.sendBlockedReason.value).toBe('error')

    const unknown = useActiveProjectWorkspace()
    const unknownGeneration = unknown.beginSessionResolution('session-c')
    unknown.applySessionSnapshot('session-c', unknownGeneration, {
      workspaceId: 'missing-project-row',
      projectWorkspace: null,
    })
    expect(unknown.status.value).toBe('unknown')
    expect(unknown.sendBlockedReason.value).toBe('unknown')
  })

  it('rejects a stale session snapshot after switching sessions', () => {
    const state = useActiveProjectWorkspace()
    const generationA = state.beginSessionResolution('session-a')
    const generationB = state.beginSessionResolution('session-b')

    expect(state.applySessionSnapshot('session-a', generationA, {
      workspaceId: 'p-a',
      projectWorkspace: project('p-a', true),
    })).toBe(false)
    expect(state.applySessionSnapshot('session-b', generationB, {
      workspaceId: 'p-b',
      projectWorkspace: project('p-b', true),
    })).toBe(true)
    expect(state.activeWorkspace.value?.id).toBe('p-b')
  })

  it('recovers an unavailable active project after a workspace refresh', () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', generation, {
      workspaceId: 'p1',
      projectWorkspace: project('p1', false),
    })

    state.applyWorkspaceRefresh(project('p1', true))

    expect(state.status.value).toBe('ready')
    expect(state.sendBlockedReason.value).toBeNull()
  })

  it('marks a durable project removed without discarding its visible snapshot', () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', generation, {
      workspaceId: 'p1',
      projectWorkspace: project('p1', true),
    })

    state.applyWorkspaceRefresh(null)

    expect(state.status.value).toBe('removed')
    expect(state.activeWorkspace.value).toMatchObject({
      id: 'p1',
      removed: true,
      available: false,
    })
  })

  it('keeps a missing draft binding visible to the send guard', () => {
    const state = useActiveProjectWorkspace()

    state.beginUnknownProjectDraft('missing-project')

    expect(state.pendingWorkspaceId.value).toBe('missing-project')
    expect(state.boundWorkspaceId.value).toBe('missing-project')
    expect(state.activeWorkspace.value).toBeNull()
    expect(state.status.value).toBe('unknown')
    expect(state.sendBlockedReason.value).toBe('unknown')
  })

  it('marks a refresh failure without discarding a pending draft binding', () => {
    const state = useActiveProjectWorkspace()
    state.beginProjectDraft(project('p1', true))

    state.failWorkspaceRefresh()

    expect(state.pendingWorkspaceId.value).toBe('p1')
    expect(state.boundWorkspaceId.value).toBe('p1')
    expect(state.activeWorkspace.value?.id).toBe('p1')
    expect(state.status.value).toBe('error')
  })
})
