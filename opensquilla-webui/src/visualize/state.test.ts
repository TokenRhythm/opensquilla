// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { hasNewerVisualizeState, readVisualizeState, saveVisualizeState } from './state'

afterEach(() => sessionStorage.clear())

describe('Visualize state snapshots', () => {
  it('inherits parameters into the next version and refuses stale or invalid replacement', () => {
    const key = 'state-version-fence'
    const v1 = { version: 1, revisionId: 'rev_state1' }
    const v2 = { version: 2, revisionId: 'rev_state2' }
    saveVisualizeState(key, { selected: ['A'] }, v1)
    const next = readVisualizeState(key, v2) as { selected: string[] }
    expect(next).toEqual({ selected: ['A'] })
    next.selected.push('local mutation')
    saveVisualizeState(key, { selected: ['stale'] }, v1)
    saveVisualizeState(key, { tooLarge: 'x'.repeat(16 * 1024) }, v2)
    saveVisualizeState(key, undefined, v2)
    expect(readVisualizeState(key)).toEqual({ selected: ['A'] })
    expect(hasNewerVisualizeState(key, v1)).toBe(true)
    expect(readVisualizeState(key, v1)).toBeUndefined()
  })

  it('keeps v1 parameters during automatic migration and never passes metadata to the guest', () => {
    const key = 'state-legacy-migration'
    sessionStorage.setItem('opensquilla.visualize.v1.' + key, JSON.stringify({ amount: 25 }))
    const receipt = { version: 3, revisionId: 'rev_migrated' }
    expect(readVisualizeState(key, receipt)).toEqual({ amount: 25 })
    expect(readVisualizeState(key)).toEqual({ amount: 25 })
    expect(sessionStorage.getItem('opensquilla.visualize.v1.' + key)).toBeNull()
    expect(JSON.parse(sessionStorage.getItem('opensquilla.visualize.v2.' + key)!)).toEqual({
      schema: 2, revision: receipt, value: { amount: 25 },
    })
  })
})
