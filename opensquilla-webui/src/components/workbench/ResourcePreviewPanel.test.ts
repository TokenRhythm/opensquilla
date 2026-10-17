// @vitest-environment happy-dom
import { createApp, h, nextTick, shallowRef } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import ResourcePreviewPanel from './ResourcePreviewPanel.vue'
import { resourcePreviewKind } from '@/workbench/resourcePreview'
import type { ResourcePreviewKind } from '@/workbench/resourcePreview'
vi.mock('./PdfResourcePreview.vue', async () => {
  const { h } = await import('vue')
  return { __esModule: true, default: { props: ['data', 'name'], setup: (props: { data: ArrayBuffer; name: string }) =>
    () => h('div', { 'data-testid': 'pdf-preview', 'data-bytes': props.data.byteLength }, props.name) } }
})
vi.mock('@tanstack/vue-virtual', async () => {
  const { computed } = await import('vue')
  return { useVirtualizer: (options: { value: { count: number } }) => computed(() => ({
    getVirtualItems: () => Array.from({ length: Math.min(30, options.value.count) }, (_, index) => ({ index, key: index, start: index * 30 })),
    getTotalSize: () => options.value.count * 30,
  })) }
})

const dispose: Array<() => void> = []
const testWindow = window as unknown as { happyDOM: { settings: { disableIframePageLoading: boolean } } }
beforeEach(() => {
  testWindow.happyDOM.settings.disableIframePageLoading = true
  let sequence = 0
  vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:preview-${++sequence}`)
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  vi.stubGlobal('Worker', undefined)
})
afterEach(() => { dispose.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); testWindow.happyDOM.settings.disableIframePageLoading = false })
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function sourceBlob(source: string) {
  const blob = new Blob([source])
  blob.text = async () => source
  blob.arrayBuffer = async () => new TextEncoder().encode(source).buffer
  blob.slice = (start = 0, end = source.length) => sourceBlob(source.slice(start, end))
  return blob
}
async function mountMutable(source: string | Blob, kind: ResourcePreviewKind, name: string) {
  const el = document.createElement('div'); document.body.appendChild(el)
  const props = shallowRef({ blob: typeof source === 'string' ? sourceBlob(source) : source, kind, name })
  const app = createApp({ render: () => h(ResourcePreviewPanel, props.value) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(el)
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount(); el.remove() } }
  dispose.push(unmount)
  await flush()
  return { el, unmount, async change(source: string | Blob, kind: ResourcePreviewKind, name: string) {
    props.value = { blob: typeof source === 'string' ? sourceBlob(source) : source, kind, name }
    await flush()
  } }
}
async function mount(source: string, kind: ResourcePreviewKind, name: string) { return (await mountMutable(source, kind, name)).el }
describe('authorized resource previews', () => {
  it('uses shared offline HTML isolation without same-origin or external network access', async () => {
    const el = await mount('<html><body>Hello<script>fetch("https://evil.test")</script></body></html>', 'html', 'demo.html')
    const frame = el.querySelector('iframe')!
    expect(frame.sandbox.toString()).toBe('allow-scripts')
    expect(frame.getAttribute('srcdoc')).toContain("connect-src 'none'")
    expect(frame.getAttribute('srcdoc')).toContain("base-uri 'none'")
    expect(frame.getAttribute('srcdoc')).toContain('Hello')
  })
  it('uses CSV quoting semantics and renders formula-like content as inert text', async () => {
    const el = await mount('name,value\r\n"a,b","line one\nline two"\r\n=HYPERLINK("x"),<img src=x onerror=alert(1)>', 'csv', 'data.csv')
    const table = el.querySelector('[role="table"]')!
    expect(table.getAttribute('aria-rowcount')).toBe('3')
    expect(table.textContent).toContain('a,b')
    expect(table.textContent).toContain('line one\nline two')
    expect(table.querySelector('img')).toBeNull()
  })
  it('recognizes existing Office renderer formats, media, and source fallbacks', () => {
    expect(resourcePreviewKind({ name: 'report.docx', mime: 'application/octet-stream' })).toBe('docx')
    expect(resourcePreviewKind({ name: 'clip.mp4', mime: 'video/mp4' })).toBe('video')
    expect(resourcePreviewKind({ name: 'index.ts', mime: 'text/plain', kind: 'text' })).toBe('source')
    expect(resourcePreviewKind({ name: 'app.exe', mime: 'application/octet-stream' })).toBe('unsupported')
  })
  it('renders SVG as an image, never as an executable document', async () => {
    const el = await mount('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'image', 'isolated.svg')
    expect(el.querySelector('img')?.alt).toBe('isolated.svg')
    expect(el.querySelector('iframe, svg, script')).toBeNull()
  })
  it('shows image decoding errors and clears them when choosing another resource', async () => {
    const panel = await mountMutable('invalid image', 'image', 'broken.png')
    const oldImage = panel.el.querySelector('img')!
    oldImage.dispatchEvent(new Event('error')); await flush()
    expect(panel.el.querySelector('[role="alert"]')).not.toBeNull()
    await panel.change('new image bytes', 'image', 'new.webp')
    oldImage.dispatchEvent(new Event('error')); await flush()
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    expect(panel.el.querySelector('img')?.alt).toBe('new.webp')
  })
  it('passes authorized PDF bytes to the application renderer without depending on a browser PDF plugin', async () => {
    const source = '%PDF-1.7\nfixture bytes'
    const panel = await mountMutable(source, 'pdf', 'document.pdf')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(panel.el.querySelector('iframe')).toBeNull()
    await vi.waitFor(() => expect(panel.el.querySelector('[data-testid="pdf-preview"]')?.textContent).toBe('document.pdf'))
    expect(panel.el.querySelector('[data-testid="pdf-preview"]')?.getAttribute('data-bytes')).toBe(String(source.length))
  })
  it.each(['image', 'audio', 'video'] as const)('does not let a late %s error from a previous resource fail the current resource', async kind => {
    const tag = kind === 'image' ? 'img' : kind
    const panel = await mountMutable('first bytes', kind, 'first.media')
    const oldElement = panel.el.querySelector(tag)!
    await panel.change('second bytes', kind, 'second.media')
    expect(panel.el.querySelector(tag)).not.toBe(oldElement)
    oldElement.dispatchEvent(new Event('error')); await flush()
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    expect(panel.el.querySelector(tag)).not.toBeNull()
  })
  it.each(['audio', 'video'] as const)('loads %s metadata without autoplay and stops playback on replacement and unmount', async kind => {
    const panel = await mountMutable('media bytes', kind, 'sample.media')
    const first = panel.el.querySelector(kind)!
    expect(first.controls).toBe(true)
    expect(first.preload).toBe('metadata')
    expect(first.autoplay).toBe(false)
    await panel.change('replacement media', kind, 'replacement.media')
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce()
    expect(HTMLMediaElement.prototype.load).toHaveBeenCalledOnce()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview-1')
    panel.unmount()
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(2)
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  })
  it.each(['audio', 'video'] as const)('reports unsupported %s codecs through an application error state', async kind => {
    const el = await mount('unsupported codec bytes', kind, 'broken.media')
    el.querySelector(kind)!.dispatchEvent(new Event('error')); await flush()
    expect(el.querySelector('[role="alert"]')).not.toBeNull()
  })
  it.each(['image', 'audio', 'video'] as const)('retries a failed %s with a fresh resource and ignores errors from the disposed attempt', async kind => {
    const tag = kind === 'image' ? 'img' : kind
    const panel = await mountMutable('resource bytes', kind, 'retry.media')
    const first = panel.el.querySelector<HTMLImageElement | HTMLMediaElement>(tag)!
    first.dispatchEvent(new Event('error')); await flush()
    const retry = panel.el.querySelector<HTMLButtonElement>('[role="alert"] button')!
    expect(retry.textContent).toBe('Retry')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview-1')
    if (kind !== 'image') {
      expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce()
      expect(HTMLMediaElement.prototype.load).toHaveBeenCalledOnce()
      expect(first.hasAttribute('src')).toBe(false)
    }
    retry.click(); await flush()
    const second = panel.el.querySelector<HTMLImageElement | HTMLMediaElement>(tag)!
    expect(second).not.toBe(first)
    expect(second.src).toBe('blob:preview-2')
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    first.dispatchEvent(new Event('error')); await flush()
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    // Retrying malformed bytes may fail again; keep a usable retry action.
    second.dispatchEvent(new Event('error')); await flush()
    expect(panel.el.querySelector('[role="alert"] button')?.textContent).toBe('Retry')
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  })
  it.each(['html', 'pdf'] as const)('retries a failed %s Blob read', async kind => {
    const blob = sourceBlob('<p>Recovered preview</p>')
    const read = kind === 'html'
      ? vi.spyOn(blob, 'text').mockRejectedValueOnce(new Error('Temporary read failure'))
      : vi.spyOn(blob, 'arrayBuffer').mockRejectedValueOnce(new Error('Temporary read failure'))
    const panel = await mountMutable(blob, kind, `retry.${kind}`)
    expect(panel.el.querySelector('[role="alert"]')).not.toBeNull()
    panel.el.querySelector<HTMLButtonElement>('[role="alert"] button')!.click(); await flush()
    expect(read).toHaveBeenCalledTimes(2)
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    if (kind === 'html') expect(panel.el.querySelector('iframe')?.srcdoc).toContain('Recovered preview')
    else await vi.waitFor(() => expect(panel.el.querySelector('[data-testid="pdf-preview"]')?.textContent).toBe('retry.pdf'))
  })
  it('does not restore a retry result after switching files', async () => {
    let finishRetry!: (source: string) => void
    const blob = sourceBlob('')
    vi.spyOn(blob, 'text').mockRejectedValueOnce(new Error('Read failed'))
      .mockImplementationOnce(() => new Promise(resolve => { finishRetry = resolve }))
    const panel = await mountMutable(blob, 'html', 'retry.html')
    panel.el.querySelector<HTMLButtonElement>('[role="alert"] button')!.click(); await flush()
    expect(panel.el.querySelector('[role="status"]')).not.toBeNull()
    await panel.change('current image', 'image', 'current.png')
    finishRetry('<p>Stale retry</p>'); await flush()
    expect(panel.el.querySelector('iframe, [role="alert"], [role="status"]')).toBeNull()
    expect(panel.el.querySelector('img')?.alt).toBe('current.png')
  })
  it('ignores an old asynchronous HTML read after selecting a different file', async () => {
    let resolveOld!: (source: string) => void
    const old = sourceBlob('')
    old.text = () => new Promise(resolve => { resolveOld = resolve })
    const panel = await mountMutable(old, 'html', 'old.html')
    await panel.change('<p>current document</p>', 'html', 'current.html')
    resolveOld('<p>old document</p>'); await flush()
    expect(panel.el.querySelector('iframe')?.srcdoc).toContain('current document')
    expect(panel.el.querySelector('iframe')?.srcdoc).not.toContain('old document')
  })
  it('ignores stale PDF byte reads after a new image is selected', async () => {
    let resolveOld!: (source: ArrayBuffer) => void
    const old = sourceBlob('')
    old.arrayBuffer = () => new Promise<ArrayBuffer>(resolve => { resolveOld = resolve })
    const panel = await mountMutable(old, 'pdf', 'old.pdf')
    await panel.change('image bytes', 'image', 'current.png')
    resolveOld(new ArrayBuffer(4)); await flush()
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    expect(panel.el.querySelector('img')?.alt).toBe('current.png')
    expect(URL.createObjectURL).toHaveBeenCalledOnce()
  })
})

describe('CSV worker lifetime', () => {
  class TestWorker {
    static instances: TestWorker[] = []
    onmessage: ((event: MessageEvent) => void) | null = null
    onerror: (() => void) | null = null
    terminate = vi.fn()
    postMessage = vi.fn()
    constructor() { TestWorker.instances.push(this) }
  }
  beforeEach(() => { TestWorker.instances = []; vi.stubGlobal('Worker', TestWorker) })
  it('a late event from a cancelled parser cannot replace rows or detach the current parser cleanup', async () => {
    const panel = await mountMutable('old', 'csv', 'old.csv')
    const old = TestWorker.instances[0]!
    const late = old.onmessage!
    await panel.change('current\tvalue', 'csv', 'current.tsv')
    const current = TestWorker.instances[1]!
    expect(old.terminate).toHaveBeenCalledOnce()
    expect(current.postMessage).toHaveBeenCalledWith({ source: 'current\tvalue', delimiter: '\t' })
    late({ data: { result: { rows: [['stale']], columns: 1, truncated: false } } } as MessageEvent)
    await flush()
    expect(panel.el.textContent).not.toContain('stale')
    panel.unmount()
    expect(current.terminate).toHaveBeenCalledOnce()
  })
  it('renders successful worker output and releases its worker', async () => {
    const el = await mount('name,value', 'csv', 'data.csv')
    const worker = TestWorker.instances[0]!
    worker.onmessage!({ data: { result: { rows: [['name', '=SUM(A1:A2)']], columns: 2, truncated: true } } } as MessageEvent)
    await flush()
    expect(el.querySelector('[role="table"]')?.textContent).toContain('=SUM(A1:A2)')
    expect(el.querySelector('[role="status"]')).not.toBeNull()
    expect(worker.terminate).toHaveBeenCalledOnce()
  })
  it('handles worker failures with an error state', async () => {
    const el = await mount('data', 'csv', 'data.csv')
    TestWorker.instances[0]!.onerror!(); await flush()
    expect(el.querySelector('[role="alert"]')).not.toBeNull()
    expect(TestWorker.instances[0]!.terminate).toHaveBeenCalledOnce()
  })
  it('retries failed CSV parsing with a new worker and rejects old worker results', async () => {
    const panel = await mountMutable('name,value', 'csv', 'retry.csv')
    const first = TestWorker.instances[0]!
    const late = first.onmessage!
    first.onerror!(); await flush()
    panel.el.querySelector<HTMLButtonElement>('[role="alert"] button')!.click(); await flush()
    const second = TestWorker.instances[1]!
    expect(first.terminate).toHaveBeenCalledOnce()
    expect(second.postMessage).toHaveBeenCalledWith({ source: 'name,value', delimiter: '' })
    late({ data: { result: { rows: [['old']], columns: 1, truncated: false } } } as MessageEvent)
    second.onmessage!({ data: { result: { rows: [['name', 'value']], columns: 2, truncated: false } } } as MessageEvent)
    await flush()
    expect(panel.el.querySelector('[role="alert"]')).toBeNull()
    expect(panel.el.querySelector('[role="table"]')?.textContent).toContain('namevalue')
    expect(panel.el.textContent).not.toContain('old')
    expect(second.terminate).toHaveBeenCalledOnce()
  })
  it('bounds parsing time and terminates a stalled worker', async () => {
    vi.useFakeTimers()
    const el = await mount('data', 'csv', 'data.csv')
    await vi.advanceTimersByTimeAsync(15000); await flush()
    expect(el.querySelector('[role="alert"]')).not.toBeNull()
    expect(TestWorker.instances[0]!.terminate).toHaveBeenCalledOnce()
  })
})
