// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, reactive, shallowRef, type App, type ShallowRef } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS, SVG_CLIPBOARD_MAX_BYTES } from '@/utils/imageClipboard'
import { useImageClipboard, type ClipboardImageSource } from './useImageClipboard'

const mocks = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: mocks.toast }) }))

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"><text>你好🙂</text></svg>'
let app: App | undefined
let copy: ReturnType<typeof useImageClipboard>
let state: { session: string; epoch: number }
let source: ShallowRef<ClipboardImageSource | null>
let write: ReturnType<typeof vi.fn>
let fetchArtifact: ReturnType<typeof vi.fn>
let fetchAttachment: ReturnType<typeof vi.fn>
let accepted: Blob[]

class TestClipboardItem {
  constructor(readonly entries: Record<string, Promise<Blob>>) {}
}

function mount(options: { loadBlob?: (signal: AbortSignal, maxBytes: number) => Promise<Blob>; workbench?: boolean } = {}) {
  state = reactive({ session: 'fixture-session', epoch: 1 })
  source = shallowRef<ClipboardImageSource | null>({ kind: 'artifact', artifact: {
    id: 'fixture-svg', name: 'drawing.svg', mime: 'image/svg+xml',
  } })
  const host = document.createElement('div')
  document.body.append(host)
  app = createApp({ setup() {
    copy = useImageClipboard({ source: () => source.value, sessionKey: () => state.session, loadBlob: options.loadBlob })
    return () => h('button')
  } })
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, { get subscriptionEpoch() { return state.epoch } } as GatewayAccess)
  if (options.workbench !== false) app.provide(ARTIFACT_WORKBENCH_KEY,
    { content: { fetchArtifact, fetchAttachment } } as unknown as ArtifactWorkbench)
  app.mount(host)
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  accepted = []
  fetchArtifact = vi.fn(async () => ({ ok: true, blob: new Blob([SVG], { type: 'image/svg+xml' }) }))
  fetchAttachment = vi.fn(async () => ({ ok: true, blob: new Blob([SVG], { type: 'image/svg+xml' }) }))
  write = vi.fn(async (items: TestClipboardItem[]) => {
    const values = await Promise.all(Object.values(items[0]!.entries))
    accepted.push(...values)
  })
  vi.stubGlobal('ClipboardItem', TestClipboardItem)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write } })
  mocks.toast.mockReset()
})

afterEach(() => {
  app?.unmount()
  app = undefined
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('useImageClipboard', () => {
  it('starts clipboard write before fetching full authenticated content and copies exact source', async () => {
    mount()
    const pending = copy.copy('svg-source')
    expect(write).toHaveBeenCalledOnce()
    expect(fetchArtifact).not.toHaveBeenCalled()
    expect(copy.busy.value).toBe(true)
    expect(await pending).toBe(true)
    expect(fetchArtifact).toHaveBeenCalledWith(source.value!.kind === 'artifact' ? source.value!.artifact : null,
      { sessionKey: state.session, signal: expect.any(AbortSignal), maxBytes: SVG_CLIPBOARD_MAX_BYTES })
    expect(await accepted[0]!.text()).toBe(SVG)
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('imageClipboard.copiedSource'), { tone: 'ok' })
    expect(copy.busy.value).toBe(false)
  })

  it('routes attachment bytes through the attachment transport', async () => {
    mount()
    source.value = { kind: 'attachment', attachment: {
      kind: 'inline', displayId: 'fixture-attachment', renderKey: 'fixture-attachment', name: 'drawing.svg', mime: 'image/svg+xml',
      downloadData: 'c3Zn',
    } }
    expect(await copy.copy('svg-source')).toBe(true)
    expect(fetchAttachment).toHaveBeenCalledWith(source.value.attachment,
      expect.objectContaining({ sessionKey: state.session, maxBytes: SVG_CLIPBOARD_MAX_BYTES }))
    expect(fetchArtifact).not.toHaveBeenCalled()
  })

  it('supports a bounded workspace loader without requiring workbench injection', async () => {
    const loadBlob = vi.fn(async () => new Blob([SVG], { type: 'text/plain' }))
    mount({ loadBlob, workbench: false })
    expect(await copy.copy('svg-source')).toBe(true)
    expect(loadBlob).toHaveBeenCalledWith(expect.any(AbortSignal), SVG_CLIPBOARD_MAX_BYTES)
    expect(fetchArtifact).not.toHaveBeenCalled()
  })

  it.each(['session', 'epoch', 'source', 'cancel', 'unmount'] as const)('cancels delayed preparation after %s changes', async change => {
    let finish!: (result: { ok: true; blob: Blob }) => void
    fetchArtifact.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    mount()
    const pending = copy.copy('svg-source')
    await Promise.resolve()
    expect(fetchArtifact).toHaveBeenCalledOnce()
    const signal = fetchArtifact.mock.calls[0]![1].signal as AbortSignal
    if (change === 'session') state.session = 'another-fixture-session'
    if (change === 'epoch') state.epoch++
    if (change === 'source') source.value = { kind: 'artifact', artifact: { id: 'other', name: 'other.svg', mime: 'image/svg+xml' } }
    if (change === 'cancel') copy.cancel()
    if (change === 'unmount') { app!.unmount(); app = undefined }
    expect(signal.aborted).toBe(true)
    expect(await pending).toBe(false)
    finish({ ok: true, blob: new Blob([SVG], { type: 'image/svg+xml' }) })
    await Promise.resolve()
    expect(accepted).toEqual([])
    expect(mocks.toast).not.toHaveBeenCalled()
    expect(copy.busy.value).toBe(false)
  })

  it('keeps copying through equivalent wrapper replacements but cancels changed content with the same id', async () => {
    let finish!: (result: { ok: true; blob: Blob }) => void
    fetchArtifact.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    mount()
    const first = copy.copy('svg-source')
    await Promise.resolve()
    const original = source.value!
    if (original.kind !== 'artifact') throw new Error('Expected fixture artifact')
    source.value = { kind: 'artifact', artifact: { ...original.artifact } }
    expect(copy.busy.value).toBe(true)
    expect(fetchArtifact.mock.calls[0]![1].signal.aborted).toBe(false)
    finish({ ok: true, blob: new Blob([SVG], { type: 'image/svg+xml' }) })
    expect(await first).toBe(true)
    const second = copy.copy('svg-source')
    await Promise.resolve()
    source.value = { kind: 'artifact', artifact: { ...original.artifact, sha256: 'changed-fixture-sha' } }
    expect(await second).toBe(false)
    expect(fetchArtifact.mock.calls[1]![1].signal.aborted).toBe(true)
  })

  it('ignores a second click while preparing and clears the timer after completion', async () => {
    vi.useFakeTimers()
    let finish!: (result: { ok: true; blob: Blob }) => void
    fetchArtifact.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    mount()
    const first = copy.copy('svg-source')
    expect(await copy.copy('svg-source')).toBe(false)
    expect(write).toHaveBeenCalledOnce()
    finish({ ok: true, blob: new Blob([SVG], { type: 'image/svg+xml' }) })
    expect(await first).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('times out a loader that ignores cancellation without leaving the clipboard operation pending', async () => {
    vi.useFakeTimers()
    fetchArtifact.mockImplementation(() => new Promise(() => {}))
    mount()
    const pending = copy.copy('svg-source')
    await vi.advanceTimersByTimeAsync(IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS)
    expect(await pending).toBe(false)
    expect(accepted).toEqual([])
    expect(copy.busy.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('imageClipboard.timedOut'), { tone: 'danger' })
  })

  it('recovers the UI when the platform write remains pending after preparation', async () => {
    vi.useFakeTimers()
    write.mockImplementation(async (items: TestClipboardItem[]) => {
      await Promise.all(Object.values(items[0]!.entries))
      return new Promise(() => {})
    })
    mount()
    const pending = copy.copy('svg-source')
    await vi.advanceTimersByTimeAsync(IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS)
    expect(await pending).toBe(false)
    expect(copy.busy.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    expect(mocks.toast).toHaveBeenCalledExactlyOnceWith(i18n.global.t('imageClipboard.timedOut'), { tone: 'danger' })
  })

  it('maps bounded-transport failures to the size feedback', async () => {
    fetchArtifact.mockResolvedValue({ ok: false, errorCode: 'too_large', message: 'synthetic' })
    mount()
    expect(await copy.copy('svg-source')).toBe(false)
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('imageClipboard.tooLarge'), { tone: 'danger' })
    expect(accepted).toEqual([])
  })

  it('reports unsupported clipboard without fetching any content', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {} })
    mount()
    expect(await copy.copy('svg-source')).toBe(false)
    expect(fetchArtifact).not.toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('imageClipboard.unsupported'), { tone: 'danger' })
  })

  it('aborts pending preparation on clipboard denial and never reports success', async () => {
    write.mockRejectedValue(new DOMException('Permission denied', 'NotAllowedError'))
    fetchArtifact.mockImplementation(() => new Promise(() => {}))
    mount()
    expect(await copy.copy('svg-source')).toBe(false)
    expect(fetchArtifact.mock.calls[0]?.[1].signal.aborted).toBe(true)
    expect(mocks.toast).toHaveBeenCalledExactlyOnceWith(i18n.global.t('imageClipboard.failed'), { tone: 'danger' })
    expect(copy.busy.value).toBe(false)
  })
})
