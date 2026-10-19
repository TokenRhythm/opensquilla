// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type ComputedRef } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import zhHans from '@/locales/zh-Hans.json'
import { WORKSPACE_FILES_KEY, WorkspaceFilesChangedError, WorkspaceTextLimitError, type WorkspaceDirectoryEntry, type WorkspaceDirectoryPage, type WorkspaceFilePage, type WorkspaceFiles, type WorkspaceFileSearch } from '@/modules/workspaceFiles'
import WorkspaceFilesPanel from './WorkspaceFilesPanel.vue'
import type { WorkspaceFilesReadingState } from './workspaceReadingState'
import { createWorkspaceFilesWorkbenchDefinition } from './workspaceFilesWorkbenchProvider'
import { createWorkspaceFilesWorkbenchItem } from '@/workbench/workspaceFileItems'
import { WorkbenchPanelRegistry, WorkbenchRuntimeManager } from '@/workbench/runtime'

const clipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: clipboard }))
vi.mock('@tanstack/vue-virtual', async () => {
  const { computed } = await import('vue')
  return {
    useVirtualizer: (options: ComputedRef<{ count: number; getItemKey(index: number): string | number }>) => computed(() => ({
      getVirtualItems: () => Array.from({ length: options.value.count }, (_, index) => ({ index, key: options.value.getItemKey(index), start: index * 28 })),
      getTotalSize: () => options.value.count * 28,
      measure: vi.fn(),
    })),
  }
})

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.clearAllMocks() })

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function entry(path: string, directory = false): WorkspaceDirectoryEntry {
  const parts = path.split('/')
  return { path, name: parts[parts.length - 1]!, type: directory ? 'directory' : 'file', kind: directory ? 'directory' : 'text', mime: directory ? '' : 'text/plain', size: directory ? null : 100, mtimeNs: '1' }
}

function listing(entries: WorkspaceDirectoryEntry[], path = '', binding = 'binding-a'): WorkspaceDirectoryPage {
  return { workspaceBinding: binding, relativePath: path, entries, nextCursor: null, hasMore: false, truncated: false }
}

function page(path: string, content: string, startLine = 1): WorkspaceFilePage {
  return { relativePath: path, content, startLine, endLine: startLine + 199, totalLines: 400 }
}

function fixture() {
  const files = {
    resolve: vi.fn<WorkspaceFiles['resolve']>(),
    listDir: vi.fn<NonNullable<WorkspaceFiles['listDir']>>(async () => listing([entry('a.ts'), entry('b.ts')])),
    readPage: vi.fn<NonNullable<WorkspaceFiles['readPage']>>(async (_session, file, start) => page(file.path, `content ${file.path} ${start}`, start)),
    read: vi.fn<WorkspaceFiles['read']>(async () => new Blob(['whole file'])),
    search: vi.fn<NonNullable<WorkspaceFiles['search']>>(async (_session, file) => ({ relativePath: file.path, totalLines: 400, matchLine: 201 })),
  }
  files.resolve.mockImplementation(async (_session, paths) => {
    const latest = await files.listDir.mock.results[files.listDir.mock.results.length - 1]?.value as WorkspaceDirectoryPage | undefined
    return paths.map(path => ({ requestedPath: path, path, name: path.split('/').pop()!, kind: 'text', mime: 'text/plain', size: 100,
      workspaceBinding: latest?.workspaceBinding || 'binding-a', revision: 'revision-a' }))
  })
  return files
}

async function flush() { await new Promise(resolve => setTimeout(resolve, 0)); await nextTick() }

async function mount(files: WorkspaceFiles, locale: 'en' | 'zh-Hans' = 'en', readingState?: WorkspaceFilesReadingState,
  handleEvent?: (event: { type: string; payload?: unknown }) => void, runtimeReady = true) {
  const el = document.createElement('div')
  document.body.append(el)
  const state = reactive({ sessionKey: 'session-a', readingState, runtimeReady })
  const onEvent = vi.fn(handleEvent)
  const app = createApp({ render: () => h(WorkspaceFilesPanel, { ...state, 'onWorkbench-event': onEvent }) })
  app.provide(WORKSPACE_FILES_KEY, files)
  app.use(createI18n({ legacy: false, locale, messages: { en, 'zh-Hans': zhHans } })).mount(el)
  let mounted = true
  const unmount = () => { if (mounted) app.unmount(); mounted = false; el.remove() }
  cleanups.push(unmount)
  await flush()
  return { el, state, unmount, onEvent }
}

async function select(el: HTMLElement, path: string) {
  const label = [...el.querySelectorAll<HTMLElement>('.workspace-files__name')].find(node => node.title === path)
  expect(label, `file tree should contain ${path}`).toBeDefined()
  label!.click()
  await flush()
}

async function search(el: HTMLElement, query: string) {
  el.querySelector<HTMLButtonElement>('.workspace-file__toolbar button[aria-label="Search source"]')!.click()
  await nextTick()
  const input = el.querySelector<HTMLInputElement>('.workspace-file__find input[type="search"]')!
  input.value = query
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  await flush()
}

describe('workspace source preview limits', () => {
  it('waits for a late runtime checkpoint instead of replacing it with an empty tree', async () => {
    const files = fixture()
    const { el, state, onEvent } = await mount(files, 'en', undefined, undefined, false)
    expect(files.listDir).not.toHaveBeenCalled()
    expect(onEvent).not.toHaveBeenCalled()
    state.readingState = { sessionKey: 'session-a', binding: 'binding-a', expanded: [], loadedCounts: {}, filter: 'a.ts',
      treeVisible: true, treeTop: 0, selectedPath: 'a.ts', sourceMode: true,
      selectedIdentity: JSON.stringify(['session-a', 'binding-a', 'a.ts', 'revision-a', 100]),
      reading: { mode: 'source', query: 'needle', jump: '', toolsOpen: true, startLine: 201, focusLine: 230, windowStart: 0, sourceTop: 400, sourceLeft: 0, markdownTop: 0 } }
    state.runtimeReady = true
    await vi.waitFor(() => expect(el.querySelector('.workspace-file__source')?.textContent).toContain('content a.ts 201'))
    expect(el.querySelector<HTMLInputElement>('.workspace-files__filter-input')?.value).toBe('a.ts')
    expect(el.querySelector<HTMLInputElement>('.workspace-file__find input[type="search"]')?.value).toBe('needle')
    expect(el.querySelector<HTMLElement>('.workspace-file__source')?.scrollTop).toBe(400)
  })

  it('returns the selected document before bounded directory replay and keeps cold expansion intent', async () => {
    const files = fixture()
    const paths = Array.from({ length: 20 }, (_, index) => `dir-${index}`)
    const calls: string[] = []
    files.listDir.mockImplementation(async (_session, path = '') => {
      calls.push(`list:${path}`)
      return path ? listing([], path) : listing([entry('a.ts'), ...paths.map(path => entry(path, true))])
    })
    files.readPage.mockImplementation(async (_session, file, start) => { calls.push('document'); return page(file.path, 'selected document', start) })
    const saved: WorkspaceFilesReadingState = { sessionKey: 'session-a', binding: 'binding-a', expanded: paths, loadedCounts: {}, filter: '',
      treeVisible: true, treeTop: 0, selectedPath: 'a.ts', sourceMode: true,
      selectedIdentity: JSON.stringify(['session-a', 'binding-a', 'a.ts', 'revision-a', 100]) }
    const { el, onEvent } = await mount(files, 'en', saved)
    await vi.waitFor(() => expect(el.querySelector('.workspace-file__source')?.textContent).toContain('selected document'))
    expect(calls.indexOf('document')).toBeLessThan(calls.indexOf('list:dir-0'))
    expect(files.listDir).toHaveBeenCalledTimes(9)
    expect(el.querySelectorAll('.workspace-files__row.is-more')).toHaveLength(12)
    const checkpoints = onEvent.mock.calls.filter(([event]) => event.type === 'workspace-files-reading-state')
    expect((checkpoints[checkpoints.length - 1]![0].payload as WorkspaceFilesReadingState).expanded).toHaveLength(20)
  })
  it('checkpoints through the real runtime manager before releasing a file tree', async () => {
    const files = fixture()
    const registry = new WorkbenchPanelRegistry()
    registry.register(createWorkspaceFilesWorkbenchDefinition(files, key => key))
    const reportError = vi.fn()
    const manager = new WorkbenchRuntimeManager(registry, { onError: reportError })
    const item = createWorkspaceFilesWorkbenchItem('session-a')
    manager.handle({ type: 'open', item }); manager.handle({ type: 'activate', item }); manager.handle({ type: 'resume', item })
    await manager.flush()
    const first = await mount(files, 'en', undefined, event => manager.handleComponentEvent(item, event))
    await manager.flush()
    expect(reportError).not.toHaveBeenCalled()
    await select(first.el, 'a.ts')
    await search(first.el, 'needle')
    await manager.flush()
    expect(reportError).not.toHaveBeenCalled()
    manager.handle({ type: 'suspend', item })
    first.unmount()
    await manager.flush()
    expect(manager.getRenderState(item.id)).toEqual({})
    manager.handle({ type: 'activate', item }); manager.handle({ type: 'resume', item })
    await manager.flush()
    const saved = manager.getRenderState(item.id).readingState as WorkspaceFilesReadingState
    expect(saved).toMatchObject({ selectedPath: 'a.ts', reading: { query: 'needle', startLine: 201 } })
    const second = await mount(files, 'en', saved)
    await vi.waitFor(() => expect(second.el.querySelector('.workspace-file__source')?.textContent).toContain('content a.ts 201'))
    await manager.disposeAll()
  })
  it('restores expanded tree, selection, search and source page after the DOM was released', async () => {
    const files = fixture()
    files.listDir.mockImplementation(async (_session, path = '') => path === 'src'
      ? listing([entry('src/a.ts')], path) : listing([entry('src', true)]))
    const first = await mount(files)
    await select(first.el, 'src')
    await select(first.el, 'src/a.ts')
    await search(first.el, 'needle')
    const source = first.el.querySelector<HTMLElement>('.workspace-file__source')!
    source.scrollTop = 350
    source.dispatchEvent(new Event('scroll'))
    const tree = first.el.querySelector<HTMLElement>('.workspace-files__tree-scroll')!
    tree.scrollTop = 48
    tree.dispatchEvent(new Event('scroll'))
    await nextTick()
    const checkpoints = first.onEvent.mock.calls.filter(([event]) => event.type === 'workspace-files-reading-state')
    const saved = checkpoints[checkpoints.length - 1]![0].payload as WorkspaceFilesReadingState
    expect(saved).toMatchObject({ selectedPath: 'src/a.ts', expanded: ['src'], treeTop: 48, reading: { query: 'needle', startLine: 201, sourceTop: 350 } })
    expect(JSON.stringify(saved)).not.toContain('content src/a.ts')
    first.unmount()
    const second = await mount(files, 'en', saved)
    await vi.waitFor(() => expect(second.el.querySelector('.workspace-file__source')?.textContent).toContain('content src/a.ts 201'))
    expect(second.el.querySelector('.workspace-files__row.is-selected')?.textContent).toContain('a.ts')
    expect(second.el.querySelector<HTMLInputElement>('.workspace-file__find input[type="search"]')?.value).toBe('needle')
    expect(second.el.querySelector<HTMLElement>('.workspace-file__source')?.scrollTop).toBe(350)
    expect(second.el.querySelector<HTMLElement>('.workspace-files__tree-scroll')?.scrollTop).toBe(48)
    expect(files.resolve).toHaveBeenCalledWith('session-a', ['src/a.ts'], expect.any(AbortSignal))
  })

  it('drops stale reading positions when the workspace binding or file revision changed', async () => {
    const files = fixture()
    const saved: WorkspaceFilesReadingState = { sessionKey: 'session-a', binding: 'binding-a', expanded: [], loadedCounts: {},
      filter: '', treeVisible: true, treeTop: 0, selectedPath: 'a.ts', sourceMode: false,
      selectedIdentity: JSON.stringify(['session-a', 'binding-a', 'a.ts', 'old-revision', 100]),
      reading: { mode: 'source', query: 'old', jump: '', toolsOpen: true, startLine: 201, focusLine: 220, windowStart: 0, sourceTop: 400, sourceLeft: 0, markdownTop: 0 } }
    const { el } = await mount(files, 'en', saved)
    await vi.waitFor(() => expect(el.querySelector('.workspace-file__source')?.textContent).toContain('content a.ts 1'))
    expect(el.querySelector<HTMLInputElement>('.workspace-file__find input[type="search"]')?.value).toBe('')
    expect(files.readPage).toHaveBeenLastCalledWith('session-a', expect.objectContaining({ revision: 'revision-a' }), 1, 200, expect.any(AbortSignal))
    const other = await mount(files, 'en', { ...saved, binding: 'obsolete-binding' })
    expect(other.el.querySelector('.workspace-file__source')).toBeNull()
    expect(other.el.querySelector('.workspace-files__row.is-selected')).toBeNull()
  })
  it('preserves MIME-only Markdown preview without duplicating resource actions', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([{ ...entry('REPORT'), mime: 'text/markdown' }]))
    files.resolve.mockResolvedValue([{ requestedPath: 'REPORT', path: 'REPORT', name: 'REPORT',
      mime: 'text/markdown', kind: 'text', size: 100, workspaceBinding: 'binding-a', revision: 'revision-a' }])
    const content = '# MIME document\nBody'
    const blob = new Blob([content])
    blob.arrayBuffer = async () => new TextEncoder().encode(content).buffer
    files.read.mockResolvedValue(blob)
    const { el } = await mount(files)
    await select(el, 'REPORT')
    await vi.waitFor(() => expect(el.querySelector('.workspace-file__markdown')?.textContent).toContain('MIME document'))
    expect(el.querySelectorAll('.workspace-resource-actions')).toHaveLength(1)
    expect(files.readPage).not.toHaveBeenCalled()
  })

  it('defaults long bounded Markdown to a complete preview and retains source pagination', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([entry('report.md')]))
    const content = '# Full report\n' + 'A paragraph.\n\n'.repeat(250) + '## Final section'
    const blob = new Blob([content])
    blob.arrayBuffer = async () => new TextEncoder().encode(content).buffer
    files.read.mockResolvedValue(blob)
    const { el } = await mount(files)
    await select(el, 'report.md')
    await vi.waitFor(() => expect(el.querySelector('.workspace-file__markdown')?.textContent).toContain('Final section'))
    expect(files.readPage).not.toHaveBeenCalled()
    const source = [...el.querySelectorAll<HTMLButtonElement>('.workspace-file__modes button')].find(button => button.textContent === 'Source')!
    source.click(); await nextTick()
    expect(el.querySelectorAll('[data-line]')).toHaveLength(200)
    el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click(); await nextTick()
    expect(el.querySelector('[data-line="201"]')).not.toBeNull()
    expect(files.readPage).not.toHaveBeenCalled()
  })

  it.each(['oversized', 'offline'])('shows explicit %s Markdown fallback while keeping source and actions usable', async scenario => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([{ ...entry('report.md'), size: scenario === 'oversized' ? 200_000 : 100 }]))
    files.read.mockRejectedValue(new Error('offline'))
    const { el } = await mount(files)
    await select(el, 'report.md')
    expect(el.querySelector('.workspace-file__status')?.textContent).toContain(scenario === 'offline' ? 'Preview unavailable' : 'too large')
    expect(el.querySelector('.workspace-file__source')).not.toBeNull()
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
    if (scenario === 'oversized') expect(files.read).not.toHaveBeenCalled()
  })

  it('does not show an older Markdown source page after a stale complete read', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([entry('report.md')]))
    files.read.mockRejectedValue(new WorkspaceFilesChangedError())
    const { el } = await mount(files)
    await select(el, 'report.md')
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('changed')
    expect(files.readPage).not.toHaveBeenCalled()
  })

  it.each([
    ['line', 'A line in this file is too long for the preview'],
    ['page', 'This section is too large for the preview'],
  ] as const)('shows actionable %s limit feedback while preserving file actions and the usable tree', async (limit, message) => {
    const files = fixture()
    files.readPage.mockRejectedValueOnce(new WorkspaceTextLimitError(limit))
    const { el } = await mount(files)
    await select(el, 'a.ts')
    expect(el.querySelector('[role="alert"]')?.textContent).toContain(message)
    expect(el.textContent).toContain('Download the file or open it in another application')
    expect(el.textContent).not.toContain('Workspace files are unavailable')
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
    await select(el, 'b.ts')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(el.textContent).toContain('content b.ts')
  })
})

describe('workspace file filter toolbar', () => {
  it.each([
    ['en', 'Search files', 'Filters files already loaded in this tree'],
    ['zh-Hans', '搜索文件', '仅筛选文件树中已加载的文件'],
  ] as const)('keeps the %s input label short and exposes the accurate filtering scope', async (locale, placeholder, help) => {
    const { el } = await mount(fixture(), locale)
    // Layout is visually checked in the real browser. This guards the narrow
    // toolbar's short label and separate icon-only action without fake geometry.
    el.style.width = '150px'
    const toolbar = el.querySelector('.workspace-files__tree-toolbar')!
    const input = toolbar.querySelector<HTMLInputElement>('input[type="search"]')!
    expect(input.classList.contains('workspace-files__filter-input')).toBe(true)
    expect(input.placeholder).toBe(placeholder)
    expect(input.getAttribute('aria-label')).toBe(placeholder)
    expect(input.title).toBe(help)
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toBe(help)
    expect(toolbar.querySelector('.workspace-files__filter-icon[aria-hidden="true"] svg')).not.toBeNull()
    expect(toolbar.querySelector('.workspace-files__filter-clear')).toBeNull()
    const refresh = toolbar.querySelector<HTMLButtonElement>('.workspace-files__refresh')!
    expect(refresh.parentElement).toBe(toolbar)
    expect(refresh.textContent?.trim()).toBe('')
    expect(refresh.getAttribute('aria-label')).toBeTruthy()
  })

  it('clears a query from its button and returns keyboard focus without fetching extra directories', async () => {
    const files = fixture()
    const { el } = await mount(files)
    const input = el.querySelector<HTMLInputElement>('.workspace-files__filter input')!
    input.value = 'a.ts'; input.dispatchEvent(new Event('input', { bubbles: true })); await flush()
    expect([...el.querySelectorAll<HTMLElement>('.workspace-files__name')].map(node => node.title)).toEqual(['a.ts'])
    const clear = el.querySelector<HTMLButtonElement>('button[aria-label="Clear file search"]')!
    clear.focus(); clear.click(); await flush()
    expect(document.activeElement).toBe(input)
    expect(input.value).toBe('')
    expect(el.querySelector('.workspace-files__filter-clear')).toBeNull()
    expect([...el.querySelectorAll<HTMLElement>('.workspace-files__name')].map(node => node.title)).toEqual(['a.ts', 'b.ts'])
    expect(files.listDir).toHaveBeenCalledOnce()
    expect(files.search).not.toHaveBeenCalled()
  })

  it('uses Escape to clear a query while leaving an empty-input Escape available to the surrounding panel', async () => {
    const { el } = await mount(fixture())
    const input = el.querySelector<HTMLInputElement>('.workspace-files__filter input')!
    const parentKeydown = vi.fn()
    el.addEventListener('keydown', parentKeydown)
    input.focus()
    input.value = 'missing'; input.dispatchEvent(new Event('input', { bubbles: true })); await flush()
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    input.dispatchEvent(escape); await flush()
    expect(escape.defaultPrevented).toBe(true)
    expect(parentKeydown).not.toHaveBeenCalled()
    expect(input.value).toBe('')
    expect(document.activeElement).toBe(input)
    const emptyEscape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    input.dispatchEvent(emptyEscape)
    expect(emptyEscape.defaultPrevented).toBe(false)
    expect(parentKeydown).toHaveBeenCalledOnce()
  })

  it('keeps refresh distinct from filtering and disabled while the directory request is pending', async () => {
    const files = fixture()
    const pending = deferred<WorkspaceDirectoryPage>()
    files.listDir.mockReturnValueOnce(pending.promise)
    const { el } = await mount(files)
    const refresh = el.querySelector<HTMLButtonElement>('.workspace-files__tree-toolbar .workspace-files__refresh')!
    expect(refresh.disabled).toBe(true)
    pending.resolve(listing([entry('ready.ts')])); await flush()
    expect(refresh.disabled).toBe(false)
    refresh.click(); await flush()
    expect(files.listDir).toHaveBeenCalledTimes(2)
    expect(files.search).not.toHaveBeenCalled()
  })
})

describe('workspace files panel request ownership', () => {
  it('retains file actions for unsupported binary formats without reading them as text', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([{ ...entry('bundle.zip'), kind: 'download', mime: 'application/zip' }]))
    const { el } = await mount(files)
    await select(el, 'bundle.zip')
    expect(files.read).not.toHaveBeenCalled()
    expect(files.readPage).not.toHaveBeenCalled()
    expect(el.querySelector('.workspace-files__binary-preview [role="alert"]')).not.toBeNull()
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
  })
  it('rejects an oversized image before downloading preview bytes but preserves file actions', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([{ ...entry('large.png'), kind: 'image', mime: 'image/png', size: 30 * 1024 * 1024 + 1 }]))
    const { el } = await mount(files)
    await select(el, 'large.png')
    expect(files.read).not.toHaveBeenCalled()
    expect(el.querySelector('.workspace-files__binary-preview [role="alert"]')).not.toBeNull()
    expect(el.querySelector('.workspace-resource-actions')).not.toBeNull()
  })
  it('aborts a pending binary preview and ignores its late response after selecting source', async () => {
    const files = fixture()
    files.listDir.mockResolvedValue(listing([{ ...entry('photo.webp'), kind: 'image', mime: 'image/webp' }, entry('current.ts')]))
    const pending = deferred<Blob>()
    files.read.mockReturnValueOnce(pending.promise)
    const { el } = await mount(files)
    await select(el, 'photo.webp')
    const signal = files.read.mock.calls[0]![2]!
    await select(el, 'current.ts')
    expect(signal.aborted).toBe(true)
    pending.resolve(new Blob(['old image'])); await flush()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('current.ts')
    expect(el.querySelector('.resource-preview')).toBeNull()
  })
  it('keeps pages pinned and requires reload after a file change', async () => {
    const files = fixture()
    const { el } = await mount(files)
    await select(el, 'a.ts')
    files.readPage.mockRejectedValueOnce(new WorkspaceFilesChangedError())
    el.querySelector<HTMLButtonElement>('.workspace-file__footer .workspace-file__more')!.click(); await flush()
    expect(files.readPage).toHaveBeenLastCalledWith('session-a', expect.objectContaining({ revision: 'revision-a' }), 201, 400, expect.any(AbortSignal))
    expect(el.textContent).toContain('The file changed')
    expect(files.resolve).toHaveBeenCalledTimes(1)
  })
  it.each(['success', 'failure'] as const)('ignores an old session directory %s after switching sessions', async outcome => {
    const files = fixture()
    const old = deferred<WorkspaceDirectoryPage>()
    files.listDir.mockReturnValueOnce(old.promise)
    files.listDir.mockResolvedValueOnce(listing([entry('new.ts')], '', 'binding-b'))
    const { el, state } = await mount(files)
    state.sessionKey = 'session-b'
    await flush()
    if (outcome === 'success') old.resolve(listing([entry('stale.ts')]))
    else old.reject(new Error('late old-session failure'))
    await flush()
    expect(el.querySelector('.workspace-files__tree')?.textContent).toContain('new.ts')
    expect(el.querySelector('.workspace-files__tree')?.textContent).not.toContain('stale.ts')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await select(el, 'new.ts')
    expect(files.readPage).toHaveBeenCalledWith('session-b', expect.objectContaining({ path: 'new.ts', workspaceBinding: 'binding-b' }), 1, 200, expect.any(AbortSignal))
  })

  it('fences old results when a session is left and reopened before its first response', async () => {
    const files = fixture()
    const old = deferred<WorkspaceDirectoryPage>()
    files.listDir.mockReturnValueOnce(old.promise)
    files.listDir.mockResolvedValueOnce(listing([entry('b.ts')]))
    files.listDir.mockResolvedValueOnce(listing([entry('fresh-a.ts')], '', 'binding-fresh'))
    const { el, state } = await mount(files)
    state.sessionKey = 'session-b'; await flush()
    state.sessionKey = 'session-a'; await flush()
    old.resolve(listing([entry('old-a.ts')], '', 'binding-old')); await flush()
    expect(el.querySelector('.workspace-files__tree')?.textContent).toContain('fresh-a.ts')
    expect(el.querySelector('.workspace-files__tree')?.textContent).not.toContain('old-a.ts')
    await select(el, 'fresh-a.ts')
    expect(files.readPage).toHaveBeenLastCalledWith('session-a', expect.objectContaining({ workspaceBinding: 'binding-fresh' }), 1, 200, expect.any(AbortSignal))
  })

  it('keeps a different selected file when an earlier page finishes', async () => {
    const files = fixture()
    const { el } = await mount(files)
    await select(el, 'a.ts')
    const pending = deferred<WorkspaceFilePage>()
    files.readPage.mockReturnValueOnce(pending.promise)
    el.querySelector<HTMLButtonElement>('.workspace-file__footer .workspace-file__more')!.click()
    await nextTick()
    await select(el, 'b.ts')
    pending.resolve(page('a.ts', 'stale next page', 201)); await flush()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.ts')
    expect(el.querySelector('pre')?.textContent).toContain('content b.ts 1')
    expect(el.textContent).not.toContain('stale next page')
  })

  it('does not issue a search-page read after the selected file has changed', async () => {
    const files = fixture()
    const { el } = await mount(files)
    await select(el, 'a.ts')
    const pending = deferred<Awaited<ReturnType<NonNullable<WorkspaceFiles['search']>>>>()
    files.search.mockReturnValueOnce(pending.promise)
    await search(el, 'needle')
    await select(el, 'b.ts')
    pending.resolve({ relativePath: 'a.ts', totalLines: 400, matchLine: 201 }); await flush()
    expect(files.readPage).toHaveBeenCalledTimes(2)
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.ts')
  })

  it('discards a search-result page already in flight when the selected file changes', async () => {
    const files = fixture()
    const { el } = await mount(files)
    await select(el, 'a.ts')
    const pending = deferred<WorkspaceFilePage>()
    files.readPage.mockReturnValueOnce(pending.promise)
    await search(el, 'needle')
    expect(files.readPage).toHaveBeenLastCalledWith('session-a', expect.objectContaining({ path: 'a.ts' }), 201, 400, expect.any(AbortSignal))
    await select(el, 'b.ts')
    pending.resolve(page('a.ts', 'stale search result', 201)); await flush()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.ts')
    expect(el.textContent).not.toContain('stale search result')
  })

  it('copies the whole file with a check and cancels a pending copy when selection changes', async () => {
    const files = fixture()
    const { el } = await mount(files)
    await select(el, 'a.ts')
    const copy = el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')!
    const icon = copy.innerHTML
    copy.click()
    await vi.waitFor(() => expect(clipboard).toHaveBeenCalledExactlyOnceWith('whole file'))
    await vi.waitFor(() => expect(copy.disabled).toBe(false))
    expect(copy.title).toBe('Copy file contents')
    expect(copy.innerHTML).not.toBe(icon)
    expect(copy.getAttribute('aria-label')).toBe('Copy file contents')
    clipboard.mockClear()
    const pending = deferred<Blob>()
    files.read.mockReturnValueOnce(pending.promise)
    el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')!.click()
    await nextTick()
    await select(el, 'b.ts')
    pending.resolve(new Blob(['stale clipboard contents'])); await flush()
    expect(clipboard).not.toHaveBeenCalled()
    expect(el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')?.disabled).toBe(false)
    expect(el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')?.innerHTML).toBe(icon)
  })

  it('filters by a loaded descendant even after its nonmatching parent is collapsed', async () => {
    const files = fixture()
    files.listDir.mockResolvedValueOnce(listing([entry('src', true), entry('unrelated.ts')]))
    files.listDir.mockResolvedValueOnce(listing([entry('src/needle.ts'), entry('src/other.ts')], 'src'))
    const { el } = await mount(files)
    await select(el, 'src')
    expect(el.querySelector('.workspace-files__tree')?.textContent).toContain('needle.ts')
    await select(el, 'src')
    expect(el.querySelector('.workspace-files__tree')?.textContent).not.toContain('needle.ts')
    const input = el.querySelector<HTMLInputElement>('.workspace-files__filter input')!
    input.value = 'needle'
    input.dispatchEvent(new Event('input', { bubbles: true })); await flush()
    expect([...el.querySelectorAll<HTMLElement>('.workspace-files__name')].map(node => node.title)).toEqual(['src', 'src/needle.ts'])
    expect(files.listDir).toHaveBeenCalledTimes(2)
  })
})

// Upstream cancellation and loaded-tree contracts remain valid for unified previews.
function directoryPage(entries = [entry('a.txt'), entry('b.txt')], workspaceBinding = 'binding-a'): WorkspaceDirectoryPage {
  return { workspaceBinding, relativePath: '', entries, hasMore: false, nextCursor: null, truncated: false }
}

function upstreamPage(path = 'a.txt', startLine = 1, content = `${path} page ${startLine}`): WorkspaceFilePage {
  return { relativePath: path, content, startLine, endLine: startLine + 199, totalLines: 600 }
}

function access() {
  const files = {
    resolve: vi.fn<WorkspaceFiles['resolve']>(async (_session, paths) => paths.map(path => ({ requestedPath: path, path, name: path, kind: 'text', mime: 'text/plain', size: 1000, workspaceBinding: 'binding-a' }))),
    read: vi.fn<WorkspaceFiles['read']>(async () => new Blob(['contents'])),
    readPage: vi.fn<NonNullable<WorkspaceFiles['readPage']>>(async (_session, file, start) => upstreamPage(file.path, start)),
    search: vi.fn<NonNullable<WorkspaceFiles['search']>>(async (_session, file) => ({ relativePath: file.path, totalLines: 600, matchLine: 201 })),
    listDir: vi.fn<NonNullable<WorkspaceFiles['listDir']>>(async () => directoryPage()),
  }
  files.resolve.mockImplementation(async (_session, paths) => {
    const latest = await files.listDir.mock.results[files.listDir.mock.results.length - 1]?.value as WorkspaceDirectoryPage | undefined
    return paths.map(path => ({ requestedPath: path, path, name: path, kind: 'text', mime: 'text/plain', size: 1000, workspaceBinding: latest?.workspaceBinding || 'binding-a' }))
  })
  return files
}

async function nextPage(el: HTMLElement) {
  el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click()
  await flush()
}

async function query(el: HTMLElement, value: string, submit = true) {
  if (!el.querySelector('.workspace-file__find')) {
    el.querySelector<HTMLButtonElement>('.workspace-file__toolbar button[aria-label="Search source"]')!.click()
    await nextTick()
  }
  const input = el.querySelector<HTMLInputElement>('.workspace-file__find input[type="search"]')!
  input.value = value
  input.dispatchEvent(new Event('input'))
  await nextTick()
  if (submit) input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
  await flush()
}

async function filterTree(el: HTMLElement, value: string) {
  const input = el.querySelector<HTMLInputElement>('.workspace-files__filter input')!
  input.value = value
  input.dispatchEvent(new Event('input'))
  await flush()
}

function treePaths(el: HTMLElement) {
  return [...el.querySelectorAll<HTMLElement>('.workspace-files__name')].map(node => node.title)
}

describe('WorkspaceFilesPanel tree filtering', () => {
  it('retains ancestors of loaded matches without fetching directories or changing selection and expansion', async () => {
    const files = access()
    files.listDir.mockResolvedValueOnce(directoryPage([entry('src', true), entry('unopened', true), entry('readme.txt')]))
      .mockResolvedValueOnce(directoryPage([entry('src/lib', true), entry('src/other.txt')]))
      .mockResolvedValueOnce(directoryPage([entry('src/lib/answer.txt'), entry('src/lib/other.txt')]))
    const { el } = await mount(files)
    await select(el, 'src')
    await select(el, 'src/lib')
    await select(el, 'src/lib/answer.txt')
    const originalPaths = treePaths(el)

    await filterTree(el, ' ANSWER.TXT ')
    expect(treePaths(el)).toEqual(['src', 'src/lib', 'src/lib/answer.txt'])
    expect([...el.querySelectorAll('[role="treeitem"]')].map(row => row.getAttribute('aria-level'))).toEqual(['1', '2', '3'])
    expect(el.querySelector('.is-selected .workspace-files__name')?.getAttribute('title')).toBe('src/lib/answer.txt')
    expect(files.listDir).toHaveBeenCalledTimes(3)
    expect(files.readPage).toHaveBeenCalledTimes(1)

    await filterTree(el, '')
    expect(treePaths(el)).toEqual(originalPaths)
    expect(el.querySelectorAll('[role="treeitem"][aria-expanded="true"]')).toHaveLength(2)
    expect(el.querySelector('.is-selected .workspace-files__name')?.getAttribute('title')).toBe('src/lib/answer.txt')
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('src/lib/answer.txt')
    expect(files.listDir).toHaveBeenCalledTimes(3)
    expect(files.readPage).toHaveBeenCalledTimes(1)
  })

  it('keeps pagination reachable through unmatched ancestors and filters newly loaded entries', async () => {
    const files = access()
    files.listDir.mockResolvedValueOnce({ ...directoryPage([entry('src', true)]), hasMore: true, nextCursor: 'root-next' })
      .mockResolvedValueOnce({ ...directoryPage([entry('src/other.txt')]), relativePath: 'src', hasMore: true, nextCursor: 'src-next' })
      .mockResolvedValueOnce({ ...directoryPage([entry('src/answer.txt'), entry('src/unrelated.txt')]), relativePath: 'src' })
    const { el } = await mount(files)
    await select(el, 'src')

    await filterTree(el, 'answer.txt')
    expect(treePaths(el)).toEqual(['src'])
    expect(el.querySelectorAll('.is-more')).toHaveLength(2)
    expect(files.listDir).toHaveBeenCalledTimes(2)
    el.querySelector<HTMLElement>('.is-more[aria-level="2"]')!.click()
    await flush()

    expect(files.listDir.mock.calls[2]!.slice(0, 5)).toEqual(['session-a', 'src', 'src-next', 200, 'binding-a'])
    expect(treePaths(el)).toEqual(['src', 'src/answer.txt'])
    expect(el.querySelectorAll('.is-more')).toHaveLength(1)
    expect(el.querySelector('.is-more')?.getAttribute('aria-level')).toBe('1')
    await filterTree(el, '')
    expect(treePaths(el)).toEqual(['src', 'src/other.txt', 'src/answer.txt', 'src/unrelated.txt'])
    expect(files.listDir).toHaveBeenCalledTimes(3)
  })
})

describe('WorkspaceFilesPanel request ownership', () => {
  it.each([
    ['a.txt', 'resolve'], ['b.txt', 'resolve'], ['a.txt', 'reject'], ['b.txt', 'reject'],
  ] as const)('ignores a stale page %s/%s after selecting or reopening a file', async (nextPath, outcome) => {
    const files = access()
    const pendingPage = deferred<WorkspaceFilePage>()
    const nextSelection = deferred<WorkspaceFilePage>()
    const { el } = await mount(files)
    await select(el, 'a.txt')
    files.readPage.mockReturnValueOnce(pendingPage.promise).mockReturnValueOnce(nextSelection.promise)
    await nextPage(el)
    const signal = files.readPage.mock.calls[1]![4]!
    await select(el, nextPath)
    expect(signal.aborted).toBe(true)
    if (outcome === 'resolve') pendingPage.resolve(upstreamPage('a.txt', 201, 'stale page'))
    else pendingPage.reject(new Error('old failure'))
    await flush()
    expect(el.querySelector('.workspace-file [role="status"]')).not.toBeNull()
    expect(el.querySelector('.workspace-file pre')).toBeNull()
    expect(el.querySelector('[role="alert"]')).toBeNull()
    nextSelection.resolve(upstreamPage(nextPath, 1, 'current selection'))
    await flush()
    expect(el.querySelector('pre')?.textContent).toContain('current selection')
    expect(el.textContent).not.toContain('stale page')
  })

  it('does not read a search result page after the selection changes', async () => {
    const files = access()
    const search = deferred<WorkspaceFileSearch>()
    files.search.mockReturnValueOnce(search.promise)
    const { el } = await mount(files)
    await select(el, 'a.txt')
    await query(el, 'needle')
    await select(el, 'b.txt')
    search.resolve({ relativePath: 'a.txt', totalLines: 600, matchLine: 401 })
    await flush()
    expect(files.search.mock.calls[0]![3]!.aborted).toBe(true)
    expect(files.readPage).toHaveBeenCalledTimes(2)
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.txt')
  })

  it.each(['resolve', 'reject'] as const)('ignores a search page %s after its file is replaced', async outcome => {
    const files = access()
    const searchPage = deferred<WorkspaceFilePage>()
    const { el } = await mount(files)
    await select(el, 'a.txt')
    files.readPage.mockReturnValueOnce(searchPage.promise)
    await query(el, 'needle')
    expect(files.readPage).toHaveBeenCalledTimes(2)
    await select(el, 'b.txt')
    if (outcome === 'resolve') searchPage.resolve(upstreamPage('a.txt', 201, 'stale search page'))
    else searchPage.reject(new Error('old search failure'))
    await flush()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.txt')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(el.textContent).not.toContain('stale search page')
  })

  it('cancels a search when its query changes without fetching or displaying the old match', async () => {
    const files = access()
    const search = deferred<WorkspaceFileSearch>()
    files.search.mockReturnValueOnce(search.promise)
    const { el } = await mount(files)
    await select(el, 'a.txt')
    await query(el, 'needle')
    await query(el, '', false)
    search.resolve({ relativePath: 'a.txt', totalLines: 600, matchLine: 401 })
    await flush()
    expect(files.search.mock.calls[0]![3]!.aborted).toBe(true)
    expect(files.readPage).toHaveBeenCalledTimes(1)
    expect(el.querySelector('pre')?.textContent).toContain('a.txt page 1')
    expect(el.querySelector('.workspace-file__status')).toBeNull()
  })

  it('keeps the latest search result when an earlier search page completes later', async () => {
    const files = access()
    const oldPage = deferred<WorkspaceFilePage>()
    const { el } = await mount(files)
    await select(el, 'a.txt')
    files.readPage.mockReturnValueOnce(oldPage.promise)
    await query(el, 'old query')
    files.search.mockResolvedValueOnce({ relativePath: 'a.txt', totalLines: 600, matchLine: 401 })
    await query(el, 'new query')
    oldPage.resolve(upstreamPage('a.txt', 201, 'old query match'))
    await flush()
    expect(el.querySelector('pre')?.textContent).toContain('a.txt page 401')
    expect(el.querySelector('[aria-current="location"]')?.getAttribute('data-line')).toBe('401')
    expect(el.querySelector<HTMLInputElement>('.workspace-file input[type="search"]')?.value).toBe('new query')
  })

  it('lets page navigation supersede a pending search without clearing the new loading state', async () => {
    const files = access()
    const search = deferred<WorkspaceFileSearch>()
    const pendingPage = deferred<WorkspaceFilePage>()
    files.search.mockReturnValueOnce(search.promise)
    const { el } = await mount(files)
    await select(el, 'a.txt')
    await query(el, 'needle')
    files.readPage.mockReturnValueOnce(pendingPage.promise)
    await nextPage(el)
    search.reject(new Error('late search failure'))
    await flush()
    expect(el.querySelector('.workspace-file [role="status"]')).not.toBeNull()
    expect(el.querySelector('[role="alert"]')).toBeNull()
    pendingPage.resolve(upstreamPage('a.txt', 201))
    await flush()
    expect(el.querySelector('pre')?.textContent).toContain('a.txt page 201')
  })

  it.each(['resolve', 'reject'] as const)('ignores a previous session directory %s and preserves the new binding', async outcome => {
    const files = access()
    const previous = deferred<WorkspaceDirectoryPage>()
    const current = deferred<WorkspaceDirectoryPage>()
    files.listDir.mockReturnValueOnce(previous.promise).mockReturnValueOnce(current.promise)
    const { el, state } = await mount(files)
    state.sessionKey = 'session-b'
    await flush()
    expect(files.listDir.mock.calls[0]![5]!.aborted).toBe(true)
    if (outcome === 'resolve') previous.resolve(directoryPage([entry('old.txt')], 'old-binding'))
    else previous.reject(new Error('old directory failure'))
    await flush()
    expect(el.querySelector<HTMLButtonElement>('.workspace-files__refresh')!.disabled).toBe(true)
    expect(el.querySelector('[role="alert"]')).toBeNull()
    current.resolve(directoryPage([entry('new.txt')], 'new-binding'))
    await flush()
    await select(el, 'new.txt')
    expect(el.textContent).not.toContain('old.txt')
    expect(files.readPage.mock.calls[0]![0]).toBe('session-b')
    expect(files.readPage.mock.calls[0]![1].workspaceBinding).toBe('new-binding')
  })

  it('keeps refresh disabled while any directory request is still pending', async () => {
    const files = access()
    const first = deferred<WorkspaceDirectoryPage>()
    const second = deferred<WorkspaceDirectoryPage>()
    files.listDir.mockResolvedValueOnce(directoryPage([entry('first', true), entry('second', true)]))
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { el } = await mount(files)
    await select(el, 'first')
    await select(el, 'second')
    first.resolve(directoryPage([]))
    await flush()
    expect(el.querySelector<HTMLButtonElement>('.workspace-files__refresh')!.disabled).toBe(true)
    second.resolve(directoryPage([]))
    await flush()
    expect(el.querySelector<HTMLButtonElement>('.workspace-files__refresh')!.disabled).toBe(false)
  })

  it('abandons a pending copy before writing the clipboard after changing files', async () => {
    const files = access()
    const pendingCopy = deferred<Blob>()
    files.read.mockReturnValueOnce(pendingCopy.promise)
    const { el } = await mount(files)
    await select(el, 'a.txt')
    const copy = el.querySelector<HTMLButtonElement>('button[aria-label="Copy file contents"]')!
    copy.click()
    await select(el, 'b.txt')
    pendingCopy.resolve(new Blob(['old contents']))
    await flush()
    expect(files.read.mock.calls[0]![2]!.aborted).toBe(true)
    expect(clipboard).not.toHaveBeenCalled()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.txt')
  })

  it('aborts directory and preview work when the panel is unmounted', async () => {
    const files = access()
    const pendingDirectory = deferred<WorkspaceDirectoryPage>()
    const pendingPreview = deferred<WorkspaceFilePage>()
    files.listDir.mockResolvedValueOnce(directoryPage([entry('folder', true), entry('a.txt')])).mockReturnValueOnce(pendingDirectory.promise)
    files.resolve.mockResolvedValueOnce([{ requestedPath: 'a.txt', path: 'a.txt', name: 'a.txt', kind: 'text', mime: 'text/plain', size: 1000, workspaceBinding: 'binding-a' }])
    files.readPage.mockReturnValueOnce(pendingPreview.promise)
    const { el, unmount } = await mount(files)
    await select(el, 'folder')
    await select(el, 'a.txt')
    unmount()
    expect(files.listDir.mock.calls[1]![5]!.aborted).toBe(true)
    expect(files.readPage.mock.calls[0]![4]!.aborted).toBe(true)
    pendingDirectory.resolve(directoryPage([]))
    pendingPreview.resolve(upstreamPage())
    await flush()
    expect(el.childElementCount).toBe(0)
  })
})
