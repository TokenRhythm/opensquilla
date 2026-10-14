// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import ChatFindBar from './ChatFindBar.vue'

const cleanups: Array<() => void> = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.useRealTimers()
})

const findMessages = {
  label: 'Find in conversation', placeholder: 'Search conversation content', previous: 'Previous match',
  next: 'Next match', close: 'Close find', loading: 'Searching', noResults: 'No matches', error: 'Search failed. Press Enter to retry',
  count: '{current} of {total}', partial: '{current} of {total}, partial results; Next continues beyond loaded matches',
  incomplete: '{current} of {total}, some content unavailable',
}

function mountBar(initial: { query?: string; resultQuery?: string; matchCount?: number; activeMatch?: number; status?: 'idle' | 'loading' | 'ready' | 'error' } = {}) {
  const props = reactive({ query: '', resultQuery: '', matchCount: 0, activeMatch: 0, status: 'idle' as 'idle' | 'loading' | 'ready' | 'error', focusSequence: 0, complete: true, coverageComplete: true, ...initial })
  const search = vi.fn()
  const navigate = vi.fn()
  const close = vi.fn()
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(ChatFindBar, {
    ...props,
    'onUpdate:query': (query: string) => { props.query = query },
    onSearch: search, onNavigate: navigate, onClose: close,
  }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en: { chat: { find: findMessages } } } })).mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  const input = element.querySelector('input')!
  const type = async (value: string) => {
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
  }
  const key = (value: string, options: KeyboardEventInit = {}) => {
    const event = new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...options })
    input.dispatchEvent(event)
    return event
  }
  return { app, props, element, input, type, key, search, navigate, close }
}

describe('ChatFindBar', () => {
  it('debounces input for 200ms, masks stale counts and immediately clears an emptied query', async () => {
    vi.useFakeTimers()
    const bar = mountBar({ query: 'old', resultQuery: 'old', matchCount: 12, activeMatch: 3, status: 'ready' })
    expect(bar.element.textContent).toContain('3/12')
    await bar.type('new')
    expect(bar.props.query).toBe('new')
    expect(bar.element.textContent).not.toContain('3/12')
    await vi.advanceTimersByTimeAsync(199)
    expect(bar.search).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(bar.search).toHaveBeenCalledWith('new')
    await bar.type('')
    expect(bar.search).toHaveBeenLastCalledWith('')
  })

  it('does not submit or navigate while IME is composing', async () => {
    vi.useFakeTimers()
    const bar = mountBar()
    bar.input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    await bar.type('zhong')
    bar.key('Enter', { isComposing: true })
    bar.key('Escape', { isComposing: true })
    await vi.advanceTimersByTimeAsync(500)
    expect(bar.search).not.toHaveBeenCalled()
    expect(bar.navigate).not.toHaveBeenCalled()
    expect(bar.close).not.toHaveBeenCalled()
    bar.input.value = '中文'
    bar.input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }))
    await vi.advanceTimersByTimeAsync(200)
    expect(bar.search).toHaveBeenCalledExactlyOnceWith('中文')
  })

  it('flushes a pending edit on Enter and navigates only its accepted results', async () => {
    vi.useFakeTimers()
    const bar = mountBar()
    await bar.type('needle')
    expect(bar.key('Enter').defaultPrevented).toBe(true)
    expect(bar.search).toHaveBeenCalledExactlyOnceWith('needle')
    expect(bar.navigate).not.toHaveBeenCalled()
    Object.assign(bar.props, { resultQuery: 'needle', status: 'ready', matchCount: 3, activeMatch: 1 })
    await nextTick()
    bar.key('Enter')
    bar.key('Enter', { shiftKey: true })
    expect(bar.navigate.mock.calls).toEqual([['next'], ['previous']])
    await vi.advanceTimersByTimeAsync(500)
    expect(bar.search).toHaveBeenCalledOnce()
  })

  it('claims Escape, cancels the debounce and never passes it to task stop handlers', async () => {
    vi.useFakeTimers()
    const bar = mountBar()
    const parentKey = vi.fn()
    document.addEventListener('keydown', parentKey)
    cleanups.push(() => document.removeEventListener('keydown', parentKey))
    await bar.type('pending')
    expect(bar.key('Escape').defaultPrevented).toBe(true)
    expect(bar.close).toHaveBeenCalledOnce()
    expect(parentKey).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(400)
    expect(bar.search).not.toHaveBeenCalled()
  })

  it('retains the query when repeated open requests refocus the input', async () => {
    const bar = mountBar({ query: 'keep me', resultQuery: 'keep me' })
    await nextTick()
    bar.input.blur()
    bar.props.focusSequence += 1
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(bar.input)
    expect(bar.input.value).toBe('keep me')
    expect(bar.search).not.toHaveBeenCalled()
  })

  it('distinguishes a complete no-match result from pagination, partial coverage and failure', async () => {
    const bar = mountBar({ query: 'needle', resultQuery: 'needle', status: 'ready' })
    const count = () => bar.element.querySelector('[role="status"]')!
    expect(count().getAttribute('aria-label')).toBe('No matches')
    expect(count().textContent).toBe('0/0')
    bar.props.complete = false
    await nextTick()
    expect(count().textContent).toBe('0/0+')
    expect(count().getAttribute('aria-label')).toContain('partial results')
    bar.key('Enter')
    expect(bar.navigate).toHaveBeenCalledWith('next')
    bar.props.complete = true
    bar.props.coverageComplete = false
    await nextTick()
    expect(count().getAttribute('aria-label')).toContain('some content unavailable')
    bar.props.status = 'error'
    await nextTick()
    expect(count().getAttribute('aria-label')).toContain('Search failed')
    bar.key('Enter')
    expect(bar.search).toHaveBeenCalledWith('needle')
  })

  it('cancels pending search when its route unmounts', async () => {
    vi.useFakeTimers()
    const bar = mountBar()
    await bar.type('pending')
    bar.app.unmount()
    await vi.advanceTimersByTimeAsync(400)
    expect(bar.search).not.toHaveBeenCalled()
  })
})
