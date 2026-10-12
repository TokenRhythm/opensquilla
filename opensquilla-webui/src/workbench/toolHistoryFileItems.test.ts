// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { ActivityToolCodeView } from '@/utils/chat/activityToolDetails'
import { WORKBENCH_WIDTH_STORAGE_KEY } from './layout'
import { useWorkbenchStore } from './store'
import { canOpenToolHistoryFile, createToolHistoryFileItem, openToolHistoryFile, toolHistoryFileFromItem } from './toolHistoryFileItems'

const settings = vi.hoisted(() => ({ features: { artifactWorkbench: true } }))
vi.mock('@/stores/app', () => ({ useAppStore: () => settings }))
const view: ActivityToolCodeView = { kind: 'write', path: 'src/example.ts', content: 'recorded content' }
const options = { sessionKey: 'session-a', toolId: 'call-one' }

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  settings.features.artifactWorkbench = true
})

describe('recorded tool file Workbench entries', () => {
  it('bounds the displayed record while retaining the immutable download source', () => {
    const content = 'a retained line\n'.repeat(3000) + 'LAST LINE'
    const item = createToolHistoryFileItem({ ...view, content }, options)!
    expect(toolHistoryFileFromItem(item)?.content).not.toContain('LAST LINE')
    expect(toolHistoryFileFromItem(item)?.truncated).toBe(true)
    expect(item.payload.fullContent).toBe(content)
    const fileSnapshot = { kind: 'write' as const, path: view.path!, content: '', sha256_ref: 'a'.repeat(64), bytes: 8_000_000 }
    const referenced = createToolHistoryFileItem({ ...view, content: 'small preview', truncated: true }, { ...options, fileSnapshot })!
    expect(referenced.payload.fileSnapshot).toEqual(fileSnapshot)
    expect(referenced.payload).not.toHaveProperty('fullContent')
  })
  it('opens distinct calls to the same file while deduplicating the same recorded operation', () => {
    expect(openToolHistoryFile(view, options)).toBe(true)
    expect(openToolHistoryFile({ ...view }, options)).toBe(true)
    expect(openToolHistoryFile({ ...view, content: 'later record' }, { ...options, toolId: 'call-two' })).toBe(true)
    const store = useWorkbenchStore()
    expect(store.items).toHaveLength(2)
    expect(toolHistoryFileFromItem(store.items[0]!)?.content).toBe('recorded content')
    expect(toolHistoryFileFromItem(store.items[1]!)?.content).toBe('later record')
    const firstMessage = createToolHistoryFileItem(view, { ...options, recordKey: 'message-one:tool:call-one:0' })!
    const laterMessage = createToolHistoryFileItem(view, { ...options, recordKey: 'message-two:tool:call-one:0' })!
    expect(firstMessage.id).not.toBe(laterMessage.id)
  })

  it('keeps open resources and saved width untouched and never persists recorded content', () => {
    const store = useWorkbenchStore()
    store.openItem({ id: 'preview', kind: 'artifact-preview', title: 'preview.html',
      scope: { type: 'session', id: 'session-a' }, hostKind: 'dom', retention: 'keep-alive', payload: {} })
    store.setWidth(614)
    const preference = localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)
    openToolHistoryFile(view, options)
    expect(store.items.map(item => item.id)).toContain('preview')
    expect(store.widthPreference.width).toBe(614)
    expect(localStorage.length).toBe(1)
    expect(localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)).toBe(preference)
    expect(preference).not.toContain('recorded content')
  })

  it('disposes history on session change and refuses an action from a stale session or disabled host', () => {
    openToolHistoryFile(view, options)
    const store = useWorkbenchStore()
    const item = store.activeItem!
    expect(item).toMatchObject({ scope: { type: 'session', id: 'session-a' }, retention: 'dispose-on-suspend', hostKind: 'dom' })
    store.setSessionScope('session-b')
    expect(store.items).toHaveLength(0)
    expect(openToolHistoryFile(view, options)).toBe(false)
    settings.features.artifactWorkbench = false
    expect(openToolHistoryFile(view, { ...options, sessionKey: 'session-b' })).toBe(false)
    expect(store.items).toHaveLength(0)
  })

  it('accepts recorded empty source files but rejects missing content, commands, failures and empty diffs', () => {
    expect(canOpenToolHistoryFile({ kind: 'read', path: 'empty.txt', content: '' })).toBe(true)
    expect(canOpenToolHistoryFile({ kind: 'write', path: 'empty.txt', content: '' })).toBe(true)
    for (const unsupported of [
      { ...view, unavailable: true }, { ...view, error: true }, { ...view, content: undefined },
      { ...view, content: '[historical_tool_argument_omitted]\nfield: content\noriginal_chars: 13332' },
      { ...view, path: undefined }, { kind: 'edit' as const, path: 'a.ts', content: '' },
      { kind: 'command' as const, path: 'a.ts', content: 'shell output', command: 'cat a.ts' },
    ]) expect(createToolHistoryFileItem(unsupported, options)).toBeNull()
    expect(createToolHistoryFileItem(view, { ...options, sessionKey: '' })).toBeNull()
    expect(createToolHistoryFileItem(view, { ...options, toolId: '' })).toBeNull()
  })

  it('copies only the recorded display snapshot and retains its partial-content boundary', () => {
    const original: ActivityToolCodeView = { ...view, partial: true }
    const item = createToolHistoryFileItem(original, options)!
    original.content = 'changed in caller'
    expect(toolHistoryFileFromItem(item)).toEqual({ ...view, partial: true })
    expect(item.payload).not.toHaveProperty('file')
    expect(item.payload).not.toHaveProperty('reference')
    expect(toolHistoryFileFromItem({ ...item, kind: 'file' })).toBeNull()
    expect(toolHistoryFileFromItem({ ...item, scope: { type: 'app' } })).toBeNull()
  })
})
