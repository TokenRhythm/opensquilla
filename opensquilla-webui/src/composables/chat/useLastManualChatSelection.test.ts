// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IDBFactory } from 'fake-indexeddb'

import {
  LAST_MANUAL_CHAT_SELECTION_STORAGE_KEY as KEY,
  useLastManualChatSelection,
  type ChatSelectionStorage,
  type LastManualChatSelection,
} from './useLastManualChatSelection'

const FIXED: LastManualChatSelection = {
  mode: 'off', model: { model: 'synthetic-model', provider: 'synthetic-provider' },
}
const ROUTER: LastManualChatSelection = { mode: 'squilla_router', model: null }
const ENSEMBLE: LastManualChatSelection = { mode: 'llm_ensemble', model: null }

function memoryStorage() {
  const values = new Map<string, string>()
  const storage: ChatSelectionStorage = {
    getItem: vi.fn(key => values.get(key) ?? null),
    setItem: vi.fn((key, value) => { values.set(key, value) }),
    removeItem: vi.fn(key => { values.delete(key) }),
  }
  return { values, storage }
}

beforeEach(() => {
  let queue = Promise.resolve<unknown>(undefined)
  vi.stubGlobal('navigator', { locks: { request: vi.fn((_name, callback) => {
    const operation = queue.then(callback)
    queue = operation.catch(() => {})
    return operation
  }) } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('useLastManualChatSelection', () => {
  it.each([FIXED, ROUTER, ENSEMBLE, { mode: 'off', model: null }])(
    'persists and restores the $mode selection', async value => {
      const preference = useLastManualChatSelection()

      expect(preference.selection.value).toBeNull()
      expect(await preference.remember(value)).toEqual(value)
      expect(JSON.parse(localStorage.getItem(KEY)!)).toMatchObject(value)
      expect(useLastManualChatSelection().selection.value).toEqual(value)
    },
  )

  it.each(['squilla_router', 'llm_ensemble'] as const)('drops stale model details for %s', async mode => {
    const preference = useLastManualChatSelection()

    expect(await preference.remember({ mode, model: FIXED.model })).toEqual({ mode, model: null })
    expect(JSON.parse(localStorage.getItem(KEY)!)).toMatchObject({ mode, model: null })
  })

  it('reads another mounted instance on demand without changing the current selection', async () => {
    const first = useLastManualChatSelection()
    const second = useLastManualChatSelection()
    await first.remember(FIXED)

    expect(second.selection.value).toBeNull()
    expect(second.readLatest()).toEqual(FIXED)
    await first.remember(ENSEMBLE)
    expect(second.selection.value).toEqual(FIXED)
    expect(second.readLatest()).toEqual(ENSEMBLE)
  })

  it('observes storage removal and corruption on the next read', async () => {
    const preference = useLastManualChatSelection()
    await preference.remember(FIXED)
    localStorage.removeItem(KEY)
    expect(preference.readLatest()).toBeNull()

    await preference.remember(ENSEMBLE)
    localStorage.setItem(KEY, '{invalid')
    expect(preference.readLatest()).toBeNull()
  })

  it('ignores accepted-send records because they do not identify a manual choice', async () => {
    localStorage.setItem('opensquilla.chat.last-successful-selection', JSON.stringify(FIXED))
    localStorage.setItem('opensquilla.chat.last-successful-mode', 'squilla_router')

    const preference = useLastManualChatSelection()
    expect(preference.readLatest()).toBeNull()
    await preference.remember(ENSEMBLE)
    expect(useLastManualChatSelection().readLatest()).toEqual(ENSEMBLE)
    await preference.clear()
    expect(useLastManualChatSelection().readLatest()).toBeNull()
  })

  it.each(['', '{broken', 'null', '"off"', '[]', '{"mode":"unknown"}', '{"mode":"off"}'])(
    'ignores an invalid stored record: %s', serialized => {
      localStorage.setItem(KEY, serialized)
      expect(useLastManualChatSelection().readLatest()).toBeNull()
    },
  )

  it.each([
    null,
    'off',
    [],
    { mode: 'router', model: null },
    { mode: 'off' },
    { mode: 'off', model: [] },
    { mode: 'off', model: { model: '', provider: 'synthetic' } },
    { mode: 'off', model: { model: ' synthetic', provider: 'synthetic' } },
    { mode: 'off', model: { model: 'synthetic', provider: 'synthetic ' } },
    { mode: 'off', model: { model: 'synthetic\nmodel', provider: 'synthetic' } },
    { mode: 'off', model: { model: 'synthetic', provider: 'synthetic\u0085provider' } },
    { mode: 'off', model: { model: 'm'.repeat(513), provider: 'synthetic' } },
    { mode: 'off', model: { model: 'synthetic', provider: 'p'.repeat(513) } },
    { mode: 'off', model: { model: 'synthetic', provider: null } },
  ])('rejects malformed selection %#', async value => {
    const preference = useLastManualChatSelection()
    expect(await preference.remember(value)).toBeNull()
    expect(preference.readLatest()).toBeNull()
  })

  it('accepts identifiers at the bounded length', async () => {
    const value = { mode: 'off', model: { model: 'm'.repeat(512), provider: 'p'.repeat(512) } }
    expect(await useLastManualChatSelection().remember(value)).toEqual(value)
  })

  it('isolates manual snapshots from input, return, and ref mutations', async () => {
    const preference = useLastManualChatSelection({ storage: null })
    const input = { mode: 'off', model: { model: 'synthetic-model', provider: 'synthetic-provider' } }
    const returned = (await preference.remember(input))!
    input.model.model = 'changed-input'
    returned.model!.provider = 'changed-return'
    preference.selection.value!.model!.model = 'changed-ref'

    expect(preference.readLatest()).toEqual(FIXED)
    const read = preference.readLatest()!
    read.model!.model = 'changed-read'
    expect(preference.readLatest()).toEqual(FIXED)
  })

  it('keeps memory usable when browser storage is absent', async () => {
    vi.stubGlobal('localStorage', undefined)
    const preference = useLastManualChatSelection()
    expect(await preference.remember(FIXED)).toEqual(FIXED)
    expect(preference.readLatest()).toEqual(FIXED)
    await preference.clear()
    expect(preference.readLatest()).toBeNull()
  })

  it('keeps memory usable when storage access throws', async () => {
    const storage: ChatSelectionStorage = {
      getItem: vi.fn(() => { throw new Error('blocked') }),
      setItem: vi.fn(() => { throw new Error('blocked') }),
      removeItem: vi.fn(() => { throw new Error('blocked') }),
    }
    const preference = useLastManualChatSelection({ storage })
    expect(preference.selection.value).toBeNull()
    expect(await preference.remember(FIXED)).toEqual(FIXED)
    expect(preference.readLatest()).toEqual(FIXED)
    await expect(preference.clear()).resolves.toBeUndefined()
    expect(preference.readLatest()).toBeNull()
  })

  it('retains the last readable selection when later reads fail', async () => {
    const { storage, values } = memoryStorage()
    values.set(KEY, JSON.stringify(FIXED))
    const preference = useLastManualChatSelection({ storage })
    vi.mocked(storage.getItem).mockImplementation(() => { throw new Error('read blocked') })

    expect(preference.readLatest()).toEqual(FIXED)
  })

  it('does not erase the newest manual selection with stale storage after a write failure', async () => {
    const { storage, values } = memoryStorage()
    values.set(KEY, JSON.stringify(ROUTER))
    const preference = useLastManualChatSelection({ storage })
    vi.mocked(storage.setItem).mockImplementationOnce(() => { throw new Error('quota') })

    await preference.remember(FIXED)
    expect(preference.readLatest()).toEqual(FIXED)
    expect(JSON.parse(values.get(KEY)!)).toEqual(ROUTER)
    expect(storage.removeItem).not.toHaveBeenCalled()

    await preference.remember(FIXED)
    values.set(KEY, JSON.stringify(ENSEMBLE))
    expect(preference.readLatest()).toEqual(ENSEMBLE)
  })

  it('clears the remembered choice across mounts', async () => {
    localStorage.setItem(KEY, JSON.stringify(FIXED))
    const preference = useLastManualChatSelection()
    await preference.clear()

    expect(preference.readLatest()).toBeNull()
    expect(JSON.parse(localStorage.getItem(KEY)!)).toMatchObject({ mode: null, model: null })
    expect(useLastManualChatSelection().selection.value).toBeNull()
  })

  it('keeps a failed clear in memory instead of restoring persisted state', async () => {
    const { storage, values } = memoryStorage()
    values.set(KEY, JSON.stringify(FIXED))
    vi.mocked(storage.setItem).mockImplementation(() => { throw new Error('write blocked') })
    const preference = useLastManualChatSelection({ storage })

    await preference.clear()
    expect(preference.readLatest()).toBeNull()
    expect(JSON.parse(values.get(KEY)!)).toEqual(FIXED)
  })

  it('shares injected host storage while isolating other hosts and the browser', async () => {
    const firstHost = memoryStorage()
    const secondHost = memoryStorage()
    const firstView = useLastManualChatSelection({ storage: firstHost.storage })
    const secondView = useLastManualChatSelection({ storage: firstHost.storage })
    const otherHost = useLastManualChatSelection({ storage: secondHost.storage })
    await firstView.remember(FIXED)

    expect(secondView.readLatest()).toEqual(FIXED)
    expect(otherHost.readLatest()).toBeNull()
    expect(localStorage.getItem(KEY)).toBeNull()
    expect(useLastManualChatSelection().readLatest()).toBeNull()
  })
})

async function independentViews() {
  // Separate module instances model tabs: only browser storage/locks are shared.
  vi.resetModules()
  const firstModule = await import('./useLastManualChatSelection')
  vi.resetModules()
  const secondModule = await import('./useLastManualChatSelection')
  return [firstModule.useLastManualChatSelection(), secondModule.useLastManualChatSelection()] as const
}

describe('manual action ordering across tabs', () => {
  it('ignores an earlier action that completes after a later successful choice', async () => {
    const [first, second] = await independentViews()
    const earlier = await first.beginSelection()
    const later = await second.beginSelection()
    await second.remember(FIXED, later)

    expect(await first.remember(ENSEMBLE, earlier)).toEqual(FIXED)
    expect(first.readLatest()).toEqual(FIXED)
    expect(second.readLatest()).toEqual(FIXED)
  })

  it('does not let a newer failed action suppress an older successful choice', async () => {
    const [first, second] = await independentViews()
    const earlier = await first.beginSelection()
    await second.beginSelection() // Its routing RPC fails, so it never commits.

    await first.remember(ENSEMBLE, earlier)
    expect(second.readLatest()).toEqual(ENSEMBLE)
  })

  it.each([false, true])('serializes simultaneous commits (newest commits first: %s)', async newestFirst => {
    vi.spyOn(Date, 'now').mockReturnValue(1234)
    const [first, second] = await independentViews()
    const [earlier, later] = await Promise.all([first.beginSelection(), second.beginSelection()])
    expect(earlier.order!.epoch).toBe(later.order!.epoch)
    expect(earlier.order!.sequence).toBeLessThan(later.order!.sequence)
    const commits = newestFirst
      ? [() => second.remember(FIXED, later), () => first.remember(ENSEMBLE, earlier)]
      : [() => first.remember(ENSEMBLE, earlier), () => second.remember(FIXED, later)]
    await Promise.all(commits.map(commit => commit()))
    expect(first.readLatest()).toEqual(FIXED)
    expect(second.readLatest()).toEqual(FIXED)
  })

  it('keeps a clear ordered so an earlier pending result cannot resurrect it', async () => {
    const [first, second] = await independentViews()
    const earlier = await first.beginSelection()
    await second.clear()
    await first.remember(FIXED, earlier)
    expect(first.readLatest()).toBeNull()
  })

  it('uses a lazy IDB transaction on HTTP without Web Locks or randomUUID', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('indexedDB', new IDBFactory())
    vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) })
    const open = vi.spyOn(indexedDB, 'open')
    const [first, second] = await independentViews()
    expect(open).not.toHaveBeenCalled() // Mounting/reading never opens a database.
    const earlier = await first.beginSelection()
    const later = await second.beginSelection()
    await Promise.all([second.remember(FIXED, later), first.remember(ENSEMBLE, earlier)])
    expect(first.readLatest()).toEqual(FIXED)
    expect(second.readLatest()).toEqual(FIXED)
    expect(useLastManualChatSelection().readLatest()).toEqual(FIXED)
  })

  it('falls back to memory when neither browser lock mechanism is available', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('indexedDB', undefined)
    const [preference] = await independentViews()
    const earlier = await preference.beginSelection()
    const later = await preference.beginSelection()
    await preference.remember(FIXED, later)
    await preference.remember(ENSEMBLE, earlier)
    expect(preference.readLatest()).toEqual(FIXED)
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('preserves in-memory ordering when storage is blocked', async () => {
    const { storage } = memoryStorage()
    vi.mocked(storage.getItem).mockImplementation(() => { throw new Error('blocked') })
    vi.mocked(storage.setItem).mockImplementation(() => { throw new Error('blocked') })
    const preference = useLastManualChatSelection({ storage })
    const earlier = await preference.beginSelection()
    const later = await preference.beginSelection()
    await preference.remember(FIXED, later)
    await preference.remember(ENSEMBLE, earlier)
    expect(preference.readLatest()).toEqual(FIXED)
  })

  it('writes the committed order atomically with the selection', async () => {
    const { storage, values } = memoryStorage()
    const first = useLastManualChatSelection({ storage })
    const second = useLastManualChatSelection({ storage })
    const earlier = await first.beginSelection()
    const later = await second.beginSelection()
    await second.remember(FIXED, later)
    expect(JSON.parse(values.get(KEY)!)).toEqual({ ...FIXED, manualSelectionOrder: later.order })
    await first.remember(ENSEMBLE, earlier)
    expect(JSON.parse(values.get(KEY)!)).toEqual({ ...FIXED, manualSelectionOrder: later.order })
  })

  it('does not accept a token from another embedded host', async () => {
    const firstHost = memoryStorage()
    const secondHost = memoryStorage()
    const first = useLastManualChatSelection({ storage: firstHost.storage })
    const second = useLastManualChatSelection({ storage: secondHost.storage })
    const token = await first.beginSelection()
    expect(await second.remember(FIXED, token)).toBeNull()
    expect(secondHost.values.has(KEY)).toBe(false)
  })
})
