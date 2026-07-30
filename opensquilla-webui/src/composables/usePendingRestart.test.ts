// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { _resetPendingRestartForTests, usePendingRestart } from './usePendingRestart'

beforeEach(() => {
  _resetPendingRestartForTests()
})

const loadedRow = (name: string, extra: Record<string, unknown> = {}) => ({
  name,
  status: 'connected',
  enabled: true,
  capability_profile: { transports: ['polling'] },
  ...extra,
})

const configOnlyRow = (name: string, extra: Record<string, unknown> = {}) => ({
  name,
  status: 'stopped',
  enabled: true,
  capability_profile: null,
  ...extra,
})

describe('usePendingRestart', () => {
  it('records one entry per channel, latest action wins', () => {
    const store = usePendingRestart()
    store.record('a', 'disable')
    store.record('a', 'enable')
    expect(store.count.value).toBe(1)
    expect(store.pending.value[0]).toMatchObject({ channel: 'a', action: 'enable' })
  })

  it('persists across composable instances (module singleton + localStorage)', () => {
    usePendingRestart().record('a', 'upsert')
    expect(usePendingRestart().isPending('a')).toBe(true)
    expect(JSON.parse(localStorage.getItem('opensquilla.channels.pendingRestart.v1') || '[]'))
      .toHaveLength(1)
  })

  it('clears a new-channel upsert once the adapter is observed loaded', () => {
    const store = usePendingRestart()
    store.record('new-ch', 'upsert', { wasLoaded: false })
    store.reconcile([configOnlyRow('new-ch')])
    expect(store.isPending('new-ch')).toBe(true)
    store.reconcile([loadedRow('new-ch')])
    expect(store.isPending('new-ch')).toBe(false)
  })

  it('never auto-clears an upsert made over a live adapter (no observable signal)', () => {
    const store = usePendingRestart()
    store.record('live-ch', 'upsert', { wasLoaded: true })
    store.reconcile([loadedRow('live-ch')])
    expect(store.isPending('live-ch')).toBe(true)
    store.dismiss('live-ch')
    expect(store.isPending('live-ch')).toBe(false)
  })

  it('keeps a remove pending while the unloaded adapter still shows as a runtime row', () => {
    const store = usePendingRestart()
    store.record('gone', 'remove')
    store.reconcile([loadedRow('gone', { configured: false })])
    expect(store.isPending('gone')).toBe(true)
    store.reconcile([])
    expect(store.isPending('gone')).toBe(false)
  })

  it('keeps a disable pending while the adapter is still loaded', () => {
    const store = usePendingRestart()
    store.record('busy', 'disable')
    // Config already says disabled, but the adapter is still delivering.
    store.reconcile([loadedRow('busy', { status: 'disabled', enabled: false })])
    expect(store.isPending('busy')).toBe(true)
    store.reconcile([configOnlyRow('busy', { status: 'disabled', enabled: false })])
    expect(store.isPending('busy')).toBe(false)
  })

  it('clears an enable once the adapter loads', () => {
    const store = usePendingRestart()
    store.record('wake', 'enable')
    store.reconcile([configOnlyRow('wake')])
    expect(store.isPending('wake')).toBe(true)
    store.reconcile([loadedRow('wake')])
    expect(store.isPending('wake')).toBe(false)
  })
})
