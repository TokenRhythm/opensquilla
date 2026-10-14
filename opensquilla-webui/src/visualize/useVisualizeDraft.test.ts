import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { useVisualizeDraft } from './useVisualizeDraft'
import type { VisualizeReceipt } from './types'
import { useChatSessionViewCache } from '@/composables/chat/useChatSessionViewCache'
import type { ChatMessage } from '@/types/chat'

const receipt = { documentId: 'doc_one', revisionId: 'rev_one', name: 'experiment.html' } as VisualizeReceipt
const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })
function harness() {
  const scope = effectScope(); scopes.push(scope)
  const sessionKey = ref('A'), deliveryIdentity = ref<string | null>('owner'), generation = ref(0), ready = ref(true)
  const draft = scope.run(() => useVisualizeDraft({ sessionKey, deliveryIdentity, generation, available: key => key === sessionKey.value && ready.value }))!
  return { draft, sessionKey, deliveryIdentity, generation, ready }
}

describe('Visualize composer snapshots', () => {
  it('freezes a click-time copy without treating later canvas edits as attached input', () => {
    const h = harness(), data = { days: 5, nested: { budget: 6000 } }
    h.draft.attach('A', receipt, { summary: 'Five days', data })
    const send = h.draft.capture()!
    data.days = 9; data.nested.budget = 9000
    const message = send.message('Plan with these conditions')
    const payload = JSON.parse(message.split('\n').slice(-1)[0]!)
    expect(payload).toEqual({ days: 5, nested: { budget: 6000 } })
    expect(send.snapshot).toMatchObject({ sessionKey: 'A', documentId: 'doc_one', revisionId: 'rev_one' })
    expect(message).toContain('Canvas conditions · experiment.html\nFive days')
    expect(message).not.toContain('rev_one')
    expect(message.startsWith('Plan with these conditions\n\n')).toBe(true)
    expect(h.draft.snapshot.value).not.toBeNull()
  })

  it('keeps context until accepted and a late acceptance cannot clear a newer attachment', () => {
    const h = harness()
    h.draft.attach('A', receipt, { summary: 'First', data: { amount: 1 } })
    const first = h.draft.capture()!
    expect(first.isCurrent()).toBe(true)
    h.draft.attach('A', receipt, { summary: 'Second', data: { amount: 2 } })
    first.accepted()
    expect(h.draft.snapshot.value?.summary).toBe('Second')
    expect(first.isCurrent()).toBe(false)
    h.draft.capture()!.accepted()
    expect(h.draft.snapshot.value).toBeNull()
  })

  it('rejects cached, foreign-session and invalid attachments without replacing the current draft', () => {
    const h = harness()
    h.draft.attach('A', receipt, { summary: 'Kept' })
    h.ready.value = false
    expect(h.draft.attach('A', receipt, { summary: 'Cached' })).toBeNull()
    expect(h.draft.capture()).toBeNull()
    h.ready.value = true
    expect(h.draft.attach('B', receipt, { summary: 'Foreign' })).toBeNull()
    expect(h.draft.attach('A', receipt, { summary: '' })).toBeNull()
    expect(h.draft.snapshot.value?.summary).toBe('Kept')
  })

  it.each(['session', 'identity', 'generation'] as const)('clears context synchronously on %s changes', change => {
    const h = harness()
    h.draft.attach('A', receipt, { summary: 'Private conditions' })
    const first = h.draft.capture()!
    if (change === 'session') h.sessionKey.value = 'B'
    if (change === 'identity') h.deliveryIdentity.value = 'other-owner'
    if (change === 'generation') h.generation.value++
    expect(h.draft.snapshot.value).toBeNull()
    expect(first.isCurrent()).toBe(false)
  })

  it('keeps matching receipts with cached live text while authoritative state resets and gates attachment until recovery', () => {
    const scope = effectScope(); scopes.push(scope)
    const sessionKey = ref('A'), deliveryIdentity = ref<string | null>('owner'), messages = ref<ChatMessage[]>([])
    const live = ref({ text: '`visualize:doc_one:rev_one`', receipts: [receipt] })
    const cache = scope.run(() => useChatSessionViewCache({ sessionKey, deliveryIdentity, messages, waitForLive: true,
      capturePresentation: () => ({ live: live.value }),
    }))!
    const draft = scope.run(() => useVisualizeDraft({ sessionKey, deliveryIdentity, generation: cache.generation,
      available: key => key === sessionKey.value && cache.ready.value && !cache.showingPreview.value,
    }))!
    cache.historyInstalled('A'); cache.liveInstalled('A')
    sessionKey.value = 'B'; live.value = { text: 'B', receipts: [] }
    sessionKey.value = 'A'; live.value = { text: '', receipts: [] }
    expect(cache.presentation.value?.live).toEqual({ text: '`visualize:doc_one:rev_one`', receipts: [receipt] })
    expect(draft.attach('A', receipt, { summary: 'Cached' })).toBeNull()
    cache.historyInstalled('A')
    expect(draft.attach('A', receipt, { summary: 'Still cached' })).toBeNull()
    cache.liveInstalled('A')
    expect(draft.attach('A', receipt, { summary: 'Live' })).toEqual({ summary: 'Live' })
  })
})
