import { afterEach, describe, expect, it, vi } from 'vitest'
import type { NativeWorkbenchApi, NativeWorkbenchSurfaceResult, Platform } from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import { WorkbenchPanelRegistry, WorkbenchRuntimeManager } from '@/workbench/runtime'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'
import { createBrowserThumbnail } from './browserThumbnail'

vi.mock('./browserThumbnail', () => ({ createBrowserThumbnail: vi.fn(async () => 'blob:browser-thumbnail') }))
afterEach(() => { vi.restoreAllMocks() })

function deferred() {
  let resolve!: (result: NativeWorkbenchSurfaceResult) => void
  const promise = new Promise<NativeWorkbenchSurfaceResult>(done => { resolve = done })
  return { promise, resolve }
}

async function harness(navigateSurface: NonNullable<NativeWorkbenchApi['navigateSurface']>, overrides: Partial<NativeWorkbenchApi> = {}) {
  const success = async () => ({ ok: true })
  const api: NativeWorkbenchApi = {
    getCapabilities: async () => ({ protocolVersions: [1, 2], modes: ['full'] }),
    createSurface: vi.fn(success), setSurfaceRect: vi.fn(success), activateSurface: vi.fn(success),
    destroySurface: vi.fn(success), onSurfaceEvent: () => () => undefined, navigateSurface,
    ...overrides,
  }
  const registry = new WorkbenchPanelRegistry()
  registry.register(createBrowserWorkbenchDefinition({ platform: {} as Platform, t: key => key,
    confirmPermission: async () => false, openExternal: vi.fn() }))
  const reportError = vi.fn()
  const manager = new WorkbenchRuntimeManager(registry, { nativeWorkbenchApi: api, onError: reportError })
  const item = createBrowserWorkbenchItem({ scopeId: 'refresh-test', url: 'https://example.test/' })!
  manager.handle({ type: 'open', item })
  manager.handle({ type: 'resume', item })
  await manager.flush()
  manager.handleSurfaceRect({ itemId: item.id, x: 0, y: 0, width: 600, height: 400, visible: true })
  manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
  await manager.flush()
  const send = (action: string) => manager.handleComponentEvent(item, { type: 'browser-action', payload: { action } })
  return { api, manager, item, reportError, send, state: () => manager.getRenderState(item.id) }
}

describe('browser navigation feedback without blocking lifecycle events', () => {
  it.each(['immediate', 'after-disposal'] as const)(
    'preserves a same-id reopened page when old recovery settles (%s)',
    async timing => {
      const recovery = deferred()
      const createSurface = vi.fn()
        .mockResolvedValueOnce({ ok: false, message: 'Initial page unavailable' })
        .mockReturnValueOnce(recovery.promise)
        .mockResolvedValue({ ok: true })
      const { manager, item, send, state, api, reportError } = await harness(
        async () => ({ ok: true }), { createSurface },
      )
      reportError.mockClear()
      send('reload')
      await vi.waitFor(() => expect(createSurface).toHaveBeenCalledTimes(2))
      manager.handle({ type: 'dispose', item, reason: 'closed' })
      if (timing === 'after-disposal') {
        // flush() also waits for the deliberately pending recovery. Observe
        // disposal alone to cover its removal of the old epoch bookkeeping.
        await vi.waitFor(() => expect(state()).toEqual({}))
      }
      const reopened = { ...item, title: 'Reopened page' }
      manager.handle({ type: 'open', item: reopened })
      manager.handle({ type: 'resume', item: reopened })
      await vi.waitFor(() => expect(createSurface).toHaveBeenCalledTimes(3))
      manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id,
        type: 'navigation-state', detail: { loading: false,
          url: 'https://example.test/reopened', title: 'Reopened page' } })
      await vi.waitFor(() => expect(state().pageTitle).toBe('Reopened page'))
      const destroysBeforeOldResult = vi.mocked(api.destroySurface).mock.calls.length
      recovery.resolve({ ok: true })
      await manager.flush()
      expect(api.destroySurface).toHaveBeenCalledTimes(destroysBeforeOldResult)
      expect(manager.hasRuntime(item.id)).toBe(true)
      expect(state()).toMatchObject({ loading: false, errorMessage: '',
        pageTitle: 'Reopened page', currentUrl: 'https://example.test/reopened' })
      expect(reportError).not.toHaveBeenCalled()
      await manager.disposeAll()
    },
  )

  it('keeps navigation events received before a recovered surface finishes creation', async () => {
    const recovery = deferred()
    const createSurface = vi.fn()
      .mockResolvedValueOnce({ ok: false, message: 'Initial page unavailable' })
      .mockReturnValueOnce(recovery.promise)
    const { manager, item, send, state } = await harness(async () => ({ ok: true }), { createSurface })
    send('reload')
    await vi.waitFor(() => expect(createSurface).toHaveBeenCalledTimes(2))
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'navigation-state',
      detail: { loading: false, url: 'https://example.test/recovered', title: 'Recovered page' } })
    // Let the independent event queue drain while createSurface is still pending.
    await new Promise(resolve => setTimeout(resolve, 0))
    recovery.resolve({ ok: true })
    await manager.flush()
    expect(state()).toMatchObject({ loading: false, errorMessage: '',
      currentUrl: 'https://example.test/recovered', pageTitle: 'Recovered page' })
    await manager.disposeAll()
  })

  it.each(['stop', 'close'] as const)('fences a recreated surface when the user selects %s during recovery', async action => {
    const retry = deferred()
    const createSurface = vi.fn()
      .mockResolvedValueOnce({ ok: false, message: 'Initial page unavailable' })
      .mockReturnValueOnce(retry.promise)
      .mockResolvedValue({ ok: true })
    const navigate = vi.fn(async () => ({ ok: false, code: 'TARGET_NOT_FOUND', message: 'Surface is still being created' }))
    const { manager, item, send, state, api } = await harness(navigate, { createSurface })
    expect(state().errorMessage).toBe('Initial page unavailable')
    send('reload')
    await vi.waitFor(() => expect(createSurface).toHaveBeenCalledTimes(2))
    send('reload')
    if (action === 'close') manager.handle({ type: 'dispose', item, reason: 'closed' })
    else {
      send('stop')
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
      expect(state()).toMatchObject({ loading: false, errorMessage: '' })
    }
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'navigation-state',
      detail: { loading: true, url: 'https://example.test/cancelled', title: 'Late cancelled page' } })
    await new Promise(resolve => setTimeout(resolve, 0))
    retry.resolve({ ok: true })
    await manager.flush()
    expect(api.activateSurface).not.toHaveBeenCalled()
    expect(createSurface).toHaveBeenCalledTimes(2)
    if (action === 'close') expect(state()).toEqual({})
    else {
      expect(state()).toMatchObject({ loading: false, errorMessage: '' })
      send('reload')
      await manager.flush()
      expect(createSurface).toHaveBeenCalledTimes(3)
      expect(state().errorMessage).toBe('')
    }
    await manager.disposeAll()
  })

  it('shows loading immediately, receives native progress and stops before reload completes', async () => {
    const reload = deferred()
    const navigate = vi.fn(async request => request.action === 'reload' ? reload.promise : { ok: true })
    const { manager, item, send, state, api, reportError } = await harness(navigate)
    send('reload')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    expect(state().loading).toBe(true)
    expect(state().errorMessage).toBe('')
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'navigation-state',
      detail: { loading: true, url: 'https://example.test/', title: 'Still loading' } })
    await vi.waitFor(() => expect(state().pageTitle).toBe('Still loading'))
    send('stop')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(2))
    expect(navigate).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'stop' }))
    expect(state().loading).toBe(false)
    reload.resolve({ ok: false, message: 'Superseded navigation' })
    await manager.flush()
    expect(state()).toMatchObject({ loading: false, errorMessage: '' })
    expect(api.destroySurface).not.toHaveBeenCalled()
    expect(reportError).not.toHaveBeenCalled()
    await manager.disposeAll()
  })

  it('ignores an older reload result while a newer reload is pending', async () => {
    const first = deferred()
    const second = deferred()
    const navigate = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { manager, item, state, reportError } = await harness(navigate)
    manager.performAction(item, 'reload')
    manager.performAction(item, 'reload')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(2))
    first.resolve({ ok: false, navigationError: { url: 'https://example.test/', code: 'ERR_ABORTED', message: 'Old failure' } })
    await Promise.resolve()
    await Promise.resolve()
    expect(state()).toMatchObject({ loading: true, errorMessage: '' })
    let flushed = false
    const flush = manager.flush().then(() => { flushed = true })
    await Promise.resolve()
    expect(flushed).toBe(false)
    second.resolve({ ok: true })
    await flush
    expect(state()).toMatchObject({ loading: false, errorMessage: '' })
    expect(reportError).not.toHaveBeenCalled()
    await manager.disposeAll()
  })

  it('restores the submitted address after stop without reviving cancelled loading or errors', async () => {
    const navigation = deferred()
    const navigate = vi.fn(async request => request.action === 'navigate' ? navigation.promise : { ok: true })
    const { manager, item, send, state } = await harness(navigate)
    manager.handleComponentEvent(item, { type: 'browser-action', payload: {
      action: 'navigate', url: 'https://example.test/pending', addressRequestId: 'browser-address-stop-test',
    } })
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    send('stop')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(2))
    navigation.resolve({ ok: false, code: 'NAVIGATION_CANCELLED', message: 'Stopped by user' })
    await manager.flush()
    expect(state()).toMatchObject({ currentUrl: 'https://example.test/', loading: false, errorMessage: '',
      navigationAddressSequence: 1, navigationAddressRequestId: 'browser-address-stop-test' })
    await manager.disposeAll()
  })

  it('suspends a pending page while continuing to receive its actual loading state', async () => {
    const reload = deferred()
    const navigate = vi.fn(() => reload.promise)
    const { manager, item, send, state, api } = await harness(navigate)
    send('reload')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    manager.handle({ type: 'suspend', item })
    await vi.waitFor(() => expect(api.setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ visible: false }),
    ))
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'navigation-state',
      detail: { loading: true, url: 'https://example.test/', title: 'Background page' } })
    await vi.waitFor(() => expect(state().pageTitle).toBe('Background page'))
    expect(state().loading).toBe(true)
    reload.resolve({ ok: true })
    // The command can finish while the native document is still loading
    // subresources. Its most recent loading event remains authoritative.
    await Promise.resolve()
    await Promise.resolve()
    expect(state().loading).toBe(true)
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'navigation-state',
      detail: { loading: false, url: 'https://example.test/', title: 'Background page' } })
    await manager.flush()
    expect(state().loading).toBe(false)
    expect(api.destroySurface).not.toHaveBeenCalled()
    await manager.disposeAll()
  })

  it('closes a tab during reload and discards its late response and events', async () => {
    const reload = deferred()
    const navigate = vi.fn(() => reload.promise)
    const { manager, item, send, state, api, reportError } = await harness(navigate)
    send('reload')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    manager.handle({ type: 'dispose', item, reason: 'closed' })
    await vi.waitFor(() => expect(api.destroySurface).toHaveBeenCalledOnce())
    reload.resolve({ ok: false, code: 'BROWSER_CRASHED', message: 'Late result' })
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
    await manager.flush()
    expect(state()).toEqual({})
    expect(manager.hasRuntime(item.id)).toBe(false)
    expect(reportError).not.toHaveBeenCalled()
  })

  it('destroys a page before awaiting its navigation during manager disposal', async () => {
    const reload = deferred()
    const navigate = vi.fn(() => reload.promise)
    const { manager, send, api } = await harness(navigate)
    send('reload')
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    vi.mocked(api.destroySurface).mockImplementation(async () => {
      reload.resolve({ ok: false, message: 'Navigation destroyed' })
      return { ok: true }
    })
    await manager.disposeAll()
    expect(api.destroySurface).toHaveBeenCalledOnce()
    await manager.flush()
  })

  it('caches one preview per loaded page and retains only the backdrop until its replacement is ready', async () => {
    const capture = vi.fn(async () => ({ targetRef: 'page-1', mimeType: 'image/png' as const,
      dataBase64: 'QQ==', width: 800, height: 600 }))
    const target = vi.fn(async () => ({ targetRef: 'page-1', surfaceId: '', url: 'https://example.test/',
      title: 'Example', sessionKey: 'refresh-test' }))
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const { manager, item, send, state } = await harness(async () => ({ ok: true }), {
      captureWorkbenchScreenshot: capture, getWorkbenchBrowserTarget: target,
    })
    await vi.waitFor(() => expect(state().thumbnailUrl).toBe('blob:browser-thumbnail'))
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
    await manager.flush()
    expect(capture).toHaveBeenCalledOnce()
    manager.handle({ type: 'suspend', item })
    await manager.flush()
    send('reload')
    await manager.flush()
    expect(state().thumbnailUrl).toBe('')
    expect(state().snapshotUrl).toBe('blob:browser-thumbnail')
    expect(revoke).not.toHaveBeenCalled()
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
    await manager.flush()
    expect(capture).toHaveBeenCalledOnce()
    manager.handle({ type: 'resume', item })
    await manager.flush()
    await vi.waitFor(() => expect(state().thumbnailUrl).toBe('blob:browser-thumbnail'))
    expect(capture).toHaveBeenCalledTimes(2)
    expect(revoke).toHaveBeenCalledOnce()
    await manager.disposeAll()
    expect(revoke).toHaveBeenCalledTimes(2)
  })

  it('discards and releases a preview which finishes after its tab closes', async () => {
    let finish!: (url: string) => void
    vi.mocked(createBrowserThumbnail).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const { manager, item, state } = await harness(async () => ({ ok: true }), {
      captureWorkbenchScreenshot: async () => ({ targetRef: 'page-1', mimeType: 'image/png',
        dataBase64: 'QQ==', width: 800, height: 600 }),
      getWorkbenchBrowserTarget: async () => ({ targetRef: 'page-1', surfaceId: '',
        url: 'https://example.test/', title: '', sessionKey: 'refresh-test' }),
    })
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    manager.handle({ type: 'dispose', item, reason: 'closed' })
    await manager.flush()
    finish('blob:late-thumbnail')
    await vi.waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:late-thumbnail'))
    expect(state()).toEqual({})
  })

  it('captures a new ready page after its superseded capture settles', async () => {
    const screenshot = { targetRef: 'page-1', mimeType: 'image/png' as const,
      dataBase64: 'QQ==', width: 800, height: 600 }
    let finishOld!: (value: typeof screenshot) => void
    const capture = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
      .mockResolvedValue(screenshot)
    const { manager, item, send, state } = await harness(async () => ({ ok: true }), {
      captureWorkbenchScreenshot: capture,
      getWorkbenchBrowserTarget: async () => ({ targetRef: 'page-1', surfaceId: '',
        url: 'https://example.test/', title: '', sessionKey: 'refresh-test' }),
    })
    await vi.waitFor(() => expect(capture).toHaveBeenCalledOnce())
    send('reload')
    await manager.flush()
    manager.handleNativeSurfaceEvent({ version: 2, surfaceId: item.id, type: 'ready' })
    await manager.flush()
    expect(state().thumbnailUrl).toBe('')
    finishOld(screenshot)
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(state().thumbnailUrl).toBe('blob:browser-thumbnail'))
    await manager.disposeAll()
  })
})
