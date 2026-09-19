import { describe, expect, it } from 'vitest'
import type { ChatToolCall } from '@/types/chat'
import {
  sessionReferencesFromMessage,
  sessionReferencesFromToolCall,
} from '@/utils/chat/sessionReferences'
import {
  sessionApplicationUrl,
  sessionGatewayUrl,
  normalizeReferenceV1,
  normalizeSessionReferenceV1,
} from '@/types/references'

function call(result: unknown, overrides: Partial<ChatToolCall> = {}): ChatToolCall {
  return {
    toolId: 'search-1',
    name: 'session_search',
    displayName: 'session_search',
    inputPreview: '',
    isRunning: false,
    status: 'success',
    isError: false,
    result: typeof result === 'string' ? result : JSON.stringify(result),
    resultPreview: '',
    isOpen: false,
    ...overrides,
  }
}

describe('session references', () => {
  it('extracts only versioned references from structured search results', () => {
    const reference = {
      version: 1,
      kind: 'session',
      id: 'agent:main:webchat:default',
      label: 'Earlier chat',
      scope: { sessionKey: 'agent:main:webchat:default' },
      state: { available: true, runStatus: null },
      capabilities: { open: true, copy: true },
    }
    expect(sessionReferencesFromToolCall(call({ results: [{ reference }] }))).toHaveLength(1)
    expect(sessionReferencesFromToolCall(call('Earlier chat: agent:main:webchat:default'))).toEqual([])
  })

  it('preserves unavailable references while dropping failed results', () => {
    const reference = {
      version: 1,
      kind: 'session',
      id: 'agent:main:webchat:old',
      label: 'Old chat',
      scope: { sessionKey: 'agent:main:webchat:old' },
      state: { available: false, runStatus: null },
      capabilities: { open: true, copy: true },
    }
    expect(sessionReferencesFromToolCall(call({ sessions: [{ reference }] }))).toHaveLength(1)
    expect(sessionReferencesFromToolCall(call({ sessions: [{ reference }] }, {
      isError: true,
    }))).toEqual([])
  })

  it('adapts known legacy search rows without making plain paths or malformed references executable', () => {
    expect(sessionReferencesFromToolCall(call({ results: [{
      session_key: 'agent:main:webchat:legacy', title: 'Previous deployment',
    }] }))).toMatchObject([{ label: 'Previous deployment', scope: {
      sessionKey: 'agent:main:webchat:legacy',
    } }])
    expect(sessionReferencesFromToolCall(call({ results: [
      { snippet: 'agent:main:webchat:forged' },
      { session_key: '' },
      { session_key: 'agent:main:webchat:legacy', reference: { version: 99 } },
    ] }))).toEqual([])
    expect(sessionReferencesFromToolCall(call({ results: [{ session_key: 'legacy' }] }, {
      name: 'read_source',
    }))).toEqual([])
  })

  it('accepts references already normalized onto a rendered message', () => {
    const reference = {
      version: 1,
      kind: 'session',
      id: 'sess-message',
      label: 'Message reference',
      scope: { sessionKey: 'sess-message' },
      state: { available: true, runStatus: 'idle' },
      capabilities: { open: true, copy: true },
    }
    expect(sessionReferencesFromMessage({ sessionReferences: [reference] } as never))
      .toMatchObject([{ reference: { id: 'agent:main:webchat:message' } }])
  })

  it.each(['agent:main:webc…', 'agent:main:webc...'])('never turns truncated historical identity %s into a reference', shortened => {
    const results = ['first', 'second', 'third'].map(title => ({
      session_key: shortened,
      title,
      reference: {
        version: 1, kind: 'session', id: shortened, label: title,
        scope: { sessionKey: shortened }, state: { available: true },
        capabilities: { open: true },
      },
    }))
    expect(sessionReferencesFromToolCall(call({ result_truncated: true, results }))).toEqual([])
    expect(sessionReferencesFromToolCall(call({ results: [{ session_key: shortened }] }))).toEqual([])
  })

  it('rejects truncation in either structured identity field without treating display-label ellipses as identity', () => {
    const complete = 'agent:main:webchat:complete'
    const reference = {
      version: 1, kind: 'session', id: complete, label: 'Long title…',
      scope: { sessionKey: complete }, state: { available: true }, capabilities: { open: true },
    }
    expect(normalizeSessionReferenceV1(reference)).toMatchObject({ id: complete, label: 'Long title…' })
    expect(normalizeSessionReferenceV1({ ...reference, id: 'agent:main:webc…' })).toBeNull()
    expect(normalizeSessionReferenceV1({ ...reference, scope: { sessionKey: 'agent:main:webc…' } })).toBeNull()
  })

  it('keeps distinct complete identities in a bounded historical result preview', () => {
    const keys = ['first', 'second', 'third'].map(suffix => `agent:main:webchat:${suffix}`)
    const results = keys.map(id => ({ reference: {
      version: 1, kind: 'session', id, label: 'Shortened display text…',
      scope: { sessionKey: id }, state: { available: true }, capabilities: { open: true },
    } }))
    expect(sessionReferencesFromToolCall(call({ result_truncated: true, results })).map(item => item.id))
      .toEqual(keys)
  })

  it('uses the router base and strips Gateway endpoint query credentials', () => {
    expect(sessionApplicationUrl('agent:main:webchat:default', '/control/'))
      .toBe('http://localhost/control/chat?session=agent%3Amain%3Awebchat%3Adefault')
    expect(sessionGatewayUrl(
      'agent:main:webchat:default',
      'wss://gateway.example/ws?token=secret',
      '/control/',
    )).toBe('https://gateway.example/control/chat?session=agent%3Amain%3Awebchat%3Adefault')
    expect(sessionGatewayUrl('agent:main:webchat:default', 'file:///tmp/gateway', '/control/'))
      .toBe('http://localhost/control/chat?session=agent%3Amain%3Awebchat%3Adefault')
  })

  it('accepts only safe external URL references', () => {
    expect(normalizeReferenceV1({
      version: 1,
      kind: 'external_url',
      id: 'https://example.test/docs',
      label: 'Docs',
      scope: {},
      capabilities: { open: true },
    })).not.toBeNull()
    expect(normalizeReferenceV1({
      version: 1,
      kind: 'external_url',
      id: 'javascript:alert(1)',
      label: 'bad',
      scope: {},
      capabilities: { open: true },
    })).toBeNull()
    expect(normalizeReferenceV1({
      version: 1,
      kind: 'external_url',
      id: 'https://user:secret@example.test/docs',
      label: 'credentialed',
      scope: {},
      capabilities: { open: true },
    })).toBeNull()
  })
})
