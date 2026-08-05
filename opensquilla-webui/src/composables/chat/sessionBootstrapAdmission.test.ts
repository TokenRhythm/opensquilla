import { afterEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'

import {
  claimSessionBootstrapAdmission,
  clearPrimedSessionBootstrapAdmission,
  OPTIONAL_SESSION_RPC_TIMEOUT_MS,
  optionalSessionRpcAllowed,
  optionalSessionRpcCallOptions,
  primeSessionBootstrapAdmission,
  sandboxSetupRpcCallOptions,
} from './sessionBootstrapAdmission'

afterEach(() => {
  clearPrimedSessionBootstrapAdmission()
})

describe('session bootstrap admission', () => {
  it('allows ordinary metadata latency before recovering a stuck connection', () => {
    expect(OPTIONAL_SESSION_RPC_TIMEOUT_MS).toBe(10_000)
    expect(optionalSessionRpcCallOptions).toEqual({
      timeoutMs: 10_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    })
  })

  it('lets the first live sandbox verification finish without recycling the socket', () => {
    expect(sandboxSetupRpcCallOptions).toEqual({
      timeoutMs: 45_000,
      timeoutAction: 'reject',
      abortAction: 'reject',
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
})
