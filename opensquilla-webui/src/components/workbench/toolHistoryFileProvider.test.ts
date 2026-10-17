import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyTextWithFallback, downloadText } from '@/utils/browser'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import { createToolHistoryFileItem } from '@/workbench/toolHistoryFileItems'
import { createResolvedWorkspaceFileItem } from '@/workbench/workspaceFileItems'
import { WorkbenchPanelRegistry } from '@/workbench/runtime'
import type { WorkbenchPanelRenderState } from '@/workbench/types'
import { createToolHistoryFileDefinition } from './toolHistoryFileProvider'
import { createWorkspaceFileDefinition } from './workspaceFileProvider'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn().mockResolvedValue(undefined), downloadText: vi.fn() }))

async function setup(contentAccess?: ArtifactContentAccess) {
  const state: Record<string, unknown> = {}
  const item = createToolHistoryFileItem({ kind: 'read', path: 'src/a.ts', content: 'historical text' },
    { sessionKey: 'session-a', toolId: 'read-one' })!
  const definition = createToolHistoryFileDefinition(key => key, contentAccess)
  const runtime = await definition.createRuntime!(item, {
    getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true,
    setExpanded: vi.fn(), reportError: vi.fn(),
  })
  const renderState = (): WorkbenchPanelRenderState => ({ active: true, hostAvailable: true, nativeSurface: false, runtimeState: state })
  return { state, item, definition, runtime, renderState }
}

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

describe('historical file provider', () => {
  it('downloads the complete inline record while the workbench renders a bounded prefix', async () => {
    const { definition, runtime, renderState } = await setup()
    const content = 'retained text\n'.repeat(3000) + 'LAST LINE'
    const item = createToolHistoryFileItem({ kind: 'write', path: 'large.txt', content },
      { sessionKey: 'session-a', toolId: 'large' })!
    expect(definition.getProps!(item, renderState()).view).toMatchObject({ truncated: true })
    expect(definition.getToolbarItems!(item, renderState())[1]).toMatchObject({ id: 'download', label: 'shared.runTrace.downloadFullContent' })
    const copies = vi.mocked(copyTextWithFallback).mock.calls.length
    runtime.performAction!('copy', item)
    expect(vi.mocked(copyTextWithFallback).mock.calls).toHaveLength(copies)
    runtime.performAction!('download', item)
    await vi.waitFor(() => expect(downloadText).toHaveBeenCalledWith('large.txt', 'text/plain;charset=utf-8', content))
    runtime.dispose!('closed')
  })

  it('downloads the original session attachment rather than the preview or current file', async () => {
    const downloadAttachment = vi.fn().mockResolvedValue('saved')
    const { definition, runtime, renderState } = await setup({ downloadAttachment } as unknown as ArtifactContentAccess)
    const fileSnapshot = { kind: 'edit' as const, path: 'a.ts', content: '', sha256_ref: 'a'.repeat(64), bytes: 8_000_000 }
    const item = createToolHistoryFileItem({ kind: 'edit', path: 'a.ts', content: '+preview', truncated: true },
      { sessionKey: 'session-a', toolId: 'large', fileSnapshot })!
    runtime.performAction!('download', item)
    await vi.waitFor(() => expect(downloadAttachment).toHaveBeenCalledWith(expect.objectContaining({ name: 'a.ts.diff', sha256_ref: fileSnapshot.sha256_ref }), { sessionKey: 'session-a' }))
    expect(definition.getProps!(item, renderState()).view).toMatchObject({ content: '+preview', truncated: true })
    runtime.dispose!('closed')
  })
  it('downloads a child record with child authority while presenting it under the parent', async () => {
    const downloadAttachment = vi.fn().mockResolvedValue('saved')
    const { runtime } = await setup({ downloadAttachment } as unknown as ArtifactContentAccess)
    const fileSnapshot = { kind: 'write' as const, path: 'a.ts', content: '', sha256_ref: 'b'.repeat(64), bytes: 8_000_000 }
    const item = createToolHistoryFileItem({ kind: 'write', path: 'a.ts', content: 'preview', truncated: true },
      { sessionKey: 'child', ownerSessionKey: 'parent', toolId: 'child-read', fileSnapshot })!
    expect(item.scope).toEqual({ type: 'session', id: 'parent' })
    runtime.performAction!('download', item)
    await vi.waitFor(() => expect(downloadAttachment).toHaveBeenCalledWith(
      expect.objectContaining({ sha256_ref: fileSnapshot.sha256_ref }), { sessionKey: 'child' },
    ))
    runtime.dispose!('closed')
  })
  it('coexists with current workspace files and renders the supplied record without filesystem requests', async () => {
    const { definition, item, renderState, runtime } = await setup()
    const read = vi.fn()
    const current = createWorkspaceFileDefinition(null, { read, resolve: vi.fn() }, key => key)
    const registry = new WorkbenchPanelRegistry()
    registry.register(current)
    registry.register(definition)
    const file = { path: 'src/a.ts', requestedPath: 'src/a.ts', name: 'a.ts', kind: 'text' as const,
      mime: 'text/typescript', size: 15, workspaceBinding: 'workspace-a' }
    expect(registry.resolve(createResolvedWorkspaceFileItem('session-a', file))).toBe(current)
    expect(registry.resolve(item)).toBe(definition)
    expect(definition.getProps!(item, renderState()).view).toMatchObject({ content: 'historical text' })
    runtime.performAction!('copy', item)
    await vi.waitFor(() => expect(copyTextWithFallback).toHaveBeenCalledWith('historical text'))
    expect(read).not.toHaveBeenCalled()
    runtime.dispose!('closed')
  })

  it('changes only wrapping and copies the complete recorded body, including an empty file', async () => {
    const { item, state, definition, runtime, renderState } = await setup()
    runtime.performAction!('wrap', item)
    expect(state.wrapLines).toBe(true)
    expect(definition.getToolbarItems!(item, renderState())[0]).toMatchObject({ pressed: true, label: 'chat.toolModal.preserveLines' })
    const empty = createToolHistoryFileItem({ kind: 'write', path: 'empty.txt', content: '' },
      { sessionKey: 'session-a', toolId: 'empty-one' })!
    runtime.performAction!('copy', empty)
    await vi.waitFor(() => expect(state.copyState).toBe('copied'))
    expect(copyTextWithFallback).toHaveBeenLastCalledWith('')
    runtime.dispose!('closed')
  })

  it('ignores late copy completion after disposal and clears feedback timers', async () => {
    vi.useFakeTimers()
    let resolve!: () => void
    vi.mocked(copyTextWithFallback).mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { item, state, runtime } = await setup()
    runtime.performAction!('copy', item)
    runtime.dispose!('closed')
    resolve()
    await Promise.resolve()
    expect(state.copyState).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('reports clipboard failure without implying that the recorded file operation failed', async () => {
    vi.mocked(copyTextWithFallback).mockRejectedValueOnce(new Error('clipboard denied'))
    const { item, state, definition, runtime, renderState } = await setup()
    runtime.performAction!('copy', item)
    await vi.waitFor(() => expect(state.copyState).toBe('error'))
    expect(definition.getToolbarItems!(item, renderState())[1]).toMatchObject({ label: 'workspaceReference.copyFailed' })
    expect(definition.getProps!(item, renderState()).view).not.toHaveProperty('error')
    runtime.dispose!('closed')
  })
})
