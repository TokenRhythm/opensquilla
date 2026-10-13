import { afterEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'

import {
  claimSessionBootstrapAdmission,
  clearPrimedSessionBootstrapAdmission,
  OPTIONAL_SESSION_READ_TIMEOUT_MS,
  optionalSessionRpcAllowed,
  optionalSessionReadOptions,
  primeSessionBootstrapAdmission,
  registerSessionBootstrapAdmissionOwner,
} from './sessionBootstrapAdmission'

afterEach(() => {
  clearPrimedSessionBootstrapAdmission()
})

describe('session bootstrap admission', () => {
  it('allows ordinary metadata latency before recovering a stuck connection', () => {
    expect(OPTIONAL_SESSION_READ_TIMEOUT_MS).toBe(10_000)
    expect(optionalSessionReadOptions).toEqual({
      timeoutMs: 10_000,
    })
  })

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

  it('does not prime over a retained view and restores priming after its disposal', () => {
    const dispose = registerSessionBootstrapAdmissionOwner()
    try {
      primeSessionBootstrapAdmission()
      expect(optionalSessionRpcAllowed.value).toBe(true)
      const releaseBootstrap = claimSessionBootstrapAdmission()
      expect(optionalSessionRpcAllowed.value).toBe(false)
      primeSessionBootstrapAdmission()
      releaseBootstrap()
      expect(optionalSessionRpcAllowed.value).toBe(true)
    } finally { dispose() }
    dispose()
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)
  })

  it('retiring an older view does not remove a newer view owner', () => {
    const older = registerSessionBootstrapAdmissionOwner()
    const newer = registerSessionBootstrapAdmissionOwner()
    try {
      older()
      older()
      primeSessionBootstrapAdmission()
      expect(optionalSessionRpcAllowed.value).toBe(true)
    } finally { newer() }
    primeSessionBootstrapAdmission()
    expect(optionalSessionRpcAllowed.value).toBe(false)
  })
})
