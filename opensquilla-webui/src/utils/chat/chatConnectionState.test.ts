import { describe, expect, it } from 'vitest'
import { effectiveChatConnectionState } from './chatConnectionState'

describe('effectiveChatConnectionState', () => {
  it('never reports a healthy chat while the session subscription is recovering', () => {
    expect(effectiveChatConnectionState('connected', 'connecting', true)).toBe('connecting')
    expect(effectiveChatConnectionState('connected', 'degraded', true)).toBe('disconnected')
    expect(effectiveChatConnectionState('connected', 'ready', true)).toBe('connected')
  })

  it('keeps the socket state authoritative outside chat and while disconnected', () => {
    expect(effectiveChatConnectionState('connected', 'degraded', false)).toBe('connected')
    expect(effectiveChatConnectionState('disconnected', 'ready', true)).toBe('disconnected')
  })
})
