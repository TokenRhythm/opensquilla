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

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('workbench store', () => {
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
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
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
