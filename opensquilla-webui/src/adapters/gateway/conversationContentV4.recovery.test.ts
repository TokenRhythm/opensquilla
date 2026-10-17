import { describe, expect, it } from 'vitest'
import { projectConversationEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

function project(name: string, body: Record<string, unknown>) {
  return projectConversationEvent(decodeConversationEvent(name, { key: 'agent:main:test', task_id: 'task-1', turn_id: 'task-1', stream_seq: 8, epoch: 2, ...body }))
}
describe('provider recovery wire projection', () => {
  it('projects explicit fallback source and destination without deriving missing fields', () => {
    expect(project('session.event.provider_activity', { phase: 'fallback', model: 'model-b', from_model: 'model-a' })).toMatchObject({ kind: 'known', semanticKind: 'provider-activity', payload: { model: 'model-b', from_model: 'model-a' } })
    expect(project('session.event.provider_activity', { phase: 'fallback', model: 'model-b' })).not.toHaveProperty('payload.from_model')
  })
  it('adapts an explicit HTTP status but does not infer it from text or application code', () => {
    const error = project('session.event.error', { message: 'private error', code: 'provider_rate_limited', failure_kind: 'rate_limited', http_status: 429 })
    expect(error).toMatchObject({ kind: 'known', payload: { httpStatus: 429 } })
    const unclassified = project('session.event.error', { message: 'HTTP 429 from an SSE error', code: '429' })
    expect(unclassified).not.toHaveProperty('payload.httpStatus')
  })
  it('keeps compaction recovery in its owner scope and strips private exception fields', () => {
    const event = project('session.event.compaction', { status: 'observed', source: 'automatic', compaction_id: 'c', phase: 'summarizing', recovery: { phase: 'retry_wait', reason: 'rate_limited', retry_attempt: 1, retry_limit: 2, retry_after_ms: 35000, started_at: 1800000000000, http_status: 429 } })
    expect(event).toMatchObject({ kind: 'known', semanticKind: 'compaction-progress', payload: { compactionRecovery: { phase: 'retry_wait', httpStatus: 429, startedAt: 1800000000000, retryAfterMs: 35000 } } })
    const failed = project('session.event.compaction', { status: 'failed', source: 'automatic', compaction_id: 'c', failure_detail: { failure_kind: 'rate_limited', http_status: 429 } })
    expect(failed).toMatchObject({ payload: { compactionFailure: { failureKind: 'rate_limited', httpStatus: 429 } } })
  })
})
