import type { ConversationCompactionContent, ConversationProviderActivity } from '@/modules/conversationEventContent'
import type { StatusPart } from '@/types/parts'
import { normalizeProviderRecovery } from './providerRecovery'
import { compactionSkippedProviderFailure } from './compactionStatus'

/** Shared main/side normalization. Keep raw provider messages out of activity labels. */
export function providerActivityStatus(payload: ConversationProviderActivity): StatusPart | null {
  const recovery = normalizeProviderRecovery(payload)
  if (!recovery) return null
  const common = { at: Number(payload.started_at || Date.now()), providerRecovery: recovery }
  const seconds = Math.ceil((recovery.retryAfterMs || 0) / 1000)
  const attempt = recovery.attempt || 0
  const limit = recovery.limit || 0
  switch (recovery.phase) {
    case 'requesting': return { ...common, action: 'provider:requesting', label: 'Waiting for model' }
    case 'reasoning': return { ...common, action: 'provider:reasoning', label: 'Thinking deeply' }
    case 'retry_wait': return recovery.reason === 'rate_limited'
      ? { ...common, action: `provider:rate_limited:${seconds}`, label: `Rate limited · ${seconds}s` }
      : { ...common, action: `provider:retry_wait:${seconds}`, label: `Waiting to retry · ${seconds}s` }
    case 'retrying': return { ...common, action: `provider:retrying:${attempt}:${limit}`, label: limit > 0 ? `Retrying ${attempt}/${limit}` : `Retrying · attempt ${attempt}` }
    case 'fallback': return { ...common, action: 'provider:fallback', label: 'Switching to backup model' }
  }
}

export function compactionActivityStatus(payload: ConversationCompactionContent, previous?: StatusPart): StatusPart {
  const raw = String(payload.status || '').toLowerCase()
  const state = compactionSkippedProviderFailure(raw, payload.reason || payload.skip_reason, payload.compactionFailure?.failureKind)
    ? 'failed' : raw === 'skipped' ? 'skipped' : raw === 'stale' ? 'stale' : raw === 'cancelled' ? 'cancelled'
      : ['completed', 'emergency_ephemeral'].includes(raw) ? 'completed'
        : ['failed', 'error', 'timed_out'].includes(raw) ? 'failed' : 'running'
  const recovery = state === 'running' ? payload.compactionRecovery ?? (previous?.state === 'running' ? previous.providerRecovery : undefined) : undefined
  return {
    action: 'context_compaction', label: '', at: Number(payload.emitted_at || payload.started_at || Date.now()),
    id: String(payload.compaction_id || 'current'), category: 'maintenance', state,
    source: String(payload.source || 'automatic'),
    durability: raw === 'emergency_ephemeral' ? 'request_scoped' : String(payload.durability || ''),
    detail: String(payload.detail || payload.phase || ''), reason: String(payload.reason || payload.skip_reason || ''),
    providerRecovery: recovery,
    httpStatus: state === 'failed' ? payload.compactionFailure?.httpStatus : state === 'running' ? recovery?.httpStatus : undefined,
    failureKind: state === 'failed' ? payload.compactionFailure?.failureKind : undefined,
  }
}
