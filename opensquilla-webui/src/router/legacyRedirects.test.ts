import { describe, expect, it } from 'vitest'
import { legacyChannelHashRedirect, parseChannelHash } from './legacyRedirects'

describe('parseChannelHash', () => {
  it('parses compose and decoded edit targets', () => {
    expect(parseChannelHash('#channel-new')).toEqual({ kind: 'new' })
    expect(parseChannelHash('#channel-team-slack')).toEqual({ kind: 'edit', name: 'team-slack' })
    expect(parseChannelHash('#channel-a%20b')).toEqual({ kind: 'edit', name: 'a b' })
    expect(parseChannelHash('channel-x')).toEqual({ kind: 'edit', name: 'x' })
  })

  it('rejects unrelated and malformed hashes', () => {
    expect(parseChannelHash('#provider-openai')).toBeNull()
    expect(parseChannelHash('#channel-')).toBeNull()
    expect(parseChannelHash('')).toBeNull()
    expect(parseChannelHash(undefined)).toBeNull()
  })
})

describe('legacyChannelHashRedirect', () => {
  it('rewrites the reserved compose hash with exact query state', () => {
    expect(legacyChannelHashRedirect({
      path: '/settings/channels',
      hash: '#channel-new',
      query: { channel: 'old', edit: '1', type: 'slack' },
    }))
      .toEqual({ path: '/channels', query: { compose: '1' }, replace: true })
  })

  it('rewrites named hashes into the exact in-place editor query', () => {
    expect(legacyChannelHashRedirect({
      path: '/settings/channels',
      hash: '#channel-team-slack',
      query: { compose: '1', type: 'discord' },
    }))
      .toEqual({
        path: '/channels',
        query: { channel: 'team-slack', tab: 'configuration', edit: '1' },
        replace: true,
      })
    expect(legacyChannelHashRedirect({ path: '/settings/channels', hash: '#channel-a%20b' }))
      .toEqual({
        path: '/channels',
        query: { channel: 'a b', tab: 'configuration', edit: '1' },
        replace: true,
      })
  })

  it('redirects bare and unknown-hash legacy paths while preserving query', () => {
    expect(legacyChannelHashRedirect({
      path: '/settings/channels',
      hash: '',
      query: { compose: '1', type: 'feishu' },
    })).toEqual({
      path: '/channels',
      query: { compose: '1', type: 'feishu' },
      replace: true,
    })
    expect(legacyChannelHashRedirect({
      path: '/settings/channels',
      hash: '#provider-openai',
      query: { channel: 'ops', tab: 'capabilities' },
    })).toEqual({
      path: '/channels',
      query: { channel: 'ops', tab: 'capabilities' },
      replace: true,
    })
    expect(legacyChannelHashRedirect({ path: '/settings/channels' }))
      .toEqual({ path: '/channels', query: {}, replace: true })
  })

  it('leaves unrelated and already-canonical routes alone', () => {
    expect(legacyChannelHashRedirect({ path: '/settings/provider', hash: '#channel-x' })).toBeNull()
    expect(legacyChannelHashRedirect({ path: '/channels', hash: '#channel-x' })).toBeNull()
  })
})
