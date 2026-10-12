import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import { createV4SessionDirectory } from '@/adapters/gateway/sessionDirectoryV4'
import { createV4SessionLifecycle } from '@/adapters/gateway/sessionLifecycleV4'
import { useSessions } from '@/composables/useSessions'
import { createAppAutomaticRpc } from '@/utils/appAutomaticRpc'
import appSource from './App.vue?raw'

const key = 'agent:main:webchat:rename-regression'
const page = (title: string) => ({ count: 1, ts: 1, sessions: [{ key, title, updatedAt: 1 }] })
const acknowledgement = { key, updated: ['displayName'] }
const busy = () => Object.assign(new Error('Session storage is busy'), {
  code: 'STORAGE_BUSY', retryable: true, retry_after_ms: 100, accepted: false,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

// Execute App's actual handler with its real directory and automatic-read seams.
// This avoids mounting unrelated App services and does not copy the handler logic.
const handlerStart = appSource.indexOf('let sessionRenameSequence =')
const handlerEnd = appSource.indexOf('function removeLocalSessions', handlerStart)
if (handlerStart < 0 || handlerEnd < 0) throw new Error('App rename handler boundary changed')
const handlerSource = transpileModule(appSource.slice(handlerStart, handlerEnd), {
  compilerOptions: { target: ScriptTarget.ES2022 },
}).outputText

async function setup() {
  const request = vi.fn().mockResolvedValue(page('Previous name'))
  const sessions = useSessions(createV4SessionDirectory({ ready: async () => {}, request }))
  const automatic = createAppAutomaticRpc({
    available: () => true, admitted: () => true, resumeDirectory: async () => {},
    subscribeCron: () => {}, loadAgents: async () => {},
    loadSidebar: sessions.loadSessions, cancelSidebar: sessions.cancelPendingRequests,
  })
  const renameRequest = vi.fn().mockResolvedValue(acknowledgement)
  const lifecycle = createV4SessionLifecycle({ request: renameRequest })
  const overrides = ref<Record<string, string>>({})
  const local = ref<Record<string, { title: string }>>({ [key]: { title: 'Previous name' } })
  const toast = vi.fn()
  const rename = new Function(
    'sessionLifecycle', 'applyConfirmedTitle', 'renameOverrides', 'localChatSessions',
    'pushToast', 'errorMessage', 'loadSidebarData', `${handlerSource}; return onRenameSession`,
  )(lifecycle, sessions.applyConfirmedTitle, overrides, local, toast,
    (error: unknown) => String(error), automatic.load,
  ) as (request: { key: string; title: string }) => Promise<void>
  await automatic.mount()
  await vi.advanceTimersByTimeAsync(0)
  return { request, sessions, automatic, renameRequest, overrides, local, toast, rename }
}

describe('App session rename through title-read contention', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('retains an acknowledged rename through a failed refresh without masking later authoritative updates or deletion', async () => {
    const app = await setup()
    app.request.mockRejectedValueOnce(busy()).mockResolvedValueOnce(page('Renamed elsewhere'))
    await app.rename({ key, title: '  My confirmed name  ' })

    expect(app.renameRequest).toHaveBeenCalledWith('sessions.rename', {
      key, displayName: 'My confirmed name',
    }, undefined)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('My confirmed name')
    expect(app.local.value[key]?.title).toBe('My confirmed name')
    expect(app.overrides.value).toEqual({})
    expect(app.sessions.sessionListError.value).toBe(true)
    expect(app.toast).toHaveBeenCalledExactlyOnceWith('Session renamed', { tone: 'ok' })

    await vi.advanceTimersByTimeAsync(500)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Renamed elsewhere')
    expect(app.sessions.sessionListError.value).toBe(false)
    app.request.mockResolvedValueOnce({ count: 0, ts: 2, sessions: [] })
    app.automatic.schedule()
    await vi.advanceTimersByTimeAsync(150)
    expect(app.sessions.sessionsList.value).toEqual([])
    expect(app.overrides.value).toEqual({})
    app.automatic.dispose()
  })

  it('does not cache an unconfirmed name when rename and the following directory read both fail', async () => {
    const app = await setup()
    app.renameRequest.mockRejectedValueOnce(new Error('Rename denied'))
    app.request.mockRejectedValueOnce(busy())
    await app.rename({ key, title: 'Not confirmed' })

    expect(app.sessions.sessionsList.value[0]?.title).toBe('Previous name')
    expect(app.local.value[key]?.title).toBe('Previous name')
    expect(app.overrides.value).toEqual({})
    expect(app.toast).toHaveBeenCalledExactlyOnceWith('Failed to rename session', { tone: 'danger' })
    app.automatic.dispose()
  })

  it('does not let an older acknowledgement clear a newer pending rename', async () => {
    const app = await setup()
    const older = deferred<typeof acknowledgement>()
    const newer = deferred<typeof acknowledgement>()
    app.renameRequest.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const oldWork = app.rename({ key, title: 'Older name' })
    const newWork = app.rename({ key, title: 'Newer name' })
    older.resolve(acknowledgement)
    await oldWork
    expect(app.overrides.value).toEqual({ [key]: 'Newer name' })
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Previous name')
    expect(app.local.value[key]?.title).toBe('Previous name')
    expect(app.toast).not.toHaveBeenCalled()
    app.request.mockRejectedValueOnce(busy())
    newer.resolve(acknowledgement)
    await newWork
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Newer name')
    expect(app.overrides.value).toEqual({})
    app.automatic.dispose()
  })

  it.each(['success', 'failure'])('ignores an older rename %s arriving after a newer confirmed name', async outcome => {
    const app = await setup()
    const older = deferred<typeof acknowledgement>()
    app.renameRequest.mockReturnValueOnce(older.promise).mockResolvedValueOnce(acknowledgement)
    const oldWork = app.rename({ key, title: 'Older name' })
    app.request.mockRejectedValueOnce(busy())
    await app.rename({ key, title: 'Newer confirmed name' })
    if (outcome === 'success') older.resolve(acknowledgement)
    else older.reject(new Error('Old failure'))
    await oldWork
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Newer confirmed name')
    expect(app.local.value[key]?.title).toBe('Newer confirmed name')
    expect(app.overrides.value).toEqual({})
    expect(app.toast).toHaveBeenCalledExactlyOnceWith('Session renamed', { tone: 'ok' })
    app.automatic.dispose()
  })

  it('fences a directory response started before the rename while retaining automatic retry', async () => {
    const app = await setup()
    const oldRead = deferred<ReturnType<typeof page>>()
    app.request.mockReturnValueOnce(oldRead.promise)
      .mockRejectedValueOnce(busy()).mockResolvedValueOnce(page('Confirmed name'))
    const oldRefresh = app.automatic.load()
    await vi.advanceTimersByTimeAsync(0)
    const oldSignal = app.request.mock.calls[1]?.[2].signal as AbortSignal
    const rename = app.rename({ key, title: 'Confirmed name' })
    await vi.advanceTimersByTimeAsync(0)
    expect(oldSignal.aborted).toBe(true)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Confirmed name')
    oldRead.resolve(page('Stale previous name'))
    await Promise.all([oldRefresh, rename])
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Confirmed name')
    expect(app.sessions.sessionListError.value).toBe(true)
    await vi.advanceTimersByTimeAsync(500)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Confirmed name')
    expect(app.sessions.sessionListError.value).toBe(false)
    expect(app.request).toHaveBeenCalledTimes(4)
    app.automatic.dispose()
  })
})
