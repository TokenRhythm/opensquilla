// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { WORKSPACE_FILES_KEY, type WorkspaceDirectoryEntry, type WorkspaceDirectoryPage, type WorkspaceFilePage, type WorkspaceFiles, type WorkspaceFileSearch } from '@/modules/workspaceFiles'
import { copyTextWithFallback } from '@/utils/browser'
import WorkspaceFilesPanel from './WorkspaceFilesPanel.vue'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn(async () => true) }))

const cleanups: Array<() => void> = []
beforeEach(() => {
  // Happy DOM has no layout; give the real virtualizer a visible viewport.
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(320)
})
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks(); vi.clearAllMocks() })

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise })
  return { promise, resolve, reject }
}

async function flush() {
  for (let index = 0; index < 5; index++) await nextTick()
}

function entry(path: string, directory = false): WorkspaceDirectoryEntry {
  return { path, name: path, type: directory ? 'directory' : 'file', kind: directory ? 'directory' : 'text', mime: directory ? '' : 'text/plain', size: 1000, mtimeNs: '1' }
}

function directoryPage(entries = [entry('a.txt'), entry('b.txt')], workspaceBinding = 'binding-a'): WorkspaceDirectoryPage {
  return { workspaceBinding, relativePath: '', entries, hasMore: false, nextCursor: null, truncated: false }
}

function page(path = 'a.txt', startLine = 1, content = `${path} page ${startLine}`): WorkspaceFilePage {
  return { relativePath: path, content, startLine, endLine: startLine + 199, totalLines: 600 }
}

function access() {
  return {
    resolve: vi.fn<WorkspaceFiles['resolve']>(async () => []),
    read: vi.fn<WorkspaceFiles['read']>(async () => new Blob(['contents'])),
    readPage: vi.fn<NonNullable<WorkspaceFiles['readPage']>>(async (_session, file, start) => page(file.path, start)),
    search: vi.fn<NonNullable<WorkspaceFiles['search']>>(async (_session, file) => ({ relativePath: file.path, totalLines: 600, matchLine: 201 })),
    listDir: vi.fn<NonNullable<WorkspaceFiles['listDir']>>(async () => directoryPage()),
  }
}

async function mount(files: WorkspaceFiles) {
  const el = document.createElement('div')
  document.body.append(el)
  const state = reactive({ sessionKey: 'session-a' })
  const app = createApp({ render: () => h(WorkspaceFilesPanel, state) })
  app.provide(WORKSPACE_FILES_KEY, files)
  app.use(i18n).mount(el)
  let mounted = true
  const unmount = () => { if (mounted) app.unmount(); mounted = false; el.remove() }
  cleanups.push(unmount)
  await flush()
  return { el, state, unmount }
}

async function select(el: HTMLElement, path: string) {
  const label = [...el.querySelectorAll<HTMLElement>('.workspace-files__name')].find(node => node.title === path)
  expect(label, `tree entry ${path}`).toBeDefined()
  label!.click()
  await flush()
}

async function nextPage(el: HTMLElement) {
  el.querySelector<HTMLButtonElement>('.workspace-file__more')!.click()
  await flush()
}

async function query(el: HTMLElement, value: string, submit = true) {
  const input = el.querySelector<HTMLInputElement>('.workspace-file input[type="search"]')!
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
    expect(el.querySelectorAll('[aria-expanded="true"]')).toHaveLength(2)
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
    if (outcome === 'resolve') pendingPage.resolve(page('a.txt', 201, 'stale page'))
    else pendingPage.reject(new Error('old failure'))
    await flush()
    expect(el.querySelector('.workspace-file [role="status"]')).not.toBeNull()
    expect(el.querySelector('.workspace-file pre')).toBeNull()
    expect(el.querySelector('[role="alert"]')).toBeNull()
    nextSelection.resolve(page(nextPath, 1, 'current selection'))
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
    if (outcome === 'resolve') searchPage.resolve(page('a.txt', 201, 'stale search page'))
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
    oldPage.resolve(page('a.txt', 201, 'old query match'))
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
    pendingPage.resolve(page('a.txt', 201))
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
    const copy = [...el.querySelectorAll<HTMLButtonElement>('.workspace-file__tool')].find(button => button.textContent?.includes('Copy file contents'))!
    copy.click()
    await select(el, 'b.txt')
    pendingCopy.resolve(new Blob(['old contents']))
    await flush()
    expect(files.read.mock.calls[0]![2]!.aborted).toBe(true)
    expect(copyTextWithFallback).not.toHaveBeenCalled()
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('b.txt')
  })

  it('aborts directory and preview work when the panel is unmounted', async () => {
    const files = access()
    const pendingDirectory = deferred<WorkspaceDirectoryPage>()
    const pendingPreview = deferred<WorkspaceFilePage>()
    files.listDir.mockResolvedValueOnce(directoryPage([entry('folder', true), entry('a.txt')])).mockReturnValueOnce(pendingDirectory.promise)
    files.readPage.mockReturnValueOnce(pendingPreview.promise)
    const { el, unmount } = await mount(files)
    await select(el, 'folder')
    await select(el, 'a.txt')
    unmount()
    expect(files.listDir.mock.calls[1]![5]!.aborted).toBe(true)
    expect(files.readPage.mock.calls[0]![4]!.aborted).toBe(true)
    pendingDirectory.resolve(directoryPage([]))
    pendingPreview.resolve(page())
    await flush()
    expect(el.childElementCount).toBe(0)
  })
})
