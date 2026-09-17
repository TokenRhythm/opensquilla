import { describe, expect, it } from 'vitest'
import { effectiveChatConnectionState } from './chatConnectionState'

describe('effectiveChatConnectionState', () => {
  it('keeps the physical Gateway transport authoritative during session recovery', () => {
    expect(effectiveChatConnectionState('connected', 'connecting', true)).toBe('connected')
    expect(effectiveChatConnectionState('connected', 'degraded', true)).toBe('connected')
    expect(effectiveChatConnectionState('connected', 'ready', true)).toBe('connected')
  })

  it('keeps the socket state authoritative outside chat and while disconnected', () => {
    expect(effectiveChatConnectionState('connected', 'degraded', false)).toBe('connected')
    expect(effectiveChatConnectionState('disconnected', 'ready', true)).toBe('disconnected')
  })
})
