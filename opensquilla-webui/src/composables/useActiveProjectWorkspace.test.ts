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
  it.each([true, false])('preserves settled same-session presentation while blocking refresh operations (project=%s)', hasProject => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, hasProject
      ? { workspaceId: 'p1', projectWorkspace: project('p1', true) } : {})
    const previousWorkspace = state.activeWorkspace.value

    const first = state.beginSessionRefresh('session-a')
    const second = state.beginSessionRefresh('session-a')

    expect(second).toBeGreaterThan(first)
    expect(state.refreshing.value).toBe(true)
    expect(state.boundWorkspaceId.value).toBe(hasProject ? 'p1' : null)
    expect(state.activeWorkspace.value).toBe(previousWorkspace)
    expect(state.status.value).toBe('resolving')
    expect(state.sendBlockedReason.value).toBe('resolving')
    expect(state.applySessionSnapshot('session-a', first, {})).toBe(false)
    expect(state.failSessionResolution('session-a', first)).toBe(false)
    expect(state.refreshing.value).toBe(true)
    expect(state.applySessionSnapshot('session-a', second, hasProject
      ? { workspaceId: 'p1', projectWorkspace: project('p1', true) } : {})).toBe(true)
    expect(state.refreshing.value).toBe(false)
    expect(state.sendBlockedReason.value).toBeNull()
  })

  it('uses initial resolution for an unhydrated or different session and rejects the departed refresh', () => {
    const state = useActiveProjectWorkspace()
    state.beginSessionResolution('session-a')
    const initial = state.beginSessionRefresh('session-a')
    expect(state.refreshing.value).toBe(false)
    expect(state.status.value).toBe('resolving')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const refresh = state.beginSessionRefresh('session-a')

    const switched = state.beginSessionRefresh('session-b')

    expect(state.refreshing.value).toBe(false)
    expect(state.boundWorkspaceId.value).toBeNull()
    expect(state.activeWorkspace.value).toBeNull()
    expect(state.status.value).toBe('resolving')
    expect(state.applySessionSnapshot('session-a', refresh, {
      workspaceId: 'p1', projectWorkspace: project('p1', true),
    })).toBe(false)
    expect(state.failSessionResolution('session-a', refresh)).toBe(false)
    expect(state.applySessionSnapshot('session-b', switched, {})).toBe(true)
  })

  it.each([
    { metadata: { workspaceId: 'p2', projectWorkspace: project('p2', true) }, status: 'ready', id: 'p2' },
    { metadata: { workspaceId: 'p1', projectWorkspace: { ...project('p1', false), removed: true } }, status: 'removed', id: 'p1' },
    { metadata: {}, status: 'none', id: null },
  ])('applies refreshed binding and removal results: $status/$id', ({ metadata, status, id }) => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const refresh = state.beginSessionRefresh('session-a')

    state.applySessionSnapshot('session-a', refresh, metadata)

    expect(state.refreshing.value).toBe(false)
    expect(state.boundWorkspaceId.value).toBe(id)
    expect(state.activeWorkspace.value?.id ?? null).toBe(id)
    expect(state.status.value).toBe(status)
  })

  it('ends a failed session refresh without losing the visible binding or allowing operations', () => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const refresh = state.beginSessionRefresh('session-a')

    expect(state.failSessionResolution('session-a', refresh)).toBe(true)

    expect(state.refreshing.value).toBe(false)
    expect(state.boundWorkspaceId.value).toBe('p1')
    expect(state.activeWorkspace.value?.id).toBe('p1')
    expect(state.sendBlockedReason.value).toBe('error')
    state.applyWorkspaceRefresh(project('p1', true))
    expect(state.sendBlockedReason.value).toBe('error')
    const retry = state.beginSessionRefresh('session-a')
    expect(state.refreshing.value).toBe(true)
    expect(state.activeWorkspace.value?.id).toBe('p1')
    state.applySessionSnapshot('session-a', retry, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    expect(state.refreshing.value).toBe(false)
    expect(state.sendBlockedReason.value).toBeNull()
    state.applyWorkspaceRefresh(project('p1', false))
    expect(state.sendBlockedReason.value).toBe('unavailable')
  })

  it.each(['resolution', 'draft', 'unknown-draft', 'clear'] as const)('clears session refresh ownership on %s', reset => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const refresh = state.beginSessionRefresh('session-a')

    if (reset === 'resolution') state.beginSessionResolution('session-a')
    else if (reset === 'draft') state.beginProjectDraft(project('p2', true))
    else if (reset === 'unknown-draft') state.beginUnknownProjectDraft('p2')
    else state.clearDraft()

    expect(state.refreshing.value).toBe(false)
    expect(state.applySessionSnapshot('session-a', refresh, {})).toBe(false)
    expect(state.failSessionResolution('session-a', refresh)).toBe(false)
    if (reset === 'resolution') {
      expect(state.boundWorkspaceId.value).toBeNull()
      expect(state.status.value).toBe('resolving')
    }
  })

  it('keeps deferred background metadata blocked until workspace validation settles', async () => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const refresh = state.beginSessionRefresh('session-a')
    let finish!: (value: ActiveProjectWorkspaceSnapshot[]) => void

    const pending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation: refresh,
      metadata: { workspaceId: 'p1', projectWorkspaceDeferred: true },
      loadWorkspaces: () => new Promise(resolve => { finish = resolve }), isCurrent: () => true,
    })

    expect(state.activeWorkspace.value?.id).toBe('p1')
    expect(state.refreshing.value).toBe(true)
    expect(state.sendBlockedReason.value).toBe('resolving')
    // An independent project-list refresh cannot settle session metadata.
    state.applyWorkspaceRefresh(project('p1', true))
    expect(state.sendBlockedReason.value).toBe('resolving')
    state.applyWorkspaceRefresh(null)
    expect(state.activeWorkspace.value?.removed).toBe(true)
    expect(state.sendBlockedReason.value).toBe('resolving')
    state.failWorkspaceRefresh()
    expect(state.refreshing.value).toBe(true)
    expect(state.sendBlockedReason.value).toBe('resolving')

    finish([project('p1', false)])
    await pending
    expect(state.refreshing.value).toBe(false)
    expect(state.sendBlockedReason.value).toBe('unavailable')
  })

  it('keeps deferred refresh failures blocked and ignores superseded validation completions', async () => {
    const state = useActiveProjectWorkspace()
    const initial = state.beginSessionResolution('session-a')
    state.applySessionSnapshot('session-a', initial, { workspaceId: 'p1', projectWorkspace: project('p1', true) })
    const first = state.beginSessionRefresh('session-a')
    let finish!: (value: ActiveProjectWorkspaceSnapshot[]) => void
    const oldPending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation: first,
      metadata: { workspaceId: 'p1', projectWorkspaceDeferred: true },
      loadWorkspaces: () => new Promise(resolve => { finish = resolve }), isCurrent: () => true,
    })
    const second = state.beginSessionRefresh('session-a')
    let fail!: (cause: Error) => void
    const pending = applySessionProjectWorkspaceMetadata({
      state, sessionKey: 'session-a', generation: second,
      metadata: { workspaceId: 'p2', projectWorkspaceDeferred: true },
      cachedWorkspace: project('p2', true),
      loadWorkspaces: () => new Promise((_resolve, reject) => { fail = reject }), isCurrent: () => true,
    })

    finish([project('p1', true)])
    await oldPending
    expect(state.boundWorkspaceId.value).toBe('p2')
    expect(state.refreshing.value).toBe(true)
    expect(state.sendBlockedReason.value).toBe('resolving')
    fail(new Error('workspace validation failed'))
    await pending
    expect(state.refreshing.value).toBe(false)
    expect(state.boundWorkspaceId.value).toBe('p2')
    expect(state.sendBlockedReason.value).toBe('error')
  })

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
