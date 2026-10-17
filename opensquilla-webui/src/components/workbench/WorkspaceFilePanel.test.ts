// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'
import * as browser from '@/utils/browser'

type ComponentProps = InstanceType<typeof WorkspaceFilePanel>['$props']
type PanelProps = { -readonly [Key in keyof ComponentProps]: ComponentProps[Key] }
const cleanups: (() => void)[] = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })
async function mount(props: PanelProps) {
  const el = document.createElement('div')
  document.body.append(el)
  const state = reactive(props)
  const onEvent = vi.fn()
  const app = createApp({ render: () => h(WorkspaceFilePanel, { ...state, 'onWorkbench-event': onEvent }) })
  app.use(i18n).mount(el)
  cleanups.push(() => { app.unmount(); el.remove() })
  await nextTick()
  return { el, state, onEvent }
}
const page = (startLine = 1, endLine = 200, focusLine?: number) => ({
  relativePath: 'large.py', content: Array.from({ length: endLine - startLine + 1 }, (_, i) => `line ${startLine + i}`).join('\r\n'),
  startLine, endLine, totalLines: 450, paged: true, focusLine,
})
async function enter(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input'))
  await nextTick()
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
  await nextTick()
}

describe('workspace source panel', () => {
  it('restores media paused and captures playhead without requiring a text snapshot', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-restore')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
    const { el, onEvent } = await mount({ snapshot: null, previewBlob: new Blob(['media']), previewKind: 'audio',
      readingState: { mode: 'preview', query: '', jump: '', toolsOpen: false, startLine: 1, focusLine: 1,
        windowStart: 0, sourceTop: 0, sourceLeft: 0, markdownTop: 0,
        resource: { page: 0, mode: 'width', scale: 1, top: 0, left: 0, time: 12 } } })
    await vi.waitFor(() => expect(el.querySelector('audio')).not.toBeNull())
    const media = el.querySelector('audio')!
    Object.defineProperty(media, 'duration', { value: 60 })
    media.dispatchEvent(new Event('loadedmetadata'))
    expect(media.currentTime).toBe(12)
    expect(media.autoplay).toBe(false)
    media.currentTime = 24; media.dispatchEvent(new Event('timeupdate'))
    expect(onEvent).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'workspace-file-reading-state',
      payload: expect.objectContaining({ resource: expect.objectContaining({ time: 24 }) }) }))
  })
  it('restores source search and scrolling without moving keyboard focus', async () => {
    const { el, onEvent } = await mount({ snapshot: page(201, 400), readingState: {
      mode: 'source', query: 'line 250', jump: '250', toolsOpen: true, startLine: 201, focusLine: 250,
      windowStart: 0, sourceTop: 400, sourceLeft: 25, markdownTop: 0,
    } })
    const source = el.querySelector<HTMLElement>('.workspace-file__source')!
    expect(source.scrollTop).toBe(400)
    expect(source.scrollLeft).toBe(25)
    expect(el.querySelector<HTMLInputElement>('input[type="search"]')?.value).toBe('line 250')
    expect(document.activeElement).not.toBe(source)
    source.scrollTop = 650
    source.dispatchEvent(new Event('scroll'))
    expect(onEvent).toHaveBeenLastCalledWith(expect.objectContaining({
      type: 'workspace-file-reading-state', payload: expect.objectContaining({ sourceTop: 650 }),
    }))
  })

  it('does not apply an old scroll restore after user interaction during loading', async () => {
    const { el, state } = await mount({ snapshot: null, loading: true, readingState: {
      mode: 'source', query: '', jump: '', toolsOpen: false, startLine: 201, focusLine: 250,
      windowStart: 0, sourceTop: 400, sourceLeft: 25, markdownTop: 0,
    } })
    el.querySelector('.workspace-file')!.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    state.loading = false
    state.snapshot = page(201, 400)
    await nextTick(); await nextTick()
    expect(el.querySelector<HTMLElement>('.workspace-file__source')?.scrollTop).toBe(0)
  })
  it('renders binary media with no source controls and preserves standalone actions on preview errors', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-test')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
    const { el, state } = await mount({
      file: { requestedPath: 'song.mp3', path: 'song.mp3', name: 'song.mp3', mime: 'audio/mpeg',
        size: 100, kind: 'download', workspaceBinding: 'binding', revision: 'revision' },
      sessionKey: 'child', snapshot: null, previewBlob: new Blob(['media']), previewKind: 'audio',
    })
    await vi.waitFor(() => expect(el.querySelector('audio[controls]')).not.toBeNull())
    expect(el.querySelector('.workspace-file__source, .workspace-file__modes, .workspace-file__footer')).toBeNull()
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
    state.errorKey = 'workspaceReference.unavailable'
    await nextTick()
    expect(el.querySelector('[role="alert"]')).not.toBeNull()
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
  })

  it('keeps explicit Markdown line references in source even when complete text is available', async () => {
    const { el } = await mount({ snapshot: { relativePath: 'README.md', content: '# Title\nDetails',
      startLine: 1, endLine: 2, totalLines: 2, reference: {} as WorkspaceSourceSnapshot['reference'] } })
    expect(el.querySelector('.workspace-file__markdown, .workspace-file__modes')).toBeNull()
    expect(el.querySelector('.workspace-file__source')?.textContent).toContain('# Title')
  })

  it('uses the shared sandboxed HTML preview and switches back to the existing source pages', async () => {
    const browserSettings = (window as unknown as { happyDOM: { settings: { disableIframePageLoading: boolean } } }).happyDOM.settings
    browserSettings.disableIframePageLoading = true
    cleanups.push(() => { browserSettings.disableIframePageLoading = false })
    const html = '<html><body><button onclick="this.textContent = \'Clicked\'">Run</button></body></html>'
    const blob = new Blob([html], { type: 'text/html' })
    blob.text = async () => html
    const { el } = await mount({ previewBlob: blob, previewKind: 'html',
      snapshot: { ...page(), relativePath: 'review.html', content: '<html>source page</html>' } })
    await vi.waitFor(() => expect(el.querySelector('iframe')?.getAttribute('srcdoc')).toContain('Clicked'))
    const frame = el.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('srcdoc')).toContain("connect-src 'none'")
    expect(el.querySelector('pre')).toBeNull()
    const modes = [...el.querySelectorAll<HTMLButtonElement>('.workspace-file__modes button')]
    modes.find(button => button.textContent === 'Source')!.click()
    await nextTick()
    expect(el.querySelector('iframe')).toBeNull()
    expect(el.querySelector('pre')?.textContent).toContain('source page')
    modes.find(button => button.textContent === 'Preview')!.click()
    await vi.waitFor(() => expect(el.querySelector('iframe')).not.toBeNull())
  })

  it('never renders a partial HTML source page as a complete preview', async () => {
    const { el } = await mount({ previewKind: 'html',
      snapshot: { ...page(), relativePath: 'partial.html', content: '<html>Only first page' } })
    expect(el.querySelector('iframe')).toBeNull()
    expect(el.querySelector('.workspace-file__modes')).toBeNull()
    expect(el.querySelector('pre')?.textContent).toContain('Only first page')
  })

  it('keeps source and paging usable with an explicit optional preview failure', async () => {
    const { el, onEvent } = await mount({ previewKind: 'html', previewBlob: null,
      previewErrorKey: 'workbench.artifactPreview.unsupported',
      snapshot: { ...page(), relativePath: 'review.html', content: '<html>Authorized source page' } })
    expect(el.querySelector('.workspace-file__status')?.textContent).toBe('Preview unavailable')
    expect(el.querySelector('pre')?.textContent).toContain('Authorized source page')
    expect(el.querySelector('iframe')).toBeNull()
    el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click()
    expect(onEvent).toHaveBeenCalledWith({ type: 'workspace-file-page', payload: { startLine: 201 } })
  })

  it('uses shared code headers and copies only source, releasing controls on source mode', async () => {
    const copy = vi.spyOn(browser, 'copyTextWithFallback').mockResolvedValue(undefined)
    const content = 'Example document.\n\n- First item\n  - Nested item\n\n```typescript\nconst value = "hello";\n```'
    const totalLines = content.split('\n').length
    const { el } = await mount({ snapshot: { relativePath: 'README.md', content, totalLines, startLine: 1, endLine: totalLines } })
    const preview = el.querySelector('.workspace-file__markdown')!
    const pre = preview.querySelector('pre.code-block.markdown-code-block')!
    expect(pre).not.toBeNull()
    expect(pre.querySelector(':scope > .code-lang')?.textContent).toBe('typescript')
    expect(pre.querySelector(':scope > code')?.textContent).toBe('const value = "hello";')
    expect(preview.querySelector('ul > li > ul > li')?.textContent).toBe('Nested item')
    const button = pre.querySelector<HTMLButtonElement>('.code-copy-btn')!
    button.click()
    await vi.waitFor(() => expect(copy).toHaveBeenCalledExactlyOnceWith('const value = "hello";'))
    expect(pre.querySelectorAll('.code-copy-btn')).toHaveLength(1)

    const source = [...el.querySelectorAll<HTMLButtonElement>('button')].find(candidate => candidate.textContent === 'Source')!
    source.click(); await nextTick()
    expect(el.querySelector('.workspace-file__markdown')).toBeNull()
    button.click()
    expect(copy).toHaveBeenCalledTimes(1)

    const previewButton = [...el.querySelectorAll<HTMLButtonElement>('button')].find(candidate => candidate.textContent === 'Preview')!
    previewButton.click(); await nextTick()
    expect(el.querySelectorAll('pre.code-block.markdown-code-block')).toHaveLength(1)
    expect(el.querySelectorAll('.code-copy-btn')).toHaveLength(1)
  })

  it('previews complete Markdown safely and switches to highlighted source', async () => {
    const { el, state } = await mount({ snapshot: { relativePath: 'README.md', content: '# Read me\nA short document.', totalLines: 2, startLine: 1, endLine: 2 } })
    expect(el.querySelector('.workspace-file__markdown p')?.textContent).toBe('A short document.')
    expect(el.querySelector('.workspace-file__markdown')?.textContent).toContain('Read me')
    state.snapshot = { ...state.snapshot!, content: '# Read me\n<script>alert(1)</script>' }
    await nextTick()
    expect(el.querySelector('script')).toBeNull()
    expect(el.querySelector('pre')).toBeNull()
    const source = [...el.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Source')!
    source.click()
    await nextTick()
    expect(el.querySelectorAll('[data-line]')).toHaveLength(2)
    expect(el.querySelector('pre')?.textContent).toContain('<script>alert(1)</script>')
  })

  it('keeps partial Markdown pages in source mode instead of showing an incomplete document', async () => {
    const { el } = await mount({ snapshot: { ...page(), relativePath: 'large.md', content: '# Partial document' } })
    expect(el.querySelector('h1')).toBeNull()
    expect(el.querySelector('pre')).not.toBeNull()
    expect(el.querySelector('.workspace-file__find')?.getAttribute('style')).toContain('display: none')
  })
  it('renders source as text and highlights the exact inclusive reference line range', async () => {
    const { el } = await mount({ snapshot: {
      relativePath: 'src/sample.py', content: 'one\n<script>alert(1)</script>\nthree\nfour\n',
      startLine: 2, endLine: 3, totalLines: 4, reference: {} as WorkspaceSourceSnapshot['reference'],
    } })
    expect(el.querySelector('script')).toBeNull()
    expect(el.textContent).toContain('<script>alert(1)</script>')
    expect([...el.querySelectorAll('.is-selected')].map(node => node.getAttribute('data-line'))).toEqual(['2', '3'])
    expect(el.querySelectorAll('[data-line]')).toHaveLength(4)
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('src/sample.py')
  })

  it('matches Unicode line boundaries and bounds the number of rendered lines', async () => {
    const { el } = await mount({ snapshot: {
      relativePath: 'a.txt', content: 'one\u2028two\u0085' + 'line\n'.repeat(1000),
      startLine: 2, endLine: 1002, totalLines: 1002,
    } })
    expect(el.querySelector('[data-line="2"]')?.textContent).toContain('two')
    expect(el.querySelectorAll('[data-line]')).toHaveLength(200)
    expect(el.querySelector('.workspace-file__more')).not.toBeNull()
  })

  it.each([['', 1], ['one\r\ntwo\r\n', 2], ['\r\n', 1]] as const)('renders an empty or CRLF source without phantom lines: %s', async (content, totalLines) => {
    const { el } = await mount({ snapshot: { relativePath: 'sample.py', content, totalLines, startLine: 1, endLine: totalLines } })
    expect(el.querySelectorAll('[data-line]')).toHaveLength(totalLines)
    expect(el.querySelector('[data-line="1"]')).not.toBeNull()
  })

  it('keeps source searches explicit and preserves the query through page navigation', async () => {
    const { el, state, onEvent } = await mount({ snapshot: page(), viewId: 1 })
    const search = el.querySelector<HTMLInputElement>('input[type="search"]')!
    search.value = 'far away'
    search.dispatchEvent(new Event('input'))
    await nextTick()
    expect(el.textContent).not.toContain('No matching lines')
    await enter(search, 'far away')
    expect(onEvent).toHaveBeenLastCalledWith({ type: 'workspace-file-search', payload: { query: 'far away' } })
    state.searchStatus = 'searching'
    state.searchQuery = 'far away'
    await nextTick()
    expect(el.textContent).not.toContain('No matching lines')
    expect(el.textContent).toContain('Checking file')
    state.searchStatus = 'not-found'
    await nextTick()
    expect(el.textContent).toContain('No matching lines')
    state.snapshot = page(201, 400)
    state.searchStatus = 'idle'
    await nextTick()
    expect(search.value).toBe('far away')
    expect(el.textContent).not.toContain('No matching lines')
    state.viewId = 2
    await nextTick()
    expect(search.value).toBe('')
  })

  it('emits bounded page requests, keeps full pages visible, and disables next at EOF', async () => {
    const { el, state, onEvent } = await mount({ snapshot: page() })
    el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click()
    expect(onEvent).toHaveBeenLastCalledWith({ type: 'workspace-file-page', payload: { startLine: 201 } })
    state.snapshot = page(201, 400, 399)
    await nextTick()
    expect(el.querySelectorAll('[data-line]')).toHaveLength(200)
    expect(el.querySelector('[data-line="201"]')).not.toBeNull()
    expect(el.querySelector('[aria-current="location"]')?.getAttribute('data-line')).toBe('399')
    el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click()
    expect(onEvent).toHaveBeenLastCalledWith({ type: 'workspace-file-page', payload: { startLine: 1 } })
    state.snapshot = page(401, 450)
    await nextTick()
    expect(el.querySelectorAll('[data-line]')).toHaveLength(50)
    expect(el.querySelectorAll('.workspace-file__more')).toHaveLength(1)
  })

  it('jumps to the exact requested line and clamps requests beyond EOF', async () => {
    const scrolled: string[] = []
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    cleanups.push(() => {
      if (original) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', original)
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
    })
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true, value: vi.fn(function (this: HTMLElement) { scrolled.push(this.dataset.line || '') }),
    })
    const { el, state, onEvent } = await mount({ snapshot: page() })
    await enter(el.querySelector<HTMLInputElement>('input[type="number"]')!, '429')
    expect(onEvent).toHaveBeenLastCalledWith({ type: 'workspace-file-page', payload: { startLine: 401, focusLine: 429 } })
    state.snapshot = page(401, 450, 429)
    await nextTick()
    await nextTick()
    expect(scrolled[scrolled.length - 1]).toBe('429')
    expect(el.querySelector('[aria-current="location"]')?.getAttribute('data-line')).toBe('429')
    await enter(el.querySelector<HTMLInputElement>('input[type="number"]')!, '9999')
    expect(scrolled[scrolled.length - 1]).toBe('450')
    expect(el.querySelector('[aria-current="location"]')?.getAttribute('data-line')).toBe('450')
  })

  it('delegates whole-file copy to its runtime and surfaces pending/failure state', async () => {
    const { el, state, onEvent } = await mount({ snapshot: page() })
    const copy = el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')!
    copy.click()
    expect(onEvent).toHaveBeenLastCalledWith({ type: 'workspace-file-copy' })
    state.copying = true
    await nextTick()
    expect(copy.disabled).toBe(true)
    state.copying = false
    state.copyErrorKey = 'workspaceReference.copyFailed'
    await nextTick()
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Could not copy')
  })
})
