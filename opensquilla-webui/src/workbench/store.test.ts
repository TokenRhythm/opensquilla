// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { WORKBENCH_WIDTH_STORAGE_KEY } from './layout'
import { useWorkbenchStore } from './store'
import type { WorkbenchItem } from './types'

function item(
  id: string,
  scope: WorkbenchItem['scope'] = { type: 'session', id: 'session-a' },
  retention: WorkbenchItem['retention'] = 'keep-alive',
): WorkbenchItem {
  return {
    id,
    kind: 'artifact-preview',
    title: `${id}.html`,
    scope,
    hostKind: 'dom',
    retention,
    payload: { artifactId: id },
  }
}

function browserItem(id: string, sessionId: string): WorkbenchItem {
  return {
    id,
    kind: 'browser',
    title: 'example.test',
    scope: { type: 'session', id: sessionId },
    hostKind: 'native-webcontents',
    retention: 'keep-alive',
    payload: { initialUrl: 'https://example.test/' },
  }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('workbench store', () => {
  it('separates explicit reveal intent from activation, lifecycle, width, and persistence', () => {
    const store = useWorkbenchStore()
    store.openItem(item('side'))
    store.setWidth(614)
    const savedWidth = localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)
    const lifecycle = vi.fn()
    store.onLifecycle(lifecycle)
    expect(store.revealRequestId).toBe(0)
    store.requestReveal()
    store.requestReveal()
    expect(store.revealRequestId).toBe(2)
    expect(store.activeItemId).toBe('side')
    expect(store.expanded).toBe(true)
    expect(store.maximized).toBe(false)
    expect(localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)).toBe(savedWidth)
    expect(localStorage.length).toBe(1)
    expect(lifecycle).not.toHaveBeenCalled()
    store.openItem(item('automatic-resource'))
    expect(store.revealRequestId).toBe(2)
  })

  it('hides side chats on parent navigation and resumes the same tab without disposing its owner', () => {
    const store = useWorkbenchStore()
    const side: WorkbenchItem = { ...item('side'), kind: 'assistant-chat' }
    store.openItem(side)
    const lifecycle = vi.fn()
    store.onLifecycle(lifecycle)
    store.setSessionScope('session-b')
    expect(store.visibleItems).toEqual([])
    expect(store.items).toContainEqual(side)
    expect(lifecycle.mock.calls.some(([event]) => event.type === 'dispose')).toBe(false)
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('side')
    expect(store.visibleItems).toContainEqual(side)
    store.closeItem('side')
    expect(lifecycle).toHaveBeenCalledWith(expect.objectContaining({ type: 'dispose', reason: 'closed' }))
  })

  it.each([true, false])('registers a late background preview without changing the current pane (expanded=%s)', expanded => {
    const store = useWorkbenchStore()
    store.openItem({ ...item('side-a'), kind: 'assistant-chat' })
    store.setSessionScope('session-b')
    store.openItem(item('files-b', { type: 'session', id: 'session-b' }))
    store.setExpanded(expanded)
    const lifecycle = vi.fn()
    store.onLifecycle(lifecycle)

    const latePreview = item('late-preview-a')
    expect(store.openItem(latePreview)).toBe(true)
    expect(store.activeSessionId).toBe('session-b')
    expect(store.activeItemId).toBe('files-b')
    expect(store.expanded).toBe(expanded)
    expect(store.items).toContainEqual(latePreview)
    expect(store.visibleItems.map(entry => entry.id)).toEqual(['files-b'])
    expect(lifecycle.mock.calls.map(([event]) => event.type)).toEqual(['open'])

    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('side-a')
    expect(store.visibleItems.map(entry => entry.id)).toEqual(['side-a', 'late-preview-a'])
    expect(store.expanded).toBe(true)
  })

  it('keeps a background preview unselected on a new-chat page when activation is declined', () => {
    const store = useWorkbenchStore()
    store.setSessionScope(null)
    store.openItem(item('late-preview-a'), { activate: false })
    expect(store.activeSessionId).toBeNull()
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(false)
    expect(store.visibleItems).toEqual([])
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('late-preview-a')
  })
  it('restores each session selection, expansion and preferred width independently', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a-first'))
    store.openItem(item('a-last'))
    store.activateItem('a-first')
    store.setWidth(614)
    store.setMaximized(true)
    const events: string[] = []
    store.onLifecycle(event => events.push(`${event.type}:${event.item.id}`))

    store.setSessionScope('session-b')
    expect(store.expanded).toBe(false)
    expect(store.maximized).toBe(false)
    store.openItem(item('b', { type: 'session', id: 'session-b' }))
    store.setWidth(432)
    store.setExpanded(false)
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('a-first')
    expect(store.visibleItems.map(entry => entry.id)).toEqual(['a-first', 'a-last'])
    expect(store.expanded).toBe(true)
    expect(store.widthPreference.width).toBe(614)
    expect(store.maximized).toBe(false)
    expect(events.some(event => event.startsWith('dispose:'))).toBe(false)
    expect(store.activateItem('b')).toBe(false)
    expect(store.activeItemId).toBe('a-first')

    store.setSessionScope('session-b')
    expect(store.activeItemId).toBe('b')
    expect(store.expanded).toBe(false)
    expect(store.widthPreference.width).toBe(432)
  })

  it('restores a deliberately empty start page instead of choosing a background tab', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a'))
    store.openEmpty()
    store.setSessionScope('session-b')
    store.openItem(item('late-a'))
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(true)
    expect(store.visibleItems).toHaveLength(2)
  })

  it.each([null, 'session-a'])('does not inherit an empty launcher from owner %s into a new draft', owner => {
    const store = useWorkbenchStore()
    store.setSessionScope(owner)
    store.openEmpty()
    store.setMaximized(true)
    store.setSessionScope('new-draft')
    expect(store.expanded).toBe(false)
    expect(store.maximized).toBe(false)
    expect(store.activeItemId).toBeNull()
    store.setSessionScope(owner)
    expect(store.expanded).toBe(true)
    expect(store.activeItemId).toBeNull()
  })

  it('retains a manually opened draft launcher on materialization without sharing its expansion', () => {
    const store = useWorkbenchStore()
    store.setSessionScope('draft-a')
    store.openEmpty()
    store.setSessionScope('draft-a')
    expect(store.expanded).toBe(true)
    store.setSessionScope('draft-b')
    expect(store.expanded).toBe(false)
    store.openEmpty()
    store.setSessionScope('draft-a')
    store.setExpanded(false)
    store.setSessionScope('draft-b')
    expect(store.expanded).toBe(true)
    store.setSessionScope('draft-a')
    expect(store.expanded).toBe(false)
  })

  it('falls back safely when an inactive session selected tab was explicitly closed', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a-first'))
    store.openItem(item('a-last'))
    store.setSessionScope('session-b')
    store.closeItem('a-last')
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('a-first')
    expect(store.expanded).toBe(true)
    store.reset()
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(false)
  })

  it('protects retained terminals while enforcing the existing four-terminal admission limit', () => {
    const store = useWorkbenchStore()
    const terminal = (index: number): WorkbenchItem => ({
      ...item(`terminal-${index}`, { type: 'session', id: `session-${index}` }),
      kind: 'terminal',
    })
    for (let index = 0; index < 4; index += 1) {
      store.setSessionScope(`session-${index}`)
      expect(store.openItem(terminal(index))).toBe(true)
    }
    const events = vi.fn()
    store.onLifecycle(events)
    expect(store.openItem(terminal(4))).toBe(false)
    expect(events).not.toHaveBeenCalled()
    expect(store.items).toHaveLength(4)
    store.setSessionScope('session-0')
    expect(store.openItem(terminal(0))).toBe(true)
    store.closeItem('terminal-1')
    expect(store.openItem(terminal(4), { activate: false })).toBe(true)
    expect(store.items).toHaveLength(4)
  })

  it('maximizes without changing saved size or the active page lifecycle', () => {
    const store = useWorkbenchStore()
    store.openItem(browserItem('page', 'session-a'))
    store.setWidth(614)
    const savedWidth = localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)
    const lifecycle = vi.fn()
    store.onLifecycle(lifecycle)

    store.toggleMaximized()
    expect(store.maximized).toBe(true)
    expect(store.activeItemId).toBe('page')
    store.toggleMaximized()
    expect(store.maximized).toBe(false)
    expect(store.widthPreference.width).toBe(614)
    expect(localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)).toBe(savedWidth)
    expect(localStorage.length).toBe(1)
    expect(lifecycle).not.toHaveBeenCalled()
  })

  it('restores the normal layout after collapse and resets transient maximization', () => {
    const store = useWorkbenchStore()
    store.openEmpty()
    store.setMaximized(true)
    store.setExpanded(false)
    store.setExpanded(true)
    expect(store.maximized).toBe(false)
    store.setMaximized(true)
    store.reset()
    expect(store.maximized).toBe(false)
    store.openItem(item('preview'))
    store.setMaximized(true)
    store.closeItem('preview')
    expect(store.expanded).toBe(false)
    expect(store.maximized).toBe(false)
  })

  it('keeps the empty browser entry available without retaining an active native page', () => {
    const store = useWorkbenchStore()
    store.setSessionScope('draft-session')
    store.openEmpty()
    expect(store.expanded).toBe(true)
    expect(store.activeItem).toBeNull()
    const page = browserItem('manual-page', 'draft-session')
    store.openItem(page)
    const suspended: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'suspend') suspended.push(event.item.id)
    })
    store.openEmpty()
    expect(suspended).toEqual(['manual-page'])
    expect(store.visibleItems).toHaveLength(1)
    store.activateItem(page.id)
    store.closeItem(page.id)
    expect(store.expanded).toBe(true)
    expect(store.activeItem).toBeNull()
    expect(store.reopenBrowserForSession('draft-session')).toBe(true)
    expect(store.activeItemId).toBe(page.id)
    store.setSessionScope('different-draft')
    expect(store.activeItem).toBeNull()
    expect(store.visibleItems).toEqual([])
    expect(store.expanded).toBe(false)
  })

  it('deduplicates resources and activates the existing identity', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a'))
    store.openItem(item('b'))
    store.openItem({ ...item('a'), title: 'updated.html' })

    expect(store.items.map(candidate => candidate.id)).toEqual(['a', 'b'])
    expect(store.activeItemId).toBe('a')
    expect(store.activeItem?.title).toBe('updated.html')
    expect(store.expanded).toBe(true)
  })

  it('activates the most recently used surviving tab after close', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a'))
    store.openItem(item('b'))
    store.openItem(item('c'))
    store.activateItem('a')
    store.activateItem('b')

    store.closeItem('b')
    expect(store.activeItemId).toBe('a')

    store.closeItem('a')
    expect(store.activeItemId).toBe('c')
  })

  it('closes every open item when the Workbench itself is closed', () => {
    const store = useWorkbenchStore()
    store.openItem(item('a'))
    store.openItem(item('b'))

    store.closeAllItems()

    expect(store.items).toEqual([])
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(false)
  })

  it('finds the most recently used item inside a requested scope', () => {
    const store = useWorkbenchStore()
    store.openItem(item('session-a-old'))
    store.openItem(item('session-b', { type: 'session', id: 'session-b' }))
    store.openItem(item('session-a-new'))
    store.activateItem('session-a-old')

    expect(store.findMostRecentItem(candidate =>
      candidate.scope.type === 'session'
      && candidate.scope.id === 'session-a',
    )?.id).toBe('session-a-old')
    expect(store.findMostRecentItem(candidate =>
      candidate.scope.type === 'workspace',
    )).toBeNull()
  })

  it.each(['dom'] as const)(
    'keeps %s tabs available when opening more previews from a collapsed pane',
    hostKind => {
      const store = useWorkbenchStore()
      const disposed = vi.fn()
      store.onLifecycle(event => {
        if (event.type === 'dispose') disposed(event)
      })
      const preview = (id: string): WorkbenchItem => ({ ...item(id), hostKind })
      for (let index = 0; index < 12; index += 1) {
        expect(store.openItem(preview(`preview-${index}`))).toBe(true)
      }
      store.activateItem('preview-0')
      store.setExpanded(false)
      expect(store.openItem(preview('preview-new'))).toBe(true)

      expect(store.items.map(candidate => candidate.id)).toEqual([
        ...Array.from({ length: 12 }, (_, index) => `preview-${index}`),
        'preview-new',
      ])
      expect(disposed).not.toHaveBeenCalled()
      expect(store.activeItemId).toBe('preview-new')
      expect(store.isVisible).toBe(true)

      expect(store.openItem({ ...preview('preview-0'), title: 'Updated' })).toBe(true)
      expect(store.items).toHaveLength(13)
      expect(store.activeItem?.title).toBe('Updated')
      expect(disposed).not.toHaveBeenCalled()
    },
  )

  it('retains ordinary previews until closed and keeps browser tabs across sessions', () => {
    const store = useWorkbenchStore()
    const disposed: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'dispose') disposed.push(`${event.item.id}:${event.reason}`)
    })
    store.openItem(item('document'))
    for (let index = 0; index < 8; index += 1) {
      store.openItem({
        ...item(`native-${index}`),
        kind: index % 2 === 0 ? 'artifact-preview' : 'browser',
        hostKind: 'native-webcontents',
      })
    }
    expect(store.items).toHaveLength(9)
    expect(disposed).toEqual([])

    store.closeItem('native-7')
    expect(store.items).toHaveLength(8)
    expect(store.activeItemId).toBe('native-6')
    expect(disposed).toEqual(['native-7:closed'])

    store.setSessionScope('session-b')
    expect(store.items.map(candidate => candidate.id)).toEqual([
      'document', ...Array.from({ length: 7 }, (_, index) => `native-${index}`),
    ])
    expect(store.visibleItems).toEqual([])
    expect(store.activeItemId).toBeNull()
    expect(disposed).toEqual(['native-7:closed'])
  })

  it('bounds new interactive native artifact owners without evicting or blocking existing tabs', () => {
    const store = useWorkbenchStore()
    const native = (index: number): WorkbenchItem => ({
      ...item(`native-artifact-${index}`, { type: 'session', id: `session-${index}` }),
      hostKind: 'native-webcontents',
    })
    for (let index = 0; index < 4; index += 1) {
      store.setSessionScope(`session-${index}`)
      expect(store.openItem(native(index))).toBe(true)
    }
    const rejected = vi.fn()
    const lifecycle = vi.fn()
    const stop = store.onAdmissionRejected(rejected)
    store.onLifecycle(lifecycle)
    const selected = store.activeItemId
    expect(store.openItem(native(4), { activate: false })).toBe(false)
    expect(rejected).toHaveBeenCalledExactlyOnceWith({
      reason: 'native-preview-capacity', item: native(4), limit: 4,
    })
    expect(lifecycle).not.toHaveBeenCalled()
    expect(store.activeItemId).toBe(selected)
    expect(store.items).toHaveLength(4)
    store.setSessionScope('session-0')
    expect(store.openItem({ ...native(0), title: 'Updated revision' })).toBe(true)
    expect(store.activeItem?.title).toBe('Updated revision')
    expect(rejected).toHaveBeenCalledTimes(1)
    expect(store.openItem(item('ordinary-code'))).toBe(true)
    expect(store.openItem(browserItem('agent-browser', 'session-0'))).toBe(true)
    store.closeItem('native-artifact-1')
    expect(store.openItem(native(4), { activate: false })).toBe(true)
    expect(store.items.filter(entry => entry.kind === 'artifact-preview' && entry.hostKind === 'native-webcontents')).toHaveLength(4)
    stop()
    expect(store.openItem(native(5))).toBe(false)
    expect(rejected).toHaveBeenCalledTimes(1)
  })

  it('adopts native browser pages beyond eight retained tabs across sessions', () => {
    const store = useWorkbenchStore()
    const opened: string[] = []
    const disposed: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'open') opened.push(event.item.id)
      if (event.type === 'dispose') disposed.push(event.item.id)
    })
    const nativePage = (index: number): WorkbenchItem => ({
      ...browserItem(`web-${index}`, `session-${index}`),
      payload: { initialUrl: 'https://example.test/', adoptedNativeSurface: true },
    })
    for (let index = 0; index < 9; index += 1) {
      expect(store.openItem(nativePage(index), { activate: false })).toBe(true)
    }
    expect(store.items).toHaveLength(9)
    expect(opened).toEqual(Array.from({ length: 9 }, (_, index) => `web-${index}`))
    expect(disposed).toEqual([])

    store.setSessionScope('session-8')
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['web-8'])
    expect(store.activateItem('web-8')).toBe(true)
    expect(store.activeItemId).toBe('web-8')
    store.setSessionScope('session-0')
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['web-0'])
    expect(disposed).toEqual([])
  })

  it('limits new browsers across sessions without evicting or blocking return and adoption', () => {
    const store = useWorkbenchStore()
    for (let index = 0; index < 8; index++) {
      expect(store.openItem(browserItem(`web-${index}`, `session-${index}`), { activate: false })).toBe(true)
    }
    const rejected = vi.fn()
    const events = vi.fn()
    store.onAdmissionRejected(rejected)
    store.onLifecycle(events)
    expect(store.openItem(browserItem('overflow', 'session-9'))).toBe(false)
    expect(rejected).toHaveBeenCalledWith(expect.objectContaining({ reason: 'browser-capacity', limit: 8 }))
    expect(events).not.toHaveBeenCalled()
    store.setSessionScope('session-0')
    expect(store.openItem(browserItem('web-0', 'session-0'))).toBe(true)
    expect(store.activeItemId).toBe('web-0')
    expect(store.items).toHaveLength(8)
    store.closeItem('web-1')
    expect(store.openItem(browserItem('new-after-close', 'session-0'))).toBe(true)
    expect(store.items).toHaveLength(8)
    expect(store.openItem(item('ordinary-file'))).toBe(true)
  })

  it('bounds the closed browser history independently of live tabs', () => {
    const store = useWorkbenchStore()
    for (let index = 0; index < 10; index += 1) {
      const page = browserItem(`web-${index}`, 'session-a')
      store.openItem(page)
      store.closeItem(page.id)
    }
    expect(store.closedBrowserItems.map(candidate => candidate.id)).toEqual(
      Array.from({ length: 8 }, (_, index) => `web-${9 - index}`),
    )
  })

  it('updates background item payloads without stealing the active tab', () => {
    const store = useWorkbenchStore()
    store.openItem(item('collection'))
    store.openItem(item('preview'))

    expect(store.updateItem({
      ...item('collection'),
      payload: { artifactIds: ['a', 'b'] },
    })).toBe(true)

    expect(store.activeItemId).toBe('preview')
    expect(store.items[0]?.payload).toEqual({ artifactIds: ['a', 'b'] })
  })

  it('retains session tabs while workspace and app tabs remain available', () => {
    const store = useWorkbenchStore()
    const events: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'dispose') events.push(`${event.item.id}:${event.reason}`)
      if (event.type === 'activate') events.push(`activate:${event.item.id}`)
    })
    store.openItem(item('workspace', { type: 'workspace', id: 'repo' }))
    store.openItem(item('global', { type: 'app' }))
    store.openItem(item('old-a', { type: 'session', id: 'old' }))
    store.openItem(item('old-b', { type: 'session', id: 'old' }))
    events.splice(0)

    store.setSessionScope('new')

    expect(store.items.map(candidate => candidate.id)).toEqual(['workspace', 'global', 'old-a', 'old-b'])
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['workspace', 'global'])
    expect(events).toEqual(['activate:global'])
  })

  it('retains native browser pages across session switches and reselects them on return', () => {
    const store = useWorkbenchStore()
    const browser = browserItem('browser-a', 'session-a')
    const other = browserItem('browser-b', 'session-b')
    const disposed: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'dispose') disposed.push(event.item.id)
    })

    store.openItem(browser)
    store.openItem(other)
    store.setExpanded(false)
    store.setSessionScope('session-a')
    store.setSessionScope('session-b')

    expect(store.items.map(candidate => candidate.id)).toEqual(['browser-a', 'browser-b'])
    expect(disposed).toEqual([])
    expect(store.activeItemId).toBe('browser-b')
    expect(store.expanded).toBe(false)

    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('browser-a')
    expect(store.items).toHaveLength(2)
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['browser-a'])
    store.setSessionScope('empty-session')
    expect(store.activeItemId).toBeNull()
    expect(store.visibleItems).toEqual([])
    store.setSessionScope('session-a')
    expect(store.activeItemId).toBe('browser-a')
    expect(store.expanded).toBe(false)
  })

  it('keeps a bounded scoped reopen entry after explicitly closing a browser tab', () => {
    const store = useWorkbenchStore()
    const first = browserItem('browser-a', 'session-a')
    first.payload = { ...first.payload, adoptedNativeSurface: true, targetRef: 'retired-page' }
    const second = browserItem('browser-b', 'session-b')
    store.openItem(first)
    store.closeItem(first.id)
    store.openItem(second)
    store.closeItem(second.id)

    expect(store.closedBrowserItems.map(candidate => candidate.scope)).toEqual([
      { type: 'session', id: 'session-b' },
      { type: 'session', id: 'session-a' },
    ])
    expect(store.items).toEqual([])
    expect(store.reopenBrowserForSession('unrelated')).toBe(false)
    expect(store.reopenBrowserForSession('session-a')).toBe(true)
    expect(store.activeItem?.payload).toEqual({
      initialUrl: 'https://example.test/', scopeId: 'session-a',
    })
    expect(store.closedBrowserItems.map(candidate => candidate.id)).toEqual(['browser-b'])
    store.reset()
    expect(store.closedBrowserItems).toEqual([])
  })

  it('records background browser tabs without activating a different session', () => {
    const store = useWorkbenchStore()
    store.setSessionScope('session-a')
    store.openItem(browserItem('browser-a', 'session-a'))
    store.setExpanded(false)
    store.openItem(browserItem('browser-b', 'session-b'), { activate: false })
    expect(store.activeItemId).toBe('browser-a')
    expect(store.expanded).toBe(false)
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['browser-a'])
    store.closeItem('browser-a')
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(false)
  })

  it('keeps every retained session browser hidden on the new-task route', () => {
    const store = useWorkbenchStore()
    store.openItem(item('shared', { type: 'app' }))
    store.openItem(browserItem('browser-a', 'session-a'))
    store.openItem(browserItem('browser-b', 'session-b'), { activate: false })
    store.setSessionScope(null)
    expect(store.activeItemId).toBe('shared')
    expect(store.visibleItems.map(candidate => candidate.id)).toEqual(['shared'])
    store.closeItem('shared')
    expect(store.activeItemId).toBeNull()
    expect(store.expanded).toBe(false)
    expect(store.visibleItems).toEqual([])
  })

  it('keeps workspace and app panels available from any chat session', () => {
    const store = useWorkbenchStore()
    store.openItem(item('other-session', { type: 'session', id: 'other' }))

    expect(store.hasAvailableItemForSession('current')).toBe(false)

    store.openItem(item('workspace', { type: 'workspace', id: 'repo' }))
    expect(store.hasAvailableItemForSession('current')).toBe(true)

    store.closeItem('workspace')
    store.openItem(item('global', { type: 'app' }))
    expect(store.hasAvailableItemForSession('current')).toBe(true)
  })

  it('announces suspend and resume when the pane or host changes visibility', () => {
    const store = useWorkbenchStore()
    const events: string[] = []
    store.onLifecycle(event => events.push(event.type))
    store.openItem(item('a'))
    events.splice(0)

    store.setExpanded(false)
    store.setExpanded(true)
    store.setHostAvailable(false)
    store.setHostAvailable(true)

    expect(events).toEqual(['suspend', 'resume', 'suspend', 'resume'])
  })

  it('persists only the versioned width preference', () => {
    const setItem = vi.spyOn(localStorage, 'setItem')
    const store = useWorkbenchStore()
    store.openItem(item('secret-artifact'))

    expect(setItem).not.toHaveBeenCalled()
    store.setWidth(614)

    expect(setItem).toHaveBeenCalledOnce()
    expect(setItem).toHaveBeenCalledWith(
      WORKBENCH_WIDTH_STORAGE_KEY,
      '{"version":1,"width":614,"source":"user"}',
    )
    expect(localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)).not.toContain('secret-artifact')
  })

  it('restores the responsive default without persisting content', () => {
    const store = useWorkbenchStore()
    store.setWidth(614)

    store.resetWidth()

    expect(store.widthPreference).toEqual({
      version: 1,
      width: 520,
      source: 'default',
    })
    expect(localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY)).toBeNull()
  })
})
