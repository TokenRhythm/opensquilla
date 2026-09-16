import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  canonicalConversationEventName,
  conversationSemanticEventKind,
  decodeConversationEvent,
  decodeConversationEventFrame,
  isConversationEventName,
} from './conversationEventsV4'

interface FixtureCase {
  id: string
  wire: unknown
}

interface FixtureDocument {
  cases: FixtureCase[]
}

function fixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../../contracts/gateway/v4/conversation/fixtures/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('conversation event v4 Adapter', () => {
  it('decodes every valid canonical, legacy, and future event fixture', () => {
    for (const testCase of fixture('events.json').cases) {
      const wire = testCase.wire as Record<string, unknown>
      const decoded = decodeConversationEventFrame(wire)
      expect(decoded.name, testCase.id).toMatch(/^(session|task|chat)\./)
      expect(decoded.rawPayload, testCase.id).toBe(wire.payload)
    }
  })

  it('rejects malformed or unrelated event fixtures', () => {
    for (const testCase of fixture('errors.json').cases) {
      expect(
        () => decodeConversationEventFrame(testCase.wire),
        testCase.id,
      ).toThrow()
    }
  })

  it('normalizes legacy aliases without mutating the payload', () => {
    const payload = {
      sessionKey: 'agent:main:legacy',
      taskId: 'task-legacy',
      streamSeq: 4,
    }
    const decoded = decodeConversationEvent('text_delta', payload)
    expect(decoded.name).toBe('session.event.text_delta')
    expect(decoded.legacy).toBe(true)
    expect(decoded.sessionKey).toBe('agent:main:legacy')
    expect(decoded.taskId).toBe('task-legacy')
    expect(decoded.streamSeq).toBe(4)
    expect(payload).toEqual({
      sessionKey: 'agent:main:legacy',
      taskId: 'task-legacy',
      streamSeq: 4,
    })
  })

  it('marks unknown additive names instead of routing them to a typed handler', () => {
    const decoded = decodeConversationEvent(
      'session.event.future_checkpoint',
      { key: 'agent:main:alpha', schema_version: 1 },
    )
    expect(decoded.kind).toBe('unknown')
    expect(decoded.isKnown).toBe(false)
    expect(decoded.sessionKey).toBe('agent:main:alpha')
  })

  it('keeps null and primitive payloads out of the object projection', () => {
    const decoded = decodeConversationEvent('session.event.warning', null)
    expect(decoded.payload).toBeNull()
    expect(decoded.rawPayload).toBeNull()
    expect(decoded.legacy).toBe(true)
  })

  it('accepts explicit null fields in a legacy payload', () => {
    const decoded = decodeConversationEventFrame({
      event: 'session.event.state_change',
      payload: {
        schema_version: null,
        sessionKey: 'agent:main:alpha',
        streamSeq: null,
      },
    })
    expect(decoded.legacy).toBe(true)
    expect(decoded.schemaVersion).toBeNull()
    expect(decoded.sessionKey).toBe('agent:main:alpha')
    expect(decoded.streamSeq).toBeNull()
  })

  it('rejects conflicting aliases and unsupported schema versions', () => {
    expect(() => decodeConversationEvent('session.event.text_delta', {
      key: 'agent:main:a',
      session_key: 'agent:main:b',
    })).toThrow(/conflicting aliases/)
    expect(() => decodeConversationEvent('session.event.text_delta', {
      key: 'agent:main:a',
      schema_version: 2,
    })).toThrow()
  })

  it('accepts both complete-frame and RpcClient callback forms', () => {
    const frame = {
      event: 'session.event.text_delta',
      payload: { key: 'agent:main:alpha', stream_seq: 3 },
      meta: { replayed: false },
      seq: 9,
    }
    expect(decodeConversationEvent(frame)).toEqual(decodeConversationEvent(
      frame.event,
      frame.payload,
      frame.meta,
      frame.seq,
    ))
  })

  it('canonicalizes historical event names and filters unrelated events', () => {
    expect(canonicalConversationEventName('session.turn_committed.v1'))
      .toBe('session.event.turn_committed')
    expect(isConversationEventName('task.running')).toBe(true)
    expect(isConversationEventName('presence')).toBe(false)
  })

  it('projects canonical, bare, and versioned aliases to the same semantic kind', () => {
    expect(conversationSemanticEventKind('session.event.text_delta')).toBe('text-delta')
    expect(conversationSemanticEventKind('text_delta')).toBe('text-delta')
    expect(conversationSemanticEventKind('session.turn_committed.v1')).toBe('turn-committed')
    expect(conversationSemanticEventKind('exec.approval.requested')).toBe('approval-requested')
  })
})
