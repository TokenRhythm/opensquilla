import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import type { SessionDirectory, SessionItem } from '@/modules/sessionDirectory'
import type {
  WorkspaceDirectoryEntry,
  WorkspaceDirectoryPage,
  WorkspaceFiles,
} from '@/modules/workspaceFiles'

import { useChatMentions } from './useChatMentions'

const SESSION_KEY = 'agent:main:webchat:s1'
const NEXT_SESSION_KEY = 'agent:main:webchat:s2'
const BINDING = 'workspace-binding-1'

function entry(
  name: string,
  path: string,
  type: 'directory' | 'file',
): WorkspaceDirectoryEntry {
  return {
    name,
    path,
    type,
    kind: type === 'directory' ? 'directory' : 'text',
    mime: type === 'directory' ? 'inode/directory' : 'text/plain',
    size: type === 'directory' ? null : 128,
    mtimeNs: '1700000000000000000',
  }
}

const ROOT_ENTRIES = [entry('src', 'src', 'directory'), entry('README.md', 'README.md', 'file')]
const SRC_ENTRIES = [
  entry('lib', 'src/lib', 'directory'),
  entry('main.ts', 'src/main.ts', 'file'),
  entry('max.ts', 'src/max.ts', 'file'),
]

function page(relativePath: string, entries: WorkspaceDirectoryEntry[]): WorkspaceDirectoryPage {
  return {
    workspaceBinding: BINDING,
    relativePath,
    entries,
    nextCursor: null,
    hasMore: false,
    truncated: false,
  }
}

function session(key: string, title: string, groupLabel = 'Today'): SessionItem {
  return {
    key,
    title,
    subtitle: '',
    groupLabel,
    effectiveAgentId: 'main',
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'main',
    status: 'idle',
    runStatus: 'idle',
    runLabel: '',
    messageCount: 2,
    updatedAt: 1,
    model: 'test-model',
    parent: null,
    forkedFromParent: false,
    hasContractGaps: false,
  }
}

const SESSIONS = [
  session('agent:main:webchat:history-1', 'Fix login redirect', 'Yesterday'),
  session('agent:main:webchat:history-2', 'Release notes', 'Last week'),
]

/** Let the mocked RPCs, the refresh await, and nextTick drain. */
const flush = () => new Promise<void>(resolve => { setTimeout(resolve, 0) })

function harness(overrides: {
  workspaceFiles?: WorkspaceFiles | null
  sessionDirectory?: SessionDirectory | null
  withCaret?: boolean
} = {}) {
  const inputText = ref('')
  const sessionKey = ref(SESSION_KEY)
  let caret = 0
  const getCaret = vi.fn(() => caret)
  const setCaret = vi.fn((offset: number) => { caret = offset })
  const autoResizeTextarea = vi.fn()
  const listDir = vi.fn(async (
    _sessionKey: string,
    path = '',
    _cursor?: string,
    _limit?: number,
    _workspaceBinding?: string,
  ) => page(path, path === 'src' ? SRC_ENTRIES : ROOT_ENTRIES))
  const listPage = vi.fn(async () => ({ items: SESSIONS, hasMore: false, nextCursor: null }))
  const workspaceFiles = 'workspaceFiles' in overrides
    ? overrides.workspaceFiles ?? undefined
    : { listDir } as unknown as WorkspaceFiles
  const sessionDirectory = 'sessionDirectory' in overrides
    ? overrides.sessionDirectory ?? undefined
    : {
        listPage,
        count: vi.fn(async () => ({ value: SESSIONS.length, exact: true })),
        resolve: vi.fn(async () => ({ key: SESSION_KEY, id: 's1' })),
        search: vi.fn(async () => ({ sessions: [], messages: [] })),
      } as unknown as SessionDirectory

  const api = useChatMentions({
    inputText,
    sessionKey,
    workspaceFiles,
    sessionDirectory,
    getCaret: overrides.withCaret === false ? undefined : getCaret,
    setCaret,
    autoResizeTextarea,
  })

  /** Type a whole value with the caret at its end, then run the input handler. */
  function type(value: string) {
    inputText.value = value
    caret = value.length
    api.handleMentionInput()
  }

  return {
    api,
    inputText,
    sessionKey,
    listDir,
    listPage,
    getCaret,
    setCaret,
    autoResizeTextarea,
    type,
    caretOf: () => caret,
    ids: () => api.mentionItems.value.map(item => item.id),
    pick: (id: string) => {
      const candidate = api.mentionItems.value.find(item => item.id === id)
      expect(candidate).toBeTruthy()
      api.chooseMention(candidate!)
      return candidate!
    },
  }
}

describe('chat mentions', () => {
  it('opens on a bare @ with root directory entries and recent sessions', async () => {
    const h = harness()
    h.type('@')
    await flush()

    expect(h.api.mentionOpen.value).toBe(true)
    expect(h.api.mentionLoading.value).toBe(false)
    expect(h.api.mentionError.value).toBe('')
    expect(h.api.mentionIdx.value).toBe(0)
    expect(h.listDir).toHaveBeenCalledTimes(1)
    expect(h.listDir.mock.calls[0]!.slice(0, 4)).toEqual([SESSION_KEY, '', '0', 200])
    expect(h.listPage).toHaveBeenCalledTimes(1)
    expect(h.listPage).toHaveBeenCalledWith({ limit: 20 })

    expect(h.ids()).toEqual(expect.arrayContaining([
      'workspace:src',
      'workspace:README.md',
      'session:agent:main:webchat:history-1',
      'session:agent:main:webchat:history-2',
    ]))
    // Workspace candidates lead, session candidates follow.
    expect(h.ids().indexOf('session:agent:main:webchat:history-1'))
      .toBeGreaterThan(h.ids().indexOf('workspace:README.md'))

    const directory = h.api.mentionItems.value.find(item => item.id === 'workspace:src')!
    expect(directory.kind).toBe('workspace_directory')
    expect(directory.label).toBe('src/')
    expect(directory.insertion).toBe('@src/')
    expect(directory.detail).toBe('src')
    expect(directory.path).toBe('src')

    const file = h.api.mentionItems.value.find(item => item.id === 'workspace:README.md')!
    expect(file.kind).toBe('workspace_file')
    expect(file.label).toBe('README.md')
    expect(file.insertion).toBe('@README.md')

    const recent = h.api.mentionItems.value.find(item => item.kind === 'session')!
    expect(recent.label).toBe('Fix login redirect')
    expect(recent.detail).toBe('Yesterday')
    expect(recent.sessionKey).toBe('agent:main:webchat:history-1')
  })

  it('lists only the src directory for @src/ma and filters it by prefix', async () => {
    const h = harness()
    h.type('@src/ma')
    await flush()

    expect(h.listDir).toHaveBeenCalledTimes(1)
    expect(h.listDir.mock.calls[0]![1]).toBe('src')
    expect(h.ids()).toContain('workspace:src/main.ts')
    expect(h.ids()).toContain('workspace:src/max.ts')
    expect(h.ids()).not.toContain('workspace:src/lib')
    expect(h.ids().some(id => id.startsWith('session:'))).toBe(false)
    expect(h.api.mentionOpen.value).toBe(true)
  })

  it('lists each directory once and reuses the reported workspace binding', async () => {
    const h = harness()
    h.type('@')
    await flush()
    h.type('@s')
    await flush()
    // Same root directory: the cached listing answers the second keystroke.
    expect(h.listDir).toHaveBeenCalledTimes(1)
    expect(h.listDir.mock.calls[0]![4]).toBeUndefined()

    h.type('@src/')
    await flush()
    expect(h.listDir).toHaveBeenCalledTimes(2)
    expect(h.listDir.mock.calls[1]![1]).toBe('src')
    expect(h.listDir.mock.calls[1]![4]).toBe(BINDING)
  })

  it('reopens on the chosen directory so the palette drills into its children', async () => {
    const h = harness()
    h.type('@sr')
    await flush()
    h.pick('workspace:src')

    expect(h.inputText.value).toBe('@src/ ')
    // The caret stays inside the token, before the separator space, or the
    // reopened palette would immediately close on whitespace.
    expect(h.caretOf()).toBe('@src/'.length)
    expect(h.autoResizeTextarea).toHaveBeenCalledTimes(1)
    expect(h.setCaret).toHaveBeenCalledWith('@src/'.length)

    await nextTick()
    await flush()

    expect(h.listDir.mock.calls.map(call => call[1])).toEqual(['', 'src'])
    expect(h.api.mentionOpen.value).toBe(true)
    expect(h.api.mentionLoading.value).toBe(false)
    // The empty fragment lists the whole directory level; sessions still trail
    // the workspace entries of the newly opened token.
    const kinds = h.api.mentionItems.value.map(item => item.kind)
    const workspaceKinds = kinds.filter(kind => kind.startsWith('workspace'))
    expect(workspaceKinds).toEqual(['workspace_directory', 'workspace_file', 'workspace_file'])
    expect(kinds.slice(workspaceKinds.length).every(kind => kind === 'session')).toBe(true)
    expect(h.api.mentionItems.value
      .filter(item => item.kind !== 'session')
      .map(item => item.label)).toEqual(['lib/', 'main.ts', 'max.ts'])
  })

  it('replaces the token with a file mention and closes the palette', async () => {
    const h = harness()
    h.type('@src/ma')
    await flush()
    h.pick('workspace:src/main.ts')

    expect(h.inputText.value).toBe('@src/main.ts ')
    expect(h.caretOf()).toBe('@src/main.ts '.length)
    expect(h.autoResizeTextarea).toHaveBeenCalledTimes(1)
    expect(h.api.mentionOpen.value).toBe(false)
    expect(h.api.mentionItems.value).toEqual([])

    await nextTick()
    await flush()
    // A file choice ends the mention: no follow-up listing for the file path.
    expect(h.listDir.mock.calls.map(call => call[1])).toEqual(['src'])
  })

  it('writes a session mention with the session key suffix', async () => {
    const h = harness()
    h.type('@fix')
    await flush()

    const sessionId = 'session:agent:main:webchat:history-1'
    expect(h.ids()).toEqual([sessionId])
    h.pick(sessionId)

    expect(h.inputText.value).toBe('@Fix login redirect (session:agent:main:webchat:history-1) ')
    expect(h.caretOf()).toBe(h.inputText.value.length)
    expect(h.api.mentionOpen.value).toBe(false)
  })

  it('keeps the palette closed for an @ inside prose', async () => {
    const h = harness()
    h.type('a@b.com')

    expect(h.api.mentionOpen.value).toBe(false)
    expect(h.api.mentionItems.value).toEqual([])
    expect(h.listDir).not.toHaveBeenCalled()
    expect(h.listPage).not.toHaveBeenCalled()
  })

  it('reports a workspace failure without dropping the session candidates', async () => {
    const h = harness()
    h.listDir.mockRejectedValueOnce(new Error('workspace offline'))
    h.type('@')
    await flush()

    expect(h.api.mentionError.value).toBe(i18n.global.t('chat.mention.unavailable'))
    expect(h.api.mentionOpen.value).toBe(true)
    expect(h.api.mentionLoading.value).toBe(false)
    expect(h.ids()).toEqual([
      'session:agent:main:webchat:history-1',
      'session:agent:main:webchat:history-2',
    ])

    // The failed directory is not cached: the next keystroke retries it.
    h.type('@s')
    await flush()
    expect(h.listDir).toHaveBeenCalledTimes(2)
    expect(h.ids()).toContain('workspace:src')
    expect(h.api.mentionError.value).toBe('')
  })

  it('closes the palette and clears the caches when the session changes', async () => {
    const h = harness()
    h.type('@')
    await flush()
    expect(h.api.mentionOpen.value).toBe(true)

    h.sessionKey.value = NEXT_SESSION_KEY
    await nextTick()

    expect(h.api.mentionOpen.value).toBe(false)
    expect(h.api.mentionItems.value).toEqual([])

    h.type('@')
    await flush()
    expect(h.listDir).toHaveBeenCalledTimes(2)
    expect(h.listDir.mock.calls[1]![0]).toBe(NEXT_SESSION_KEY)
    expect(h.listPage).toHaveBeenCalledTimes(2)
    expect(h.api.mentionOpen.value).toBe(true)
  })

  it('drops a listing that resolves after the palette already closed', async () => {
    const h = harness()
    let release!: (value: WorkspaceDirectoryPage) => void
    h.listDir.mockReturnValueOnce(new Promise<WorkspaceDirectoryPage>((resolve) => {
      release = resolve
    }))

    h.type('@')
    expect(h.api.mentionOpen.value).toBe(true)
    expect(h.api.mentionLoading.value).toBe(true)

    h.type('hello@')
    h.type('hello @ world')
    expect(h.api.mentionOpen.value).toBe(false)
    expect(h.api.mentionLoading.value).toBe(false)

    release(page('', ROOT_ENTRIES))
    await flush()

    expect(h.api.mentionItems.value).toEqual([])
    expect(h.api.mentionOpen.value).toBe(false)
    expect(h.api.mentionLoading.value).toBe(false)
  })

  it('still works without an injected workspace or session directory', async () => {
    const h = harness({ workspaceFiles: null, sessionDirectory: null })
    h.type('@')
    await flush()

    expect(h.api.mentionOpen.value).toBe(true)
    expect(h.api.mentionItems.value).toEqual([])
    expect(h.api.mentionError.value).toBe('')
    expect(h.api.mentionLoading.value).toBe(false)
  })
})
