import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type EffectScope } from 'vue'
import { SessionMessageSearchError, type SessionMessageSearch, type SessionMessageSearchMatch, type SessionMessageSearchPage } from '@/modules/sessionMessageSearch'
import { useChatFind } from './useChatFind'

const scopes: EffectScope[] = []
function match(messageId: string, offsetUtf16 = 0, overrides: Partial<SessionMessageSearchMatch> = {}): SessionMessageSearchMatch {
  return { messageId, entryCursor: `entry:${messageId}`, contentRevision: 1, offsetUtf16, lengthUtf16: 2,
    snippet: '🐱🐱 找到的正文', ...overrides }
}
function page(matches: SessionMessageSearchMatch[] = [], overrides: Partial<SessionMessageSearchPage> = {}): SessionMessageSearchPage {
  return { sessionId: 'canonical-A', sessionEpoch: 3, query: '🐱', matches, nextCursor: null,
    complete: true, coverageComplete: true, scannedEntries: 20, unavailableEntries: 0, ...overrides }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function setup(available = true) {
  const scope = effectScope()
  scopes.push(scope)
  const search = vi.fn<SessionMessageSearch['search']>().mockResolvedValue(page())
  const options = {
    sessionKey: ref('session-A'), identity: ref('canonical-A:3'), foregroundBusy: ref(false),
    search: available ? { search, context: vi.fn<SessionMessageSearch['context']>() } : null,
    onOpen: vi.fn(), onReset: vi.fn(), onClear: vi.fn(),
    onSelect: vi.fn<Parameters<typeof useChatFind>[0]['onSelect']>().mockResolvedValue(undefined),
  }
  return { ...options, search, scope, api: scope.run(() => useChatFind(options))! }
}
const flush = () => vi.advanceTimersByTimeAsync(0)

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('useChatFind', () => {
  it('retries busy history positioning without showing a search failure', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([match('one')]))
    h.onSelect.mockRejectedValueOnce(new SessionMessageSearchError('busy', 'busy', 500))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(500)
    await searching
    expect(h.onSelect).toHaveBeenCalledTimes(2)
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 1, matchCount: 1 })
  })

  it('leaves a busy position unselected after bounded retries and permits explicit retry', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([match('one')]))
    h.onSelect.mockRejectedValue(new SessionMessageSearchError('busy', 'busy'))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(10000)
    await searching
    expect(h.onSelect).toHaveBeenCalledTimes(3)
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 0, matchCount: 1 })
    h.onSelect.mockResolvedValue(undefined)
    h.api.navigate('next'); await flush()
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 1 })
  })

  it('aborts a busy position retry when a newer result is selected', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([match('one'), match('two')]))
    h.onSelect.mockRejectedValueOnce(new SessionMessageSearchError('busy', 'busy', 1000))
    h.api.show()
    const searching = h.api.search('needle')
    await flush()
    h.api.navigate('next'); await flush()
    await searching
    await vi.advanceTimersByTimeAsync(2000)
    expect(h.onSelect).toHaveBeenCalledTimes(2)
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 2 })
  })

  it('starts previous navigation at the last result when initial positioning stayed busy', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([match('one'), match('two'), match('three')]))
    h.onSelect.mockRejectedValue(new SessionMessageSearchError('busy', 'busy'))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(10000)
    await searching
    expect(h.api.state.value.activeMatch).toBe(0)
    h.onSelect.mockResolvedValue(undefined)
    h.api.navigate('previous'); await flush()
    expect(h.onSelect).toHaveBeenLastCalledWith(match('three'), 0, expect.anything())
    expect(h.api.state.value.activeMatch).toBe(3)
  })

  it('returns to the last presented result when superseding pending positions stay busy', async () => {
    const h = setup(), pending = deferred<void>()
    h.search.mockResolvedValue(page([match('one'), match('two'), match('three')]))
    h.api.show()
    await h.api.search('needle')
    h.onSelect.mockReturnValueOnce(pending.promise)
      .mockRejectedValue(new SessionMessageSearchError('busy', 'busy'))
    h.api.navigate('next')
    h.api.navigate('next')
    pending.resolve()
    await vi.advanceTimersByTimeAsync(10000)
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 1, matchCount: 3 })
  })
  it('limits scanning during a running turn and resumes only on explicit navigation', async () => {
    const h = setup()
    h.foregroundBusy.value = true
    h.search.mockResolvedValue(page([], { complete: false, nextCursor: 'next' }))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(3000)
    await searching
    expect(h.search).toHaveBeenCalledTimes(4)
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: false, matchCount: 0 })
    h.api.navigate('next')
    await vi.advanceTimersByTimeAsync(3000)
    expect(h.search).toHaveBeenCalledTimes(8)
  })

  it('bounds elapsed paging time even when each page returns slowly', async () => {
    const h = setup()
    h.search.mockImplementation(async () => {
      await new Promise(resolve => setTimeout(resolve, 700))
      return page([], { complete: false, nextCursor: 'next' })
    })
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(10000)
    await searching
    expect(h.search).toHaveBeenCalledTimes(3)
    expect(h.api.state.value.status).toBe('ready')
  })

  it('allows provisional coverage to become complete after incremental verification', async () => {
    const h = setup()
    h.search.mockResolvedValueOnce(page([], { complete: false, coverageComplete: false, nextCursor: 'proof' }))
      .mockResolvedValueOnce(page([match('found')]))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(100)
    await searching
    expect(h.api.state.value).toMatchObject({ complete: true, coverageComplete: true })
  })

  it('backs off on admission pressure without discarding results or changing cursor', async () => {
    const h = setup()
    h.search.mockResolvedValueOnce(page([match('one')], { complete: false, nextCursor: 'same' }))
      .mockRejectedValueOnce(new SessionMessageSearchError('busy', 'busy', 500))
      .mockResolvedValueOnce(page([match('two')]))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(599)
    expect(h.search).toHaveBeenCalledTimes(2)
    expect(h.api.state.value).toMatchObject({ status: 'ready', matchCount: 1 })
    await vi.advanceTimersByTimeAsync(1)
    await searching
    expect(h.search.mock.calls.slice(1).map(([r]) => r.cursor)).toEqual(['same', 'same'])
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: true, matchCount: 2 })
  })

  it('stops retrying busy storage and can resume, without parallel scans', async () => {
    const h = setup()
    h.search.mockRejectedValue(new SessionMessageSearchError('busy', 'busy'))
    h.api.show()
    const searching = h.api.search('needle')
    await vi.advanceTimersByTimeAsync(10000)
    await searching
    expect(h.search).toHaveBeenCalledTimes(3)
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: false })
    h.search.mockResolvedValue(page([match('recovered')]))
    h.api.navigate('next'); h.api.navigate('next')
    await flush()
    expect(h.search).toHaveBeenCalledTimes(4)
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: true, matchCount: 1 })
  })

  it('cancels the busy backoff immediately when editing the query', async () => {
    const h = setup()
    h.search.mockRejectedValue(new SessionMessageSearchError('busy', 'busy', 1000))
    h.api.show()
    const searching = h.api.search('needle')
    await flush()
    h.api.updateQuery('new')
    await searching
    await vi.advanceTimersByTimeAsync(3000)
    expect(h.search).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('returns to the live conversation only after the explicit send is accepted', () => {
    const h = setup(), returnToLive = vi.fn()
    h.api.show()
    const accepted = h.api.captureAcceptedSend(returnToLive)
    expect(h.api.open.value).toBe(true)
    expect(returnToLive).not.toHaveBeenCalled()
    accepted()
    expect(h.api.open.value).toBe(false)
    expect(h.onReset).toHaveBeenCalledWith(false)
    expect(returnToLive).toHaveBeenCalledOnce()
    accepted()
    expect(returnToLive).toHaveBeenCalledOnce()
  })

  it.each(['query', 'reopen', 'session', 'identity', 'initially-closed'] as const)(
    'does not let an old accepted send close a newer search after %s', change => {
      const h = setup(), returnToLive = vi.fn()
      if (change !== 'initially-closed') h.api.show()
      const accepted = h.api.captureAcceptedSend(returnToLive)
      if (change === 'query') h.api.updateQuery('new search')
      if (change === 'reopen') { h.api.close(); h.api.show() }
      if (change === 'session') { h.sessionKey.value = 'session-B'; h.api.show() }
      if (change === 'identity') { h.identity.value = 'identity-B'; h.api.show() }
      if (change === 'initially-closed') h.api.show()
      accepted()
      expect(h.api.open.value).toBe(true)
      expect(returnToLive).not.toHaveBeenCalled()
    },
  )

  it('opens once, requests focus on repeat, and restores the previous reading position on close', () => {
    const h = setup()
    h.api.show(); h.api.show()
    expect(h.onOpen).toHaveBeenCalledOnce()
    expect(h.api.state.value).toMatchObject({ open: true, focusSequence: 2, activeMatch: 0, matchCount: 0 })
    h.api.close()
    expect(h.onReset).toHaveBeenLastCalledWith(true)
    expect(h.api.state.value).toMatchObject({ open: false, query: '', resultQuery: '', status: 'idle', complete: true })
  })

  it('does not search a closed find bar or a whitespace-only query', async () => {
    const h = setup()
    await h.api.search('🐱')
    h.api.show()
    await h.api.search('  ')
    expect(h.search).not.toHaveBeenCalled()
    expect(h.api.state.value).toMatchObject({ status: 'idle', matchCount: 0, complete: true })
  })

  it('reports unavailable search without starting any request', async () => {
    const h = setup(false)
    h.api.show()
    await h.api.search('🐱')
    expect(h.api.state.value.status).toBe('error')
    expect(h.search).not.toHaveBeenCalled()
    expect(h.onSelect).not.toHaveBeenCalled()
  })

  it.each(['session', 'identity', 'round-trip', 'close', 'dispose'] as const)(
    'cancels pending pages on %s and ignores a late response', async reason => {
      const h = setup(), pending = deferred<SessionMessageSearchPage>()
      h.search.mockReturnValue(pending.promise)
      h.api.show()
      const searching = h.api.search('🐱')
      const signal = h.search.mock.calls[0]![0].signal!
      if (reason === 'session' || reason === 'round-trip') {
        h.sessionKey.value = 'session-B'
        if (reason === 'round-trip') h.sessionKey.value = 'session-A'
      } else if (reason === 'identity') h.identity.value = 'canonical-A:4'
      else if (reason === 'close') h.api.close()
      else h.scope.stop()
      expect(signal.aborted).toBe(true)
      expect(h.onReset).toHaveBeenLastCalledWith(reason === 'close')
      pending.resolve(page([match('stale')]))
      await searching
      expect(h.api.state.value).toMatchObject({ open: false, status: 'idle', matchCount: 0, activeMatch: 0 })
      expect(h.onSelect).not.toHaveBeenCalled()
    },
  )

  it('cancels immediately while editing the query, before a new search starts', async () => {
    const h = setup(), pending = deferred<SessionMessageSearchPage>()
    h.search.mockReturnValue(pending.promise)
    h.api.show()
    const searching = h.api.search('old')
    const signal = h.search.mock.calls[0]![0].signal!
    h.api.updateQuery('new')
    expect(signal.aborted).toBe(true)
    h.api.navigate('next')
    pending.resolve(page([match('stale')], { query: 'old' }))
    await searching
    expect(h.api.state.value).toMatchObject({ query: 'new', status: 'loading', matchCount: 0, activeMatch: 0, complete: false })
    expect(h.search).toHaveBeenCalledOnce()
    expect(h.onSelect).not.toHaveBeenCalled()
    h.api.updateQuery('  ')
    expect(h.api.state.value).toMatchObject({ status: 'idle', complete: true })
  })

  it.each(['resolve', 'reject'] as const)('keeps a newer query when the superseded page completes with %s', async completion => {
    const h = setup(), old = deferred<SessionMessageSearchPage>()
    h.search.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page([match('new')], { query: 'new' }))
    h.api.show()
    const olderSearch = h.api.search('old')
    await h.api.search('new')
    if (completion === 'resolve') old.resolve(page([match('old')], { query: 'old' }))
    else old.reject(new Error('Late old query failure'))
    await olderSearch
    expect(h.api.state.value).toMatchObject({ query: 'new', resultQuery: 'new', status: 'ready', matchCount: 1, activeMatch: 1 })
    expect(h.api.matches.value[0]?.messageId).toBe('new')
    expect(h.onSelect).toHaveBeenCalledOnce()
  })

  it('continues through empty pages using the pinned session identity and preserves partial coverage', async () => {
    const h = setup()
    h.search.mockResolvedValueOnce(page([], { nextCursor: 'cursor:1', complete: false, coverageComplete: false, unavailableEntries: 1 }))
      .mockResolvedValueOnce(page([], { nextCursor: 'cursor:2', complete: false }))
      .mockResolvedValueOnce(page([match('last')]))
    h.api.show()
    const searching = h.api.search('  🐱  ')
    await flush()
    expect(h.api.state.value).toMatchObject({ status: 'loading', matchCount: 0, complete: false, coverageComplete: false })
    await vi.advanceTimersByTimeAsync(200)
    await searching
    expect(h.search.mock.calls.map(([request]) => request.cursor)).toEqual([undefined, 'cursor:1', 'cursor:2'])
    expect(h.search.mock.calls[1]![0]).toMatchObject({ key: 'session-A', query: '🐱', sessionId: 'canonical-A', sessionEpoch: 3, limit: 100 })
    expect(h.api.state.value).toMatchObject({ status: 'ready', matchCount: 1, complete: true, coverageComplete: false })
    expect(h.onSelect).toHaveBeenCalledWith(match('last'), 0, expect.objectContaining({ query: '🐱', sessionId: 'canonical-A', sessionEpoch: 3 }))
  })

  it('deduplicates overlapping pages while retaining UTF-16 offsets and occurrence within a message', async () => {
    const h = setup(), first = match('emoji', 0), second = match('emoji', 2), third = match('other', 4)
    h.search.mockResolvedValueOnce(page([first], { complete: false, nextCursor: 'next' }))
      .mockResolvedValueOnce(page([first, second, second, third]))
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(100)
    await searching
    expect(h.api.matches.value).toEqual([first, second, third])
    h.api.navigate('next'); await flush()
    expect(h.onSelect).toHaveBeenLastCalledWith(second, 1, expect.objectContaining({ query: '🐱' }))
    h.api.navigate('next'); await flush()
    expect(h.onSelect).toHaveBeenLastCalledWith(third, 0, expect.anything())
    expect(h.api.state.value).toMatchObject({ matchCount: 3, activeMatch: 3 })
  })

  it('wraps navigation only after the search is complete and aborts the previous selection', async () => {
    const h = setup(), first = match('one'), second = match('two')
    h.search.mockResolvedValue(page([first, second]))
    h.api.show()
    await h.api.search('🐱')
    const originalSignal = h.onSelect.mock.calls[0]![2].signal
    h.api.navigate('previous'); await flush()
    expect(originalSignal.aborted).toBe(true)
    expect(h.onSelect).toHaveBeenLastCalledWith(second, 0, expect.anything())
    h.api.navigate('next'); await flush()
    expect(h.onSelect).toHaveBeenLastCalledWith(first, 0, expect.anything())
    expect(h.api.state.value.activeMatch).toBe(1)
  })

  it.each(['query', 'close'] as const)('aborts pending selection on %s without turning a late failure into a current error', async change => {
    const h = setup(), pending = deferred<void>()
    h.search.mockResolvedValue(page([match('one')]))
    h.onSelect.mockReturnValueOnce(pending.promise)
    h.api.show()
    const searching = h.api.search('🐱')
    await flush()
    const selectedSignal = h.onSelect.mock.calls[0]![2].signal
    if (change === 'query') h.api.updateQuery('new')
    else h.api.close()
    expect(selectedSignal.aborted).toBe(true)
    pending.reject(new Error('Late context lookup failure'))
    await searching
    expect(h.api.state.value).toMatchObject({ status: change === 'query' ? 'loading' : 'idle', matchCount: 0 })
  })

  it('ignores a failed superseded selection while a newer result is selected', async () => {
    const h = setup(), pending = deferred<void>()
    h.search.mockResolvedValue(page([match('one'), match('two'), match('three')]))
    h.api.show()
    await h.api.search('🐱')
    h.onSelect.mockReturnValueOnce(pending.promise)
    h.api.navigate('next')
    const oldSelectionSignal = h.onSelect.mock.calls[1]![2].signal
    h.api.navigate('next')
    pending.reject(new Error('Late second result failure'))
    await flush()
    expect(oldSelectionSignal.aborted).toBe(true)
    expect(h.api.state.value).toMatchObject({ status: 'ready', activeMatch: 3 })
  })

  it('reports a current selection failure without reporting a successful position', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([match('one')]))
    h.onSelect.mockRejectedValue(new Error('Context unavailable'))
    h.api.show()
    await h.api.search('🐱')
    expect(h.api.state.value.status).toBe('error')
    h.api.navigate('next')
    expect(h.onSelect).toHaveBeenCalledOnce()
  })

  it('keeps an initial selection failure visible even when later search pages succeed', async () => {
    const h = setup()
    h.search.mockResolvedValueOnce(page([match('one')], { complete: false, nextCursor: 'next' }))
      .mockResolvedValueOnce(page([match('two')]))
    h.onSelect.mockRejectedValueOnce(new Error('Context unavailable'))
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(100)
    await searching
    expect(h.api.state.value.status).toBe('error')
  })

  it('keeps already found results partial when a later page fails, then permits a clean retry', async () => {
    const h = setup()
    h.search.mockResolvedValueOnce(page([match('one')], { complete: false, nextCursor: 'next' }))
      .mockRejectedValueOnce(new Error('Search temporarily unavailable'))
      .mockResolvedValueOnce(page([match('retry')]))
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(100)
    await searching
    expect(h.api.state.value).toMatchObject({ status: 'error', complete: false, matchCount: 1 })
    await h.api.search('🐱')
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: true, matchCount: 1, activeMatch: 1 })
    expect(h.api.matches.value[0]?.messageId).toBe('retry')
  })

  it('aborts the inter-page wait without issuing another request', async () => {
    const h = setup()
    h.search.mockResolvedValue(page([], { complete: false, nextCursor: 'next' }))
    h.api.show()
    const searching = h.api.search('🐱')
    await flush()
    h.api.close()
    await searching
    await vi.advanceTimersByTimeAsync(500)
    expect(h.search).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('bounds each eager batch at 500 results and resumes the same snapshot on navigation at the loaded edge', async () => {
    const h = setup()
    h.search.mockImplementation(async request => {
      const cursor = Number(request.cursor ?? 0)
      const entries = Array.from({ length: 100 }, (_, i) => match(`message-${cursor * 100 + i}`))
      return page(entries, { complete: cursor === 5, nextCursor: cursor === 5 ? null : String(cursor + 1) })
    })
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(400)
    await searching
    expect(h.search).toHaveBeenCalledTimes(5)
    expect(h.api.state.value).toMatchObject({ status: 'ready', matchCount: 500, activeMatch: 1, complete: false })
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.search).toHaveBeenCalledTimes(5)
    for (let i = 1; i < 500; i += 1) h.api.navigate('next')
    expect(h.api.state.value.activeMatch).toBe(500)
    h.api.navigate('next'); await flush()
    expect(h.search).toHaveBeenCalledTimes(6)
    expect(h.search.mock.calls[5]![0]).toMatchObject({ key: 'session-A', sessionId: 'canonical-A', sessionEpoch: 3, cursor: '5' })
    expect(h.api.state.value).toMatchObject({ status: 'ready', matchCount: 600, activeMatch: 501, complete: true })
    expect(h.onSelect).toHaveBeenLastCalledWith(match('message-500'), 0, expect.anything())
  })

  it('bounds empty-page scanning and permits resuming when no matches have been loaded', async () => {
    const h = setup()
    h.search.mockImplementation(async request => {
      const cursor = Number(request.cursor ?? 0)
      return cursor === 20 ? page([match('found')]) : page([], { complete: false, nextCursor: String(cursor + 1) })
    })
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(1900)
    await searching
    expect(h.search).toHaveBeenCalledTimes(20)
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: false, matchCount: 0, activeMatch: 0 })
    h.api.navigate('next'); await flush()
    expect(h.search).toHaveBeenCalledTimes(21)
    expect(h.api.state.value).toMatchObject({ status: 'ready', complete: true, matchCount: 1, activeMatch: 1 })
  })

  it('lets a newer previous click cancel a pending next-page navigation', async () => {
    const h = setup(), pending = deferred<SessionMessageSearchPage>()
    h.search.mockResolvedValueOnce(page([match('one'), match('two')], { complete: false, nextCursor: 'next' }))
      .mockReturnValueOnce(pending.promise)
    h.api.show()
    const searching = h.api.search('🐱')
    await vi.advanceTimersByTimeAsync(100)
    h.api.navigate('next')
    h.api.navigate('next')
    h.api.navigate('previous')
    expect(h.api.state.value.activeMatch).toBe(1)
    pending.resolve(page([match('three')]))
    await searching
    expect(h.api.state.value.activeMatch).toBe(1)
    expect(h.onSelect).toHaveBeenLastCalledWith(match('one'), 0, expect.anything())
  })
})
