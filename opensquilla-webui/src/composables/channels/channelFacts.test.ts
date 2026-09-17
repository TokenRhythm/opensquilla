import { describe, expect, it } from 'vitest'
import { humanize, normalizeTransportToken, transportLabel, transportTokens } from './channelFacts'
import type { Channel } from '@/modules/channelAdministration'

const channel = (extra: Partial<Channel> = {}): Channel => ({
  name: 'tg',
  type: 'telegram',
  ...extra,
} as Channel)

describe('normalizeTransportToken', () => {
  it('folds display strings and raw tokens onto one vocabulary', () => {
    expect(normalizeTransportToken('HTTP sync')).toBe('http_sync')
    expect(normalizeTransportToken(' WebSocket ')).toBe('websocket')
    expect(normalizeTransportToken('polling')).toBe('polling')
  })
})

describe('transportTokens', () => {
  it('prefers live capability transports over the static fallback', () => {
    const ch = channel({ capability_profile: { transports: ['polling', 'webhook'] } } as Partial<Channel>)
    expect(transportTokens(ch)).toEqual(['polling', 'webhook'])
  })

  it('falls back to the curated static token for not-yet-loaded channels', () => {
    expect(transportTokens(channel())).toEqual(['polling'])
    expect(transportTokens(channel({ type: 'matrix' } as Partial<Channel>))).toEqual(['http_sync'])
    expect(transportTokens(channel({ type: 'msteams' } as Partial<Channel>))).toEqual(['webhook'])
  })

  it('normalizes and dedupes live transports', () => {
    const ch = channel({ capability_profile: { transports: ['Polling', 'polling', 'http sync'] } } as Partial<Channel>)
    expect(transportTokens(ch)).toEqual(['polling', 'http_sync'])
  })
})

describe('transportLabel', () => {
  it('degrades to humanized tokens when no translate callback is given', () => {
    expect(transportLabel(channel())).toBe('Polling')
    expect(transportLabel(channel({ type: 'matrix' } as Partial<Channel>))).toBe('Http Sync')
  })

  it('renders live and static transports through the translate callback', () => {
    const zh = (token: string) => ({ polling: '轮询', http_sync: 'HTTP 同步' }[token] || humanize(token))
    expect(transportLabel(channel(), '', zh)).toBe('轮询')
    expect(transportLabel(channel({ type: 'matrix' } as Partial<Channel>), '', zh)).toBe('HTTP 同步')
    const live = channel({ capability_profile: { transports: ['polling', 'webhook'] } } as Partial<Channel>)
    expect(transportLabel(live, '', (t) => `T:${t}`)).toBe('T:polling / T:webhook')
  })

  it('returns the notReported placeholder when nothing is known', () => {
    expect(transportLabel(channel({ type: 'unknown-type' } as Partial<Channel>), 'n/a')).toBe('n/a')
  })
})
