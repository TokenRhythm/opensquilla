// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'

type ComponentProps = InstanceType<typeof WorkspaceFilePanel>['$props']
type PanelProps = { -readonly [Key in keyof ComponentProps]: ComponentProps[Key] }
const cleanups: (() => void)[] = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })
async function mount(props: PanelProps) {
  const el = document.createElement('div')
  const state = reactive(props)
  const onEvent = vi.fn()
  const app = createApp({ render: () => h(WorkspaceFilePanel, { ...state, 'onWorkbench-event': onEvent }) })
  app.use(i18n).mount(el)
  cleanups.push(() => app.unmount())
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
    const copy = [...el.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Copy file contents'))!
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
