import { describe, expect, it, vi } from 'vitest'
import { WorkspaceReferenceError, type WorkspaceReferences } from '@/modules/workspaceReferences'
import { normalizeWorkspaceFileReferenceV1 } from '@/types/references'
import { createResolvedWorkspaceFileItem, createWorkspaceFileItem } from '@/workbench/workspaceFileItems'
import type { WorkspaceFile, WorkspaceFiles } from '@/modules/workspaceFiles'
import { copyTextWithFallback } from '@/utils/browser'
import type { WorkbenchItem } from '@/workbench/types'
import { WorkbenchPanelRegistry, WorkbenchRuntimeManager } from '@/workbench/runtime'
import { createWorkspaceFileDefinition } from './workspaceFileProvider'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn().mockResolvedValue(undefined) }))

const reference = normalizeWorkspaceFileReferenceV1({
  version: 1, kind: 'workspace_file', id: 'a.py', label: 'a.py:1', scope: {},
  locator: { relativePath: 'a.py', startLine: 1, endLine: 1 },
  state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true },
})!
async function setup(read: WorkspaceReferences['read']) {
  let state: Record<string, unknown> = {}
  const item = createWorkspaceFileItem('task', reference)
  const definition = createWorkspaceFileDefinition({ read }, key => key)
  const runtime = await definition.createRuntime!(item, {
    getRenderState: () => state,
    updateRenderState: patch => { state = { ...state, ...patch } },
    isItemOpen: () => true, setExpanded: vi.fn(), reportError: vi.fn(),
  })
  return { item, runtime, state: () => state }
}
describe('workspace file runtime', () => {
  it('loads a resolved workspace text file with line metadata', async () => {
    const file: WorkspaceFile = {
      requestedPath: '_img/build.py', path: '_img/build.py', name: 'build.py', mime: 'text/x-python',
      size: 14, kind: 'text', workspaceBinding: 'binding-A',
    }
    const state: Record<string, unknown> = {}
    const runtime = await createWorkspaceFileDefinition(null, {
      read: vi.fn().mockResolvedValue(new Blob(['one\ntwo\n'])),
      resolve: vi.fn(),
    }, key => key).createRuntime!(createResolvedWorkspaceFileItem('task', file), {
      getRenderState: () => state,
      updateRenderState: patch => Object.assign(state, patch),
      isItemOpen: () => true, setExpanded: vi.fn(), reportError: vi.fn(),
    })
    const item = createResolvedWorkspaceFileItem('task', file)
    await runtime.activate!(item)
    await vi.waitFor(() => expect(state.snapshot).toMatchObject({ relativePath: '_img/build.py', totalLines: 2, startLine: 1, endLine: 2 }))
    expect((state.snapshot as { content: string }).content).toBe('one\ntwo\n')
  })

  it('loads and switches bounded pages for a large resolved text file', async () => {
    const file: WorkspaceFile = {
      requestedPath: 'large.py', path: 'large.py', name: 'large.py', mime: 'text/x-python',
      size: 8 * 1024 * 1024, kind: 'text', workspaceBinding: 'binding-A', textPaging: true,
    }
    const state: Record<string, unknown> = {}
    const readPage = vi.fn(async (_session: string, _file: WorkspaceFile, startLine: number) => ({
      relativePath: 'large.py', content: `line ${startLine}\n`, totalLines: 5000,
      startLine, endLine: startLine,
    }))
    const runtime = await createWorkspaceFileDefinition(null, {
      read: vi.fn(), resolve: vi.fn(), readPage,
    }, key => key).createRuntime!(createResolvedWorkspaceFileItem('task', file), {
      getRenderState: () => state, updateRenderState: patch => Object.assign(state, patch),
      isItemOpen: () => true, setExpanded: vi.fn(), reportError: vi.fn(),
    })
    const item = createResolvedWorkspaceFileItem('task', file)
    await runtime.activate!(item)
    await vi.waitFor(() => expect(state.snapshot).toMatchObject({ startLine: 1, paged: true }))
    runtime.handleComponentEvent!({ type: 'workspace-file-page', payload: { startLine: 201 } }, item)
    await vi.waitFor(() => expect(state.snapshot).toMatchObject({ startLine: 201, paged: true }))
    expect(readPage).toHaveBeenCalledWith('task', file, 201, 400, expect.any(AbortSignal))
  })

  it('clears source on suspend and revalidates when resumed', async () => {
    const read = vi.fn().mockResolvedValue({ content: 'old text' })
    const { item, runtime, state } = await setup(read)
    await runtime.activate!(item)
    expect(state().snapshot).toEqual({ content: 'old text' })
    runtime.suspend!(item)
    expect(state().snapshot).toBeNull()
    read.mockRejectedValue(new WorkspaceReferenceError('STALE_REFERENCE'))
    await runtime.resume!(item)
    expect(state().snapshot).toBeNull()
    expect(state().errorKey).toBe('workspaceReference.stale')
  })
  it('cannot restore source after a pending request is disposed', async () => {
    let finish!: (value: unknown) => void
    const { item, runtime, state } = await setup(vi.fn<WorkspaceReferences['read']>(() => new Promise(resolve => { finish = resolve as typeof finish })))
    const loading = runtime.activate!(item)
    runtime.dispose!('closed')
    finish({ content: 'late text' })
    await loading
    expect(state().snapshot).toBeNull()
    expect(state().loading).toBe(false)
  })
  it('aborts a pending load and starts a fresh request immediately on resume', async () => {
    const pending: { finish: (value: unknown) => void; signal?: AbortSignal }[] = []
    const read = vi.fn<WorkspaceReferences['read']>((_key, _reference, signal) => new Promise(resolve => {
      pending.push({ finish: resolve as (value: unknown) => void, signal })
    }))
    const { item, runtime, state } = await setup(read)
    runtime.activate!(item)
    expect(state().loading).toBe(true)
    runtime.suspend!(item)
    expect(pending[0]!.signal?.aborted).toBe(true)
    expect(state()).toMatchObject({ loading: false, snapshot: null, errorKey: '' })

    runtime.resume!(item)
    expect(read).toHaveBeenCalledTimes(2)
    expect(state().loading).toBe(true)
    pending[0]!.finish({ content: 'obsolete source' })
    await Promise.resolve()
    expect(state()).toMatchObject({ loading: true, snapshot: null })
    pending[1]!.finish({ content: 'current source' })
    await Promise.resolve()
    expect(state()).toMatchObject({ loading: false, snapshot: { content: 'current source' } })
  })
  it.each([
    { payload: {} },
    { scope: { type: 'app' } },
  ] satisfies Partial<WorkbenchItem>[])('leaves an invalid first descriptor idle with a clear error: %s', async patch => {
    const read = vi.fn<WorkspaceReferences['read']>()
    const { item, runtime, state } = await setup(read)
    runtime.activate!({ ...item, ...patch })
    expect(read).not.toHaveBeenCalled()
    expect(state()).toMatchObject({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
  })
  it('cancels a pending request when replaced by an invalid descriptor', async () => {
    let finish!: (value: unknown) => void
    let signal: AbortSignal | undefined
    const { item, runtime, state } = await setup(vi.fn<WorkspaceReferences['read']>((_key, _reference, inputSignal) => {
      signal = inputSignal
      return new Promise(resolve => { finish = resolve as typeof finish })
    }))
    runtime.activate!(item)
    runtime.update!({ ...item, payload: {} })
    expect(signal?.aborted).toBe(true)
    expect(state()).toMatchObject({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
    finish({ content: 'obsolete source' })
    await Promise.resolve()
    expect(state()).toMatchObject({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
  })
})

const file: WorkspaceFile = {
  requestedPath: 'large.py', path: 'large.py', name: 'large.py', mime: 'text/x-python',
  size: 8 * 1024 * 1024, kind: 'text', workspaceBinding: 'binding-A', textPaging: true,
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(finish => { resolve = finish })
  return { promise, resolve }
}
async function setupFile(overrides: Partial<WorkspaceFiles> = {}, inputFile = file) {
  const state: Record<string, unknown> = {}
  const read = vi.fn().mockResolvedValue(new Blob(['entire file\nincluding other pages\n']))
  const readPage = vi.fn(async (_session: string, _file: WorkspaceFile, startLine: number) => ({
    relativePath: inputFile.path,
    content: Array.from({ length: Math.min(200, 450 - startLine + 1) }, (_, i) => `line ${startLine + i}`).join('\n'),
    startLine, endLine: Math.min(startLine + 199, 450), totalLines: 450,
  }))
  const access: WorkspaceFiles = { read, readPage, resolve: vi.fn(), ...overrides }
  const item = createResolvedWorkspaceFileItem('task', inputFile)
  const runtime = await createWorkspaceFileDefinition(null, access, key => key).createRuntime!(item, {
    getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true, setExpanded: vi.fn(), reportError: vi.fn(),
  })
  runtime.activate!(item)
  await vi.waitFor(() => expect(state.loading).toBe(false))
  const event = (type: string, payload?: unknown) => runtime.handleComponentEvent!({ type, payload }, item)
  return { runtime, state, item, read, readPage, event }
}

describe('paged source operations', () => {
  it('uses one full-file search request then fetches and focuses only the matched page', async () => {
    const pending = deferred<{ relativePath: string; totalLines: number; matchLine: number | null }>()
    const search = vi.fn(() => pending.promise)
    const { state, readPage, event } = await setupFile({ search })
    const searching = event('workspace-file-search', { query: ' line 445 ' })
    expect(state).toMatchObject({ searchStatus: 'searching', searchQuery: 'line 445' })
    expect(readPage).toHaveBeenCalledTimes(1)
    pending.resolve({ relativePath: file.path, totalLines: 450, matchLine: 445 })
    await searching
    expect(search).toHaveBeenCalledWith('task', file, 'line 445', expect.any(AbortSignal))
    expect(readPage).toHaveBeenLastCalledWith('task', file, 401, 600, expect.any(AbortSignal))
    await vi.waitFor(() => expect(state).toMatchObject({ searchStatus: 'found', snapshot: { startLine: 401, endLine: 450, focusLine: 445 } }))
  })

  it('reports no match only after the full-file search finishes and never refetches a page', async () => {
    const pending = deferred<{ relativePath: string; totalLines: number; matchLine: null }>()
    const { state, readPage, event } = await setupFile({ search: () => pending.promise })
    const searching = event('workspace-file-search', { query: 'absent' })
    expect(state.searchStatus).toBe('searching')
    pending.resolve({ relativePath: file.path, totalLines: 450, matchLine: null })
    await searching
    expect(state.searchStatus).toBe('not-found')
    expect(readPage).toHaveBeenCalledTimes(1)
  })

  it('aborts stale searches when the query changes and ignores their late results', async () => {
    const pending = deferred<{ relativePath: string; totalLines: number; matchLine: number }>()
    const search = vi.fn<NonNullable<WorkspaceFiles['search']>>(() => pending.promise)
    const { state, readPage, event } = await setupFile({ search })
    const searching = event('workspace-file-search', { query: 'old' })
    event('workspace-file-search-cancel')
    expect(search.mock.calls[0]![3]?.aborted).toBe(true)
    pending.resolve({ relativePath: file.path, totalLines: 450, matchLine: 445 })
    await searching
    expect(state.searchStatus).toBe('idle')
    expect(readPage).toHaveBeenCalledTimes(1)
    expect(state.snapshot).toMatchObject({ startLine: 1 })
  })

  it('retains source with an explicit error when paged search is unavailable', async () => {
    const { state, event } = await setupFile()
    await event('workspace-file-search', { query: 'needle' })
    expect(state).toMatchObject({ searchStatus: 'idle', searchErrorKey: 'workspaceReference.unavailable', snapshot: { startLine: 1 } })
  })

  it('bounds page requests and focuses jumps inside the requested page', async () => {
    const { state, readPage, event } = await setupFile()
    await event('workspace-file-page', { startLine: 401, focusLine: 9999 })
    expect(state.snapshot).toMatchObject({ startLine: 401, endLine: 450, focusLine: 450 })
    await event('workspace-file-page', { startLine: 451 })
    await event('workspace-file-page', { startLine: -1 })
    expect(readPage).toHaveBeenCalledTimes(2)
  })

  it('never retries failed paged reads through the full-content endpoint', async () => {
    const read = vi.fn()
    const readPage = vi.fn().mockRejectedValue(new WorkspaceReferenceError('FILE_NOT_FOUND'))
    const { state, runtime, item } = await setupFile({ read, readPage }, { ...file, size: 20 })
    expect(state).toMatchObject({ snapshot: null, errorKey: 'workspaceReference.missing' })
    await runtime.resume!(item)
    expect(readPage).toHaveBeenCalledTimes(1)
    expect(read).not.toHaveBeenCalled()
  })

  it('copies the entire file through a cancellable read, not just its visible page', async () => {
    vi.mocked(copyTextWithFallback).mockClear()
    const { state, read, event } = await setupFile()
    await event('workspace-file-copy')
    expect(read).toHaveBeenCalledWith('task', file, expect.any(AbortSignal))
    await vi.waitFor(() => expect(copyTextWithFallback).toHaveBeenCalledWith('entire file\nincluding other pages\n'))
    await vi.waitFor(() => expect(state).toMatchObject({ copied: true, copying: false }))
  })

  it.each(['dispose', 'switch'] as const)('cancels full-copy reads on %s and never copies stale content', async action => {
    vi.mocked(copyTextWithFallback).mockClear()
    const pending = deferred<Blob>()
    const read = vi.fn<WorkspaceFiles['read']>(() => pending.promise)
    const { state, runtime, item, event } = await setupFile({ read })
    const copying = event('workspace-file-copy')
    if (action === 'dispose') runtime.dispose!('scope-changed')
    else await runtime.update!({ ...item, scope: { type: 'session', id: 'other-task' } })
    expect(read.mock.calls[0]![2]?.aborted).toBe(true)
    pending.resolve(new Blob(['old file']))
    await copying
    expect(copyTextWithFallback).not.toHaveBeenCalled()
    expect(state).toMatchObject({ copied: false, copying: false })
  })

  it('does not report late clipboard success after the panel is disposed', async () => {
    const pending = deferred<void>()
    vi.mocked(copyTextWithFallback).mockClear().mockImplementationOnce(() => pending.promise)
    const { state, runtime, event } = await setupFile()
    const copying = event('workspace-file-copy')
    await vi.waitFor(() => expect(copyTextWithFallback).toHaveBeenCalledTimes(1))
    expect(state.copying).toBe(true)
    runtime.dispose!('closed')
    pending.resolve()
    await copying
    expect(state).toMatchObject({ copied: false, copying: false })
  })

  it.each([new Uint8Array([0x61, 0, 0x62]), new Uint8Array([0xff, 0xfe])])('rejects binary/invalid UTF-8 fallback and full-copy contents', async bytes => {
    const read = vi.fn().mockResolvedValue(new Blob([bytes]))
    const legacy = await setupFile({ read }, { ...file, size: bytes.length, textPaging: undefined })
    expect(legacy.state).toMatchObject({ snapshot: null, errorKey: 'workspaceReference.unsupported' })
    vi.mocked(copyTextWithFallback).mockClear()
    const paged = await setupFile({ read })
    await paged.event('workspace-file-copy')
    expect(copyTextWithFallback).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(paged.state).toMatchObject({ copied: false, copying: false, copyErrorKey: 'workspaceReference.copyFailed' }))
  })

  it('keeps legacy hosts bounded while counting empty and CRLF files correctly', async () => {
    for (const [content, totalLines] of [['', 1], ['one\r\ntwo\r\n', 2]] as const) {
      const readPage = vi.fn()
      const { state } = await setupFile({ read: vi.fn().mockResolvedValue(new Blob([content])), readPage }, { ...file, size: 12, textPaging: undefined })
      expect(readPage).not.toHaveBeenCalled()
      expect(state.snapshot).toMatchObject({ content, totalLines, startLine: 1, endLine: totalLines })
    }
    const read = vi.fn()
    const { state } = await setupFile({ read }, { ...file, textPaging: undefined })
    expect(state.errorKey).toBe('workspaceReference.unsupported')
    expect(read).not.toHaveBeenCalled()
  })
})

async function setupManaged(overrides: Partial<WorkspaceFiles> = {}) {
  const registry = new WorkbenchPanelRegistry()
  const readPage = vi.fn().mockResolvedValue({ relativePath: file.path, content: 'first page', startLine: 1, endLine: 1, totalLines: 450 })
  registry.register(createWorkspaceFileDefinition(null, { resolve: vi.fn(), read: vi.fn(), readPage, ...overrides }, key => key))
  const manager = new WorkbenchRuntimeManager(registry)
  const item = createResolvedWorkspaceFileItem('task', file)
  manager.handle({ type: 'open', item })
  manager.handle({ type: 'activate', item })
  manager.handle({ type: 'resume', item })
  return { manager, item }
}

describe('source runtime manager cancellation', () => {
  it('does not queue suspension behind an unfinished initial page read', async () => {
    const pending = deferred<Awaited<ReturnType<NonNullable<WorkspaceFiles['readPage']>>>>()
    const readPage = vi.fn<NonNullable<WorkspaceFiles['readPage']>>(() => pending.promise)
    const { manager, item } = await setupManaged({ readPage })
    await vi.waitFor(() => expect(readPage).toHaveBeenCalledTimes(1))
    manager.handle({ type: 'suspend', item })
    await vi.waitFor(() => expect(readPage.mock.calls[0]![4]?.aborted).toBe(true))
    await manager.flush()
    expect(manager.hasRuntime(item.id)).toBe(false)
    pending.resolve({ relativePath: file.path, content: 'obsolete source', startLine: 1, endLine: 1, totalLines: 1 })
    await Promise.resolve()
    expect(manager.getRenderState(item.id).snapshot).toBeNull()
    await manager.disposeAll()
  })

  it('delivers query cancellation immediately while its server search is unresolved', async () => {
    const pending = deferred<Awaited<ReturnType<NonNullable<WorkspaceFiles['search']>>>>()
    const search = vi.fn<NonNullable<WorkspaceFiles['search']>>(() => pending.promise)
    const { manager, item } = await setupManaged({ search })
    await vi.waitFor(() => expect(manager.getRenderState(item.id).snapshot).toBeTruthy())
    manager.handleComponentEvent(item, { type: 'workspace-file-search', payload: { query: 'old query' } })
    await vi.waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    manager.handleComponentEvent(item, { type: 'workspace-file-search-cancel' })
    await vi.waitFor(() => expect(search.mock.calls[0]![3]?.aborted).toBe(true))
    await manager.flush()
    expect(manager.getRenderState(item.id).searchStatus).toBe('idle')
    pending.resolve({ relativePath: file.path, totalLines: 450, matchLine: 445 })
    await Promise.resolve()
    expect(manager.getRenderState(item.id).snapshot).toMatchObject({ startLine: 1 })
    await manager.disposeAll()
  })

  it('cancels a pending whole-file copy on suspension before the response is decoded or copied', async () => {
    vi.mocked(copyTextWithFallback).mockClear()
    const pending = deferred<Blob>()
    const read = vi.fn<WorkspaceFiles['read']>(() => pending.promise)
    const { manager, item } = await setupManaged({ read })
    await vi.waitFor(() => expect(manager.getRenderState(item.id).snapshot).toBeTruthy())
    manager.handleComponentEvent(item, { type: 'workspace-file-copy' })
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    manager.handle({ type: 'suspend', item })
    await vi.waitFor(() => expect(read.mock.calls[0]![2]?.aborted).toBe(true))
    await manager.flush()
    const blob = new Blob(['old file contents'])
    const decode = vi.spyOn(blob, 'arrayBuffer')
    pending.resolve(blob)
    await Promise.resolve()
    await Promise.resolve()
    expect(decode).not.toHaveBeenCalled()
    expect(copyTextWithFallback).not.toHaveBeenCalled()
    expect(manager.getRenderState(item.id)).toMatchObject({ snapshot: null, copying: false, copied: false })
    await manager.disposeAll()
  })

  it('rejects a file that grew beyond the legacy bound before decoding it', async () => {
    const blob = new Blob(['x'.repeat(2 * 1024 * 1024 + 1)])
    const decode = vi.spyOn(blob, 'arrayBuffer')
    const { state } = await setupFile({ read: vi.fn().mockResolvedValue(blob) }, { ...file, size: 10, textPaging: undefined })
    expect(state).toMatchObject({ snapshot: null, errorKey: 'workspaceReference.unsupported' })
    expect(decode).not.toHaveBeenCalled()
  })
})
