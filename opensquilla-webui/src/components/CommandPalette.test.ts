// @vitest-environment happy-dom
import { createApp, h, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import {
  SESSION_DIRECTORY_KEY,
  type SessionDirectory,
  type SessionSearchResult,
} from '@/modules/sessionDirectory'
import CommandPalette from './CommandPalette.vue'

const routerPush = vi.fn()

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

function emptySearch(): SessionSearchResult {
  return { sessions: [], messages: [] }
}

function deferredSearch() {
  let resolve!: (value: SessionSearchResult) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<SessionSearchResult>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fakeDirectory(
  search: SessionDirectory['search'] = vi.fn().mockResolvedValue(emptySearch()),
): SessionDirectory {
  return {
    listPage: vi.fn().mockResolvedValue({ items: [], hasMore: false, nextCursor: null }),
    count: vi.fn().mockResolvedValue({ value: 0, exact: true }),
    resolve: vi.fn().mockResolvedValue({
      key: 'agent:main:webchat:default',
      id: 'default',
    }),
    search,
  }
}

describe('CommandPalette navigation and conversation search', () => {
  let app: App<Element> | null = null

  beforeEach(() => {
    vi.useFakeTimers()
    i18n.global.locale.value = 'en'
  })

  afterEach(() => {
    app?.unmount()
    app = null
    document.body.innerHTML = ''
    routerPush.mockReset()
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  async function mountPalette(directory = fakeDirectory()) {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const open = ref(true)
    const selectSession = vi.fn()
    app = createApp({
      render: () => h(CommandPalette, {
        open: open.value, recents: [],
        'onUpdate:open': (value: boolean) => { open.value = value },
        onSelectSession: selectSession,
      }),
    })
    app.use(i18n)
    app.provide(SESSION_DIRECTORY_KEY, directory)
    app.mount(el)
    await nextTick()
    return { directory, el, open, selectSession }
  }

  async function search(el: Element, value: string) {
    const input = el.querySelector<HTMLInputElement>('.cmdp-search__input')!
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
  }

  async function settleSearch() {
    await vi.advanceTimersByTimeAsync(180)
    await nextTick()
    // The Adapter promise and the component's continuation each get a turn.
    await Promise.resolve()
    await nextTick()
  }

  it('shows one primary usage destination for English and Chinese queries', async () => {
    const { el } = await mountPalette()

    await search(el, 'usage')
    expect(Array.from(el.querySelectorAll('.cmdp-option__label')).map(node => node.textContent))
      .toEqual(['View usage'])

    await search(el, '用量')
    expect(Array.from(el.querySelectorAll('.cmdp-option__label')).map(node => node.textContent))
      .toEqual(['View usage'])
    expect(el.querySelector('.cmdp-group-label')?.textContent).toBe('Work')
  })

  it.each(['logs', 'overview', 'health'])('does not advertise the retired %s page', async (query) => {
    const { el } = await mountPalette()
    await search(el, query)
    await settleSearch()
    expect(el.querySelectorAll('.cmdp-option')).toHaveLength(0)
  })

  it('debounces search by 180ms and sends the domain request', async () => {
    const searchCall = vi.fn().mockResolvedValue(emptySearch())
    const { directory, el } = await mountPalette(fakeDirectory(searchCall))

    await search(el, 'milk')
    await vi.advanceTimersByTimeAsync(179)
    expect(directory.search).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(directory.search).toHaveBeenCalledTimes(1)
    expect(directory.search).toHaveBeenCalledWith({ query: 'milk', limit: 12 })
  })

  it('renders session and message hits with transcript highlighting', async () => {
    const searchCall = vi.fn().mockResolvedValue({
      sessions: [{
        key: 'agent:main:s1',
        title: 'Deploy planning',
        surface: 'webchat',
      }],
      messages: [{
        key: 'agent:main:s2',
        title: 'Grocery list',
        snippet: 'buy >>>milk<<< today',
        createdAt: 1700000000000,
      }],
    } satisfies SessionSearchResult)
    const { el } = await mountPalette(fakeDirectory(searchCall))

    await search(el, 'milk')
    await settleSearch()

    expect(Array.from(el.querySelectorAll('.cmdp-option__label')).map(node => node.textContent))
      .toEqual(['Deploy planning', 'Grocery list'])
    expect(el.querySelector('.cmdp-option__snippet')?.textContent).toBe('buy milk today')
    expect(el.querySelector('.cmdp-option__snippet mark.cmdp-mark')?.textContent).toBe('milk')
  })

  it('does not let a stale search response replace the latest query', async () => {
    let resolveFirst!: (value: SessionSearchResult) => void
    let resolveSecond!: (value: SessionSearchResult) => void
    const first = new Promise<SessionSearchResult>(resolve => { resolveFirst = resolve })
    const second = new Promise<SessionSearchResult>(resolve => { resolveSecond = resolve })
    const searchCall = vi.fn()
      .mockReturnValueOnce(first)
      .mockReturnValueOnce(second)
    const { el } = await mountPalette(fakeDirectory(searchCall))

    await search(el, 'first')
    await vi.advanceTimersByTimeAsync(180)
    await search(el, 'second')
    await vi.advanceTimersByTimeAsync(180)
    expect(searchCall).toHaveBeenCalledTimes(2)

    resolveSecond({
      sessions: [{ key: 'agent:main:new', title: 'Second result', surface: null }],
      messages: [],
    })
    await nextTick()
    await Promise.resolve()
    await nextTick()
    expect(el.textContent).toContain('Second result')

    resolveFirst({
      sessions: [{ key: 'agent:main:old', title: 'First result', surface: null }],
      messages: [],
    })
    await nextTick()
    await Promise.resolve()
    await nextTick()
    expect(el.textContent).toContain('Second result')
    expect(el.textContent).not.toContain('First result')
  })

  it('clears results after a search error and stops the searching indicator', async () => {
    const searchCall = vi.fn().mockRejectedValue(new Error('search unavailable'))
    const { el } = await mountPalette(fakeDirectory(searchCall))

    await search(el, 'milk')
    await settleSearch()

    expect(el.querySelector('.cmdp-searching')).toBeNull()
    expect(el.querySelector('.cmdp-option__snippet')).toBeNull()
    expect(el.querySelector('.cmdp-empty')?.textContent).toContain('No matches')
  })

  it('retires an old response during the next query debounce window', async () => {
    const first = deferredSearch()
    const searchCall = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(emptySearch())
    const { el, selectSession } = await mountPalette(fakeDirectory(searchCall))
    await search(el, 'first')
    await vi.advanceTimersByTimeAsync(180)
    await search(el, 'second')
    first.resolve({
      sessions: [{ key: 'agent:main:old', title: 'Obsolete result', surface: null }],
      messages: [],
    })
    await vi.advanceTimersByTimeAsync(179)
    expect(searchCall).toHaveBeenCalledTimes(1)
    expect(el.textContent).not.toContain('Obsolete result')
    el.querySelector('input')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(selectSession).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(searchCall).toHaveBeenLastCalledWith({ query: 'second', limit: 12 })
  })

  it('immediately makes rendered old hits unavailable to Enter and a retained click', async () => {
    const searchCall = vi.fn().mockResolvedValue({
      sessions: [{ key: 'agent:main:old', title: 'Obsolete result', surface: null }],
      messages: [],
    } satisfies SessionSearchResult)
    const { el, selectSession } = await mountPalette(fakeDirectory(searchCall))
    await search(el, 'first')
    await settleSearch()
    const oldButton = el.querySelector<HTMLButtonElement>('.cmdp-option')!
    const input = el.querySelector<HTMLInputElement>('input')!
    input.value = 'second'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    // Exercise selection before Vue has even removed the previous DOM nodes.
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    oldButton.click()
    expect(selectSession).not.toHaveBeenCalled()
    await nextTick()
    expect(el.textContent).not.toContain('Obsolete result')
    expect(searchCall).toHaveBeenCalledTimes(1)
  })

  it.each(['success', 'error'] as const)(
    'keeps the new search pending when the obsolete request settles with %s', async outcome => {
      const first = deferredSearch()
      const second = deferredSearch()
      const searchCall = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
      const { el } = await mountPalette(fakeDirectory(searchCall))
      await search(el, 'first')
      await vi.advanceTimersByTimeAsync(180)
      await search(el, 'second')
      await vi.advanceTimersByTimeAsync(180)
      if (outcome === 'success') first.resolve(emptySearch())
      else first.reject(new Error('obsolete failure'))
      await nextTick()
      await Promise.resolve()
      await nextTick()
      expect(el.querySelector('.cmdp-searching')).not.toBeNull()
      second.resolve({
        sessions: [{ key: 'agent:main:new', title: 'Current result', surface: null }], messages: [],
      })
      await nextTick()
      await Promise.resolve()
      await nextTick()
      expect(el.textContent).toContain('Current result')
      expect(el.querySelector('.cmdp-searching')).toBeNull()
    },
  )

  it('retires a pending search on close before a clean reopen', async () => {
    const pending = deferredSearch()
    const { el, open } = await mountPalette(fakeDirectory(vi.fn().mockReturnValue(pending.promise)))
    await search(el, 'first')
    await vi.advanceTimersByTimeAsync(180)
    el.querySelector<HTMLButtonElement>('.cmdp-search__close')!.click()
    await nextTick()
    open.value = true
    await nextTick()
    pending.resolve({
      sessions: [{ key: 'agent:main:old', title: 'Closed result', surface: null }], messages: [],
    })
    await nextTick()
    await Promise.resolve()
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('input')!.value).toBe('')
    await search(el, 'next')
    expect(el.textContent).not.toContain('Closed result')
    expect(el.querySelector('.cmdp-searching')).toBeNull()
  })

  it('does not dispatch a debounced search after unmount', async () => {
    const searchCall = vi.fn().mockResolvedValue(emptySearch())
    const { el } = await mountPalette(fakeDirectory(searchCall))
    await search(el, 'pending')
    app!.unmount()
    app = null
    await vi.advanceTimersByTimeAsync(180)
    expect(searchCall).not.toHaveBeenCalled()
  })

  it('does not search short ASCII queries and clears prior hits', async () => {
    const searchCall = vi.fn().mockResolvedValue({
      sessions: [{ key: 'agent:main:s1', title: 'Milk planning', surface: null }],
      messages: [],
    } satisfies SessionSearchResult)
    const { el } = await mountPalette(fakeDirectory(searchCall))

    await search(el, 'milk')
    await settleSearch()
    expect(el.textContent).toContain('Milk planning')

    await search(el, 'm')
    expect(searchCall).toHaveBeenCalledTimes(1)
    expect(el.textContent).not.toContain('Milk planning')
    expect(el.querySelector('.cmdp-option__snippet')).toBeNull()
    expect(el.querySelector('.cmdp-searching')).toBeNull()
  })
})
