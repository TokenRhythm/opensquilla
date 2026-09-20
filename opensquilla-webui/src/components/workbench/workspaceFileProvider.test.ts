import { describe, expect, it, vi } from 'vitest'
import { WorkspaceReferenceError, type WorkspaceReferences } from '@/modules/workspaceReferences'
import { normalizeWorkspaceFileReferenceV1 } from '@/types/references'
import { createWorkspaceFileItem } from '@/workbench/workspaceFileItems'
import type { WorkbenchItem } from '@/workbench/types'
import { createWorkspaceFileDefinition } from './workspaceFileProvider'

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
    expect(state()).toEqual({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
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
    expect(state()).toEqual({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
    finish({ content: 'obsolete source' })
    await Promise.resolve()
    expect(state()).toEqual({ loading: false, snapshot: null, errorKey: 'workspaceReference.invalid' })
  })
})
