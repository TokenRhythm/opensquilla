import { effectScope, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import type { ChatMaintenanceEvent, ChatMessage } from '@/types/chat'
import { useChatSessionViewCache } from './useChatSessionViewCache'

const ID = 'cmp-retained'
const terminal = (overrides: Partial<ChatMaintenanceEvent> = {}): ChatMaintenanceEvent => ({
  kind: 'context_compaction', compactionId: ID, source: 'manual', state: 'completed', durability: 'durable', ...overrides,
})

function harness(maxBytes?: number) {
  const scope = effectScope()
  const sessionKey = ref('A')
  const deliveryIdentity = ref<string | null>('gateway:owner')
  const authority = ref({ lease: 'initial' })
  const rows: ChatMessage[] = [
    { role: 'user', messageId: 'query-a', text: 'Keep this query', ts: 1 },
    { role: 'assistant', messageId: 'tool-a', text: 'Old tool output', ts: 2 },
    { role: 'maintenance', clientId: 'receipt-a', text: '', ts: 3, maintenance: terminal({ state: 'running', durability: '' }) },
  ]
  const messages = ref<ChatMessage[]>(rows)
  const cache = scope.run(() => useChatSessionViewCache({
    sessionKey, deliveryIdentity, messages, currentReadAuthority: () => authority.value, waitForLive: true, maxBytes,
  }))!
  cache.observeEpoch('A', 7)
  cache.historyInstalled('A')
  cache.liveInstalled('A')
  sessionKey.value = 'B'
  messages.value = []
  authority.value = { lease: 'return' }
  sessionKey.value = 'A'
  const proof = () => ({ identity: deliveryIdentity.value, authority: authority.value, epoch: 7 })
  return { scope, sessionKey, deliveryIdentity, authority, messages, cache, proof }
}

describe('cached compaction terminal presentation', () => {
  it('patches the same anchored row under a new valid lease without installing history, live, or controls', () => {
    const h = harness()
    try {
      const before = h.cache.messages.value
      expect(h.cache.reconcileCompactionTerminal('A', terminal(), h.proof())).toBe(true)
      expect(h.cache.messages.value).toHaveLength(3)
      expect(h.cache.messages.value.slice(0, 2)).toEqual(before.slice(0, 2))
      expect(h.cache.messages.value[2]).toMatchObject({ clientId: 'receipt-a', ts: 3, maintenance: { state: 'completed' } })
      expect(h.messages.value).toEqual([])
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.cache.ready.value).toBe(false)
      // A second switch must not restore the stale entry from the Map.
      h.sessionKey.value = 'B'
      h.sessionKey.value = 'A'
      expect(h.cache.messages.value[2]?.maintenance?.state).toBe('completed')
    } finally { h.scope.stop() }
  })

  it.each(['old-lease', 'wrong-key', 'epoch', 'identity', 'unknown-id', 'progress'] as const)(
    'rejects %s without touching users, order, or the preview barrier', mode => {
      const h = harness()
      try {
        const before = h.cache.messages.value
        const proof = h.proof()
        if (mode === 'old-lease') proof.authority = { lease: 'initial' }
        if (mode === 'epoch') proof.epoch = 8
        if (mode === 'identity') proof.identity = 'other-owner'
        const receipt = terminal(mode === 'unknown-id' ? { compactionId: 'other-id' }
          : mode === 'progress' ? { state: 'running' } : {})
        expect(h.cache.reconcileCompactionTerminal(mode === 'wrong-key' ? 'B' : 'A', receipt, proof)).toBe(false)
        expect(h.cache.messages.value).toBe(before)
        expect(h.cache.showingPreview.value).toBe(true)
        expect(h.cache.ready.value).toBe(false)
      } finally { h.scope.stop() }
    },
  )

  it('keeps a terminal monotonic and preserves a durable receipt against duplicate metadata omissions', () => {
    const h = harness()
    try {
      h.cache.reconcileCompactionTerminal('A', terminal({ historyArchived: true }), h.proof())
      expect(h.cache.reconcileCompactionTerminal('A', terminal({ durability: '' }), h.proof())).toBe(true)
      expect(h.cache.messages.value[2]?.maintenance?.durability).toBe('durable')
      expect(h.cache.reconcileCompactionTerminal('A', terminal({ state: 'failed' }), h.proof())).toBe(false)
      expect(h.cache.messages.value[2]?.maintenance?.state).toBe('completed')
    } finally { h.scope.stop() }
  })

  it('uses a fresh current-lease epoch for legacy frames, never the preview\'s old lease', () => {
    const h = harness()
    try {
      const proof = { ...h.proof(), epoch: null }
      expect(h.cache.reconcileCompactionTerminal('A', terminal(), proof)).toBe(false)
      h.cache.observeEpoch('A', 7)
      expect(h.cache.reconcileCompactionTerminal('A', terminal(), proof)).toBe(true)
      expect(h.cache.messages.value[2]?.maintenance?.state).toBe('completed')
    } finally { h.scope.stop() }
  })

  it.each(['history-first', 'epoch-first'] as const)('uses canonical durable history proof before deferred metadata (%s)', order => {
    const h = harness()
    try {
      h.messages.value = [{ role: 'maintenance', text: '', ts: 4, restoredFromHistory: true,
        maintenance: terminal({ historyArchived: true, canonicalComplete: true }) }]
      if (order === 'epoch-first') h.cache.observeEpoch('A', 7)
      h.cache.historyInstalled('A', true, [h.messages.value[0]!.maintenance!])
      if (order === 'history-first') {
        expect(h.cache.messages.value[2]?.maintenance?.state).toBe('running')
        h.cache.observeEpoch('A', 7)
      }
      expect(h.cache.messages.value[2]?.maintenance?.state).toBe('completed')
      expect(h.cache.messages.value[0]?.messageId).toBe('query-a')
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.cache.ready.value).toBe(false)
    } finally { h.scope.stop() }
  })

  it.each(['fallback', 'local-terminal', 'changed-epoch'] as const)('does not mistake %s for matching canonical authority', mode => {
    const h = harness()
    try {
      h.cache.observeEpoch('A', mode === 'changed-epoch' ? 8 : 7)
      h.messages.value = [{ role: 'maintenance', text: '', ts: 4, restoredFromHistory: mode !== 'local-terminal',
        maintenance: terminal({ historyArchived: true }) }]
      h.cache.historyInstalled('A', mode !== 'fallback', mode === 'local-terminal' ? [] : [h.messages.value[0]!.maintenance!])
      if (mode === 'changed-epoch') expect(h.cache.showingPreview.value).toBe(false)
      else expect(h.cache.messages.value[2]?.maintenance?.state).toBe('running')
    } finally { h.scope.stop() }
  })

  it('charges an oversized terminal receipt to the existing capture budget', () => {
    const h = harness(1200)
    try {
      const before = h.cache.messages.value
      expect(h.cache.reconcileCompactionTerminal('A', terminal({ detail: '字'.repeat(1200) }), h.proof())).toBe(false)
      expect(h.cache.messages.value).toBe(before)
    } finally { h.scope.stop() }
  })
})
