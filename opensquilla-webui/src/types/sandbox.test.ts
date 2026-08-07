import { describe, expect, it } from 'vitest'

import {
  SANDBOX_RUN_MODES,
  isSandboxRunMode,
  normalizeSandboxRunMode,
} from './sandbox'

describe('sandbox run modes', () => {
  it('exposes only canonical Safe and Full values', () => {
    expect(SANDBOX_RUN_MODES).toEqual(['safe', 'full'])
    expect(isSandboxRunMode('standard')).toBe(false)
    expect(isSandboxRunMode('trusted')).toBe(false)
  })

  it('decodes old client values one way into canonical values', () => {
    expect(normalizeSandboxRunMode('standard')).toBe('safe')
    expect(normalizeSandboxRunMode('trusted')).toBe('safe')
    expect(normalizeSandboxRunMode('managed')).toBe('safe')
    expect(normalizeSandboxRunMode('bypass')).toBe('full')
  })
})
