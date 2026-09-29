// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AppSettings } from './appSettings'
import {
  agentTraceEnabled,
  notifyAgentTracePreferenceChanged,
  refreshAgentTraceEnabled,
  registerAgentTracePreferenceReader,
  setAgentTraceEnabled,
} from './agentTracePreference'

const storageKey = 'opensquilla.agent-trace-preference-change'
const unregisterReaders: Array<() => void> = []

function registerReader(read: AppSettings['read'], isAvailable = () => true) {
  const unregister = registerAgentTracePreferenceReader({ read }, isAvailable)
  unregisterReaders.push(unregister)
}

function receiveChange(nonce: string, extra: Record<string, unknown> = {}) {
  window.dispatchEvent(new StorageEvent('storage', {
    key: storageKey,
    newValue: JSON.stringify({ nonce, ...extra }),
  }))
}

afterEach(() => {
  unregisterReaders.splice(0).forEach(unregister => unregister())
  setAgentTraceEnabled(false)
  window.localStorage.removeItem(storageKey)
})

describe('agent trace preference', () => {
  it('accepts only an explicit saved true value', async () => {
    const read = vi.fn().mockResolvedValue(null)
    await refreshAgentTraceEnabled({ read })
    expect(agentTraceEnabled.value).toBe(false)
    expect(read).toHaveBeenCalledWith('privacy.agent_trace_enabled')

    read.mockResolvedValue(true)
    await refreshAgentTraceEnabled({ read })
    expect(agentTraceEnabled.value).toBe(true)
  })

  it('fails closed when settings cannot be read', async () => {
    setAgentTraceEnabled(true)
    await refreshAgentTraceEnabled({ read: vi.fn().mockRejectedValue(new Error('disconnected')) })
    expect(agentTraceEnabled.value).toBe(false)
  })

  it('hides an earlier Gateway trace while the next setting read is pending', async () => {
    setAgentTraceEnabled(true)
    let resolveRead: ((value: boolean) => void) | undefined
    const pending = refreshAgentTraceEnabled({
      read: () => new Promise(resolve => { resolveRead = resolve }),
    })
    expect(agentTraceEnabled.value).toBe(false)
    resolveRead?.(true)
    await pending
    expect(agentTraceEnabled.value).toBe(true)
  })

  it('ignores a stale read after a newly saved setting', async () => {
    let resolveRead: ((value: boolean) => void) | undefined
    const pending = refreshAgentTraceEnabled({
      read: () => new Promise(resolve => { resolveRead = resolve }),
    })
    setAgentTraceEnabled(true)
    resolveRead?.(false)
    await pending
    expect(agentTraceEnabled.value).toBe(true)
  })

  it('re-reads this Gateway instead of trusting another tab\'s enabled value', async () => {
    let resolveRead: ((value: boolean) => void) | undefined
    const read = vi.fn(() => new Promise<boolean>(resolve => { resolveRead = resolve }))
    registerReader(read)
    setAgentTraceEnabled(true)

    receiveChange('other-tab', { enabled: false })
    expect(agentTraceEnabled.value).toBe(false)
    expect(read).toHaveBeenCalledWith('privacy.agent_trace_enabled')

    resolveRead?.(true)
    await vi.waitFor(() => expect(agentTraceEnabled.value).toBe(true))
  })

  it('keeps a different Gateway disabled even if another tab enables trace', async () => {
    const read = vi.fn().mockResolvedValue(false)
    registerReader(read)
    receiveChange('different-gateway', { enabled: true })
    await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
    expect(agentTraceEnabled.value).toBe(false)
  })

  it('fails closed without reading a disconnected Gateway', () => {
    const read = vi.fn().mockResolvedValue(true)
    registerReader(read, () => false)
    setAgentTraceEnabled(true)
    receiveChange('disconnected')
    expect(agentTraceEnabled.value).toBe(false)
    expect(read).not.toHaveBeenCalled()
  })

  it('uses another available reader when one component is disconnected', async () => {
    const read = vi.fn().mockResolvedValue(true)
    registerReader(read)
    registerReader(vi.fn(), () => false)
    receiveChange('available-reader')
    await vi.waitFor(() => expect(agentTraceEnabled.value).toBe(true))
    expect(read).toHaveBeenCalledOnce()
  })

  it('broadcasts only saved changes, not local disconnect state', () => {
    window.localStorage.removeItem(storageKey)
    setAgentTraceEnabled(false)
    expect(window.localStorage.getItem(storageKey)).toBeNull()

    notifyAgentTracePreferenceChanged()
    const message = JSON.parse(window.localStorage.getItem(storageKey) || '{}')
    expect(message.nonce).toEqual(expect.any(String))
    expect(message).not.toHaveProperty('enabled')
  })
})
