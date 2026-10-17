// @vitest-environment happy-dom
import { computed } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createVisualizeSnapshotStore } from './snapshotStore'

const prefix = 'snapshot-test.v2.'
const legacyPrefix = 'snapshot-test.v1.'
const revision = (version: number) => ({ version, revisionId: `rev_${version}` })
const createStore = () => createVisualizeSnapshotStore<unknown>({
  prefix, legacyPrefix, maxValueLength: 256,
  normalize(value) {
    const encoded = JSON.stringify(value)
    if (!encoded || encoded.length > 256) throw new Error('invalid snapshot')
    return JSON.parse(encoded)
  },
})

beforeEach(() => sessionStorage.clear())
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('Visualize snapshot ownership', () => {
  it('preserves a newer snapshot across reload without the display-version index', () => {
    const store = createStore()
    store.save('widget', { amount: 3 }, revision(3))
    const reloaded = createStore()
    expect(reloaded.hasNewer('widget', revision(1))).toBe(true)
    expect(reloaded.read('widget', revision(1))).toBeUndefined()
    expect(reloaded.save('widget', { amount: 1 }, revision(1))).toBe(false)
    expect(reloaded.save('widget', { amount: 0 })).toBe(false)
    expect(reloaded.read('widget')).toEqual({ amount: 3 })
    expect(reloaded.read('widget', revision(4))).toEqual({ amount: 3 })
    expect(createStore().save('widget', { amount: -1 }, revision(3))).toBe(false)
    expect(reloaded.read('another-widget', revision(1))).toBeUndefined()
  })

  it('notifies mounted readers when a new revision claims ownership without guest data', () => {
    const store = createStore()
    const historical = computed(() => store.hasNewer('widget', revision(1)))
    expect(historical.value).toBe(false)
    expect(store.read('widget', revision(2))).toBeUndefined()
    expect(historical.value).toBe(true)
    expect(store.save('widget', { old: true }, revision(1))).toBe(false)
    expect(createStore().hasNewer('widget', revision(1))).toBe(true)
  })

  it('keeps an explicit clear and its fence together after reload', () => {
    const store = createStore()
    store.save('widget', { summary: 'selected' }, revision(2))
    store.save('widget', null, revision(3))
    const reloaded = createStore()
    expect(reloaded.read('widget', revision(3))).toBeNull()
    expect(reloaded.save('widget', { summary: 'stale' }, revision(2))).toBe(false)
    expect(reloaded.read('widget')).toBeNull()
  })

  it.each([false, 0, null, { schema: 2, revision: { version: 99, revisionId: 'rev_guest' }, value: 'guest data' }])(
    'migrates raw legacy data without interpreting its contents as host metadata: %j', value => {
      sessionStorage.setItem(legacyPrefix + 'widget', JSON.stringify(value))
      const store = createStore()
      expect(store.read('widget', revision(2))).toEqual(value)
      expect(sessionStorage.getItem(legacyPrefix + 'widget')).toBeNull()
      expect(createStore().read('widget', revision(2))).toEqual(value)
      expect(createStore().hasNewer('widget', revision(1))).toBe(true)
    },
  )

  it('migrates an unversioned read once and does not rewrite unchanged reads', () => {
    sessionStorage.setItem(legacyPrefix + 'widget', JSON.stringify({ count: 5 }))
    // Happy DOM binds Storage methods through a proxy, whose descriptors cannot
    // reliably be restored by spying on the instance. Stub only the global view.
    const storage = sessionStorage
    const write = vi.fn((key: string, value: string) => storage.setItem(key, value))
    vi.stubGlobal('sessionStorage', new Proxy(storage, {
      get(target, property) { return property === 'setItem' ? write : Reflect.get(target, property) },
    }))
    const store = createStore()
    expect(store.read('widget')).toEqual({ count: 5 })
    expect(write).toHaveBeenCalledTimes(1)
    store.read('widget')
    expect(write).toHaveBeenCalledTimes(1)
    store.read('widget', revision(2))
    expect(write).toHaveBeenCalledTimes(2)
    store.read('widget', revision(2))
    store.read('widget')
    expect(write).toHaveBeenCalledTimes(2)
    createStore().read('widget', revision(2))
    expect(write).toHaveBeenCalledTimes(2)
  })

  it('retains a known version for an unversioned receipt of the same revision', () => {
    const store = createStore()
    store.save('widget', { count: 3 }, revision(3))
    expect(store.save('widget', { count: 4 }, { revisionId: 'rev_3' })).toBe(true)
    expect(store.save('widget', { count: 0 }, { revisionId: 'rev_other' })).toBe(false)
    expect(createStore().hasNewer('widget', revision(2))).toBe(true)
    expect(store.read('widget')).toEqual({ count: 4 })
  })

  it('retains the in-memory fence when browser storage is unavailable', () => {
    vi.stubGlobal('sessionStorage', {
      getItem() { throw new Error('disabled') },
      setItem() { throw new Error('disabled') },
    })
    const store = createStore()
    store.save('widget', { count: 3 }, revision(3))
    expect(store.save('widget', { count: 1 }, revision(1))).toBe(false)
    expect(store.read('widget', revision(3))).toEqual({ count: 3 })
  })

  it('strictly bounds stored records when the current key appears first', () => {
    const store = createStore()
    for (let index = 0; index < 49; index += 1) {
      sessionStorage.setItem(prefix + `widget-${index}`, JSON.stringify({ schema: 2, revision: revision(3), value: index }))
    }
    store.save('widget-0', 100, revision(4))
    const keys = Object.keys(sessionStorage).filter(key => key.startsWith(prefix))
    expect(keys).toHaveLength(48)
    expect(createStore().read('widget-0', revision(4))).toBe(100)
    expect(createStore().hasNewer('widget-0', revision(3))).toBe(true)
  })
})
