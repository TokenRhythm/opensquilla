import { describe, expect, it } from 'vitest'

import { ALL_STATES } from './presets'
import {
  extractPurposeSuffix,
  resolveOrbState,
  type OrbLifecycle,
} from './purposeToOrbState'

const ALL_LIFECYCLES: OrbLifecycle[] = [
  'working', 'answering', 'settled', 'interrupted', 'failed',
]

describe('resolveOrbState purpose → orb state mapping', () => {
  it.each([
    ['discover', 'searching'],
    ['search', 'searching'],
    ['read', 'listening'],
    ['inspect', 'listening'],
    ['change', 'solving'],
    ['create', 'solving'],
    ['run', 'connecting'],
    ['recall', 'breathing'],
    ['use', 'working'],
  ] as const)('working + purpose.%s → %s', (suffix, expected) => {
    expect(resolveOrbState('working', `chat.activity.purpose.${suffix}`))
      .toBe(expected)
  })

  it.each([
    'discover', 'search', 'read', 'inspect', 'change', 'create', 'run', 'recall', 'use',
  ] as const)('working + purposeRunning.%s matches the base-prefix mapping', (suffix) => {
    // Active clusters emit purposeRunning.* codes (assistantActivity.ts
    // RUNNING_PURPOSE_CODES); both prefixes must resolve identically.
    const base = resolveOrbState('working', `chat.activity.purpose.${suffix}`)
    expect(resolveOrbState('working', `chat.activity.purposeRunning.${suffix}`))
      .toBe(base)
  })
})

describe('resolveOrbState lifecycle precedence', () => {
  it('answering is always composing, whatever the purpose says', () => {
    const purposes = [
      null,
      undefined,
      '',
      'chat.activity.purpose.search',
      'chat.activity.purposeRunning.read',
      'chat.activity.purpose.bogus',
    ]
    for (const purposeCode of purposes) {
      expect(resolveOrbState('answering', purposeCode)).toBe('composing')
    }
  })

  it('unknown or missing purposes fall back to working', () => {
    for (const purposeCode of [null, undefined, '', 'nonsense', 'chat.activity.purpose.fly']) {
      expect(resolveOrbState('working', purposeCode)).toBe('working')
    }
  })

  it('terminal lifecycles produce a deterministic fallback (never blank)', () => {
    for (const lifecycle of ['settled', 'interrupted', 'failed'] as const) {
      expect(resolveOrbState(lifecycle, 'chat.activity.purpose.search')).toBe('working')
      expect(resolveOrbState(lifecycle)).toBe('working')
    }
  })
})

describe('resolveOrbState total coverage', () => {
  it('every (lifecycle, purpose) combination returns a valid orb state', () => {
    const purposes = [
      null,
      'chat.activity.purpose.discover',
      'chat.activity.purposeRunning.run',
      'chat.activity.purpose.unknown-verb',
      'chat.activity.lifecycle.working',
    ]
    for (const lifecycle of ALL_LIFECYCLES) {
      for (const purpose of purposes) {
        expect(ALL_STATES).toContain(resolveOrbState(lifecycle, purpose))
      }
    }
  })
})

describe('extractPurposeSuffix', () => {
  it('strips both purpose prefixes', () => {
    expect(extractPurposeSuffix('chat.activity.purpose.search')).toBe('search')
    expect(extractPurposeSuffix('chat.activity.purposeRunning.search')).toBe('search')
  })

  it('returns null for unknown, other-prefixed, or empty codes', () => {
    expect(extractPurposeSuffix('chat.activity.purpose.fly')).toBeNull()
    expect(extractPurposeSuffix('chat.activity.lifecycle.working')).toBeNull()
    expect(extractPurposeSuffix('')).toBeNull()
    expect(extractPurposeSuffix(null)).toBeNull()
    expect(extractPurposeSuffix(undefined)).toBeNull()
  })
})
