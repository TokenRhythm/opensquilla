import { afterEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'

import {
  claimSessionBootstrapAdmission,
  clearPrimedSessionBootstrapAdmission,
  optionalSessionRpcAllowed,
  primeSessionBootstrapAdmission,
} from './sessionBootstrapAdmission'

afterEach(() => {
  clearPrimedSessionBootstrapAdmission()
})

describe('session bootstrap admission', () => {
  it('atomically transfers a router-primed hold to ChatView', () => {
    expect(optionalSessionRpcAllowed.value).toBe(true)
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)

    const observed = vi.fn()
    const stop = watch(optionalSessionRpcAllowed, observed, { flush: 'sync' })
    const release = claimSessionBootstrapAdmission()

    expect(optionalSessionRpcAllowed.value).toBe(false)
    expect(observed).not.toHaveBeenCalled()

    release()
    expect(optionalSessionRpcAllowed.value).toBe(true)
    expect(observed).toHaveBeenCalledOnce()
    stop()
  })

  it('keeps route priming singleton and releases an abandoned navigation', () => {
    primeSessionBootstrapAdmission()
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)

    clearPrimedSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(true)
    clearPrimedSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(true)
  })
})
