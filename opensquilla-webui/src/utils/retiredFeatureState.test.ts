import { describe, expect, it } from 'vitest'
import { clearRetiredBrowserFeatureState, clearRetiredFeatureState } from './retiredFeatureState'

describe('retired workflow state cleanup', () => {
  it('removes recovery inputs while retaining ordinary drafts and settings', () => {
    const entries = new Map([
      ['opensquilla.chat.metaDraftOutbox:v1', 'old workflow'],
      ['opensquilla.chat.metaDiscardOutbox:v1', 'old cancellation'],
      ['opensquilla.chat.hiddenControlOutbox:v1', 'old control'],
      ['opensquilla.chat.metaSetupJob:synthetic-session', 'old job'],
      ['opensquilla.chat.metaSetupLaunch:synthetic-session', 'old launch'],
      ['opensquilla.chat.metaSetupManual:synthetic-session', 'old setup'],
      ['opensquilla.chat.draft:synthetic-session', 'ordinary draft'],
      ['opensquilla.theme', 'dark'],
    ])
    const storage = {
      get length() { return entries.size },
      key(index: number) { return [...entries.keys()][index] ?? null },
      removeItem(key: string) { entries.delete(key) },
    }
    clearRetiredFeatureState(storage)
    clearRetiredFeatureState(storage)
    expect([...entries]).toEqual([
      ['opensquilla.chat.draft:synthetic-session', 'ordinary draft'],
      ['opensquilla.theme', 'dark'],
    ])
  })

  it('keeps startup available when browser storage cannot be read', () => {
    expect(() => clearRetiredFeatureState({
      get length(): number { throw new Error('storage unavailable') },
      key: () => null,
      removeItem: () => {},
    })).not.toThrow()
  })

  it.each(['localStorage', 'sessionStorage'] as const)(
    'cleans the other store when %s is blocked',
    blocked => {
      const entries = new Map<string, string>()
      const storage: Storage = {
        get length() { return entries.size },
        key(index) { return [...entries.keys()][index] ?? null },
        getItem(key) { return entries.get(key) ?? null },
        setItem(key, value) { entries.set(key, value) },
        removeItem(key) { entries.delete(key) },
        clear() { entries.clear() },
      }
      storage.setItem('opensquilla.chat.metaSetupJob:synthetic-session', 'retired setup')
      storage.setItem('opensquilla.chat.hiddenControlOutbox:v1', 'retired control')
      storage.setItem('opensquilla.chat.draft:synthetic-session', 'ordinary draft')
      const browser = {
        get localStorage() {
          if (blocked === 'localStorage') throw new Error('storage unavailable')
          return storage
        },
        get sessionStorage() {
          if (blocked === 'sessionStorage') throw new Error('storage unavailable')
          return storage
        },
      }
      expect(() => clearRetiredBrowserFeatureState(browser)).not.toThrow()
      expect(storage.getItem('opensquilla.chat.metaSetupJob:synthetic-session')).toBeNull()
      expect(storage.getItem('opensquilla.chat.hiddenControlOutbox:v1')).toBeNull()
      expect(storage.getItem('opensquilla.chat.draft:synthetic-session')).toBe('ordinary draft')
      storage.clear()
    },
  )
})
