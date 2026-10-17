import { describe, expect, it } from 'vitest'
import {
  createDraftProjectHydrationGuard,
  applySessionProjectWorkspaceMetadata,
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
  it('hydrates deferred project metadata from cache and validates it without dropping the project chip', async () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    let finish!: (value: ActiveProjectWorkspaceSnapshot[]) => void
    const pending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation,
      metadata: { workspaceId: 'p1', projectWorkspace: null, projectWorkspaceDeferred: true },
      cachedWorkspace: project('p1', true),
      loadWorkspaces: () => new Promise(resolve => { finish = resolve }), isCurrent: () => true,
    })
    expect(state.status.value).toBe('ready')
    expect(state.activeWorkspace.value?.id).toBe('p1')
    finish([project('p1', false)])
    await pending
    expect(state.status.value).toBe('unavailable')
  })

  it('resolves deferred workspace metadata after a cold durable-session reload', async () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    await applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation,
      metadata: { workspaceId: 'p1', projectWorkspaceDeferred: true },
      loadWorkspaces: async () => [project('p1', true)], isCurrent: () => true,
    })
    expect(state.status.value).toBe('ready')
    expect(state.activeWorkspace.value?.id).toBe('p1')
  })

  it('rejects deferred workspace validation after a newer session metadata generation', async () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    let finish!: (value: ActiveProjectWorkspaceSnapshot[]) => void
    const pending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation,
      metadata: { workspaceId: 'p1', projectWorkspaceDeferred: true },
      loadWorkspaces: () => new Promise(resolve => { finish = resolve }), isCurrent: () => true,
    })
    const next = state.beginSessionResolution('session-b')
    state.applySessionSnapshot('session-b', next, { workspaceId: 'p2', projectWorkspace: project('p2', true) })
    finish([project('p1', true)])
    await pending
    expect(state.boundWorkspaceId.value).toBe('p2')
    expect(state.activeWorkspace.value?.id).toBe('p2')
  })

  it('ignores a deferred workspace validation failure from a previous connection', async () => {
    const state = useActiveProjectWorkspace()
    const generation = state.beginSessionResolution('session-a')
    let current = true
    let fail!: (cause: Error) => void
    const pending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation,
      metadata: { workspaceId: 'p1', projectWorkspaceDeferred: true }, cachedWorkspace: project('p1', true),
      loadWorkspaces: () => new Promise((_resolve, reject) => { fail = reject }), isCurrent: () => current,
    })
    current = false
    fail(new Error('old connection closed'))
    await pending
    expect(state.status.value).toBe('ready')
  })

  it('keeps same-key draft workspace identity while durable metadata refreshes', () => {
    const state = useActiveProjectWorkspace()
    state.beginProjectDraft(project('p1', true))
    const generation = state.beginSessionResolution('draft-key', true)
    expect(state.boundWorkspaceId.value).toBe('p1')
    expect(state.status.value).toBe('ready')
    expect(state.pendingWorkspaceId.value).toBeNull()
    expect(state.applySessionSnapshot('draft-key', generation, {
      workspaceId: 'p1', projectWorkspace: project('p1', true),
    })).toBe(true)
    state.beginSessionResolution('different-session')
    expect(state.boundWorkspaceId.value).toBeNull()
    expect(state.status.value).toBe('resolving')
  })

  it('invalidates an older draft-project hydration when navigation advances', () => {
    const guard = createDraftProjectHydrationGuard()
    const projectA = guard.begin()
    const projectB = guard.begin()

    expect(guard.isCurrent(projectA)).toBe(false)
    expect(guard.isCurrent(projectB)).toBe(true)
    guard.invalidate()
    expect(guard.isCurrent(projectB)).toBe(false)
  })

  it('synchronously aborts the old project request before a new generation starts', () => {
    const guard = createDraftProjectHydrationGuard()
    const projectA = guard.begin()
    const controllerA = guard.createController(projectA)
    expect(controllerA?.signal.aborted).toBe(false)

    const projectB = guard.begin()

    expect(controllerA?.signal.aborted).toBe(true)
    expect(guard.createController(projectA)).toBeNull()
    expect(guard.createController(projectB)?.signal.aborted).toBe(false)
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
