// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { WORKBENCH_WIDTH_STORAGE_KEY } from './layout'
import {
  useWorkbenchStore,
  WORKBENCH_PREVIEW_ITEM_LIMIT,
} from './store'
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
    expect(store.expanded).toBe(true)
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

  it('evicts the least recently used preview after the bounded tab limit', () => {
    const store = useWorkbenchStore()
    const disposed: string[] = []
    store.onLifecycle(event => {
      if (event.type === 'dispose') {
        disposed.push(`${event.item.id}:${event.reason}`)
      }
    })

    for (let index = 0; index < WORKBENCH_PREVIEW_ITEM_LIMIT; index += 1) {
      store.openItem(item(`preview-${index}`))
    }
    store.activateItem('preview-0')
    store.openItem(item('preview-new'))

    expect(store.items).toHaveLength(WORKBENCH_PREVIEW_ITEM_LIMIT)
    expect(store.items.some(candidate => candidate.id === 'preview-0')).toBe(true)
    expect(store.items.some(candidate => candidate.id === 'preview-1')).toBe(false)
    expect(disposed).toContain('preview-1:evicted')
    expect(store.activeItemId).toBe('preview-new')
  })

  it('refuses a ninth native surface without evicting a hidden item', () => {
    const store = useWorkbenchStore()
    const nativeItem = (id: string): WorkbenchItem => ({
      ...item(id),
      hostKind: 'native-webcontents',
    })
    for (let index = 0; index < WORKBENCH_PREVIEW_ITEM_LIMIT; index += 1) {
      expect(store.openItem(nativeItem(`native-${index}`))).toBe(true)
    }

    expect(store.openItem(nativeItem('native-new'))).toBe(false)
    expect(store.items).toHaveLength(WORKBENCH_PREVIEW_ITEM_LIMIT)
    expect(store.items.map(candidate => candidate.id)).toEqual(
      Array.from(
        { length: WORKBENCH_PREVIEW_ITEM_LIMIT },
        (_, index) => `native-${index}`,
      ),
    )
    expect(store.activeItemId).toBe('native-7')
  })

  it('bounds web browser tabs across sessions without evicting a retained page', () => {
    const store = useWorkbenchStore()
    const webPage = (index: number): WorkbenchItem => ({
      ...browserItem(`web-${index}`, `session-${index}`),
      hostKind: 'dom',
    })
    for (let index = 0; index < WORKBENCH_PREVIEW_ITEM_LIMIT; index += 1) {
      expect(store.openItem(webPage(index), { activate: false })).toBe(true)
    }
    expect(store.openItem(webPage(WORKBENCH_PREVIEW_ITEM_LIMIT))).toBe(false)
    expect(store.items).toHaveLength(WORKBENCH_PREVIEW_ITEM_LIMIT)
    expect(store.openItem(webPage(0))).toBe(true)
    expect(store.activeItemId).toBe('web-0')
    store.closeItem('web-1')
    expect(store.openItem(webPage(WORKBENCH_PREVIEW_ITEM_LIMIT))).toBe(true)
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

  it('disposes stale session items without touching workspace or app items', () => {
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

    expect(store.items.map(candidate => candidate.id)).toEqual(['workspace', 'global'])
    expect(events).toEqual([
      'old-a:scope-changed',
      'old-b:scope-changed',
      'activate:global',
    ])
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
