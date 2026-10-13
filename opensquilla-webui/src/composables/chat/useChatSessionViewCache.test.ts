import { effectScope, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '@/types/chat'
import { useChatSessionViewCache } from './useChatSessionViewCache'

const row = (id: string, text = id): ChatMessage => ({ role: 'assistant', text, messageId: id, ts: null })

function harness(limits: { maxSessions?: number; maxBytes?: number; maxMessages?: number } = {}) {
  const scope = effectScope()
  const sessionKey = ref('A')
  const deliveryIdentity = ref<string | null>('gateway-one:owner')
  const messages = ref<ChatMessage[]>([row('a')])
  const cache = scope.run(() => useChatSessionViewCache({ sessionKey, deliveryIdentity, messages, ...limits }))!
  cache.historyInstalled('A')
  cache.observeEpoch('A', 0)
  function switchTo(key: string) {
    sessionKey.value = key
    // The real runtime still clears its authoritative inputs on navigation.
    messages.value = []
  }
  function install(key: string, rows: ChatMessage[]) {
    messages.value = rows
    cache.historyInstalled(key)
  }
  return { cache, scope, sessionKey, deliveryIdentity, messages, switchTo, install }
}

describe('useChatSessionViewCache', () => {
  it('restores A immediately on A/B/A without installing messages or a runtime owner', () => {
    const h = harness()
    h.switchTo('B')
    expect(h.cache.showingPreview.value).toBe(false)
    h.install('B', [row('b')])
    h.switchTo('A')
    expect(h.cache.messages.value.map(m => m.messageId)).toEqual(['a'])
    expect(h.messages.value).toEqual([])
    expect(h.cache.showingPreview.value).toBe(true)
    h.install('A', [row('a', 'updated while away'), row('a2')])
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.messages.value.map(m => m.messageId)).toEqual(['a', 'a2'])
    h.scope.stop()
  })

  it('does not overwrite a cached view with an unconfirmed empty or partial return', () => {
    const h = harness()
    h.switchTo('B')
    h.switchTo('A')
    h.messages.value = [row('partial')]
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    h.cache.historyInstalled('B') // stale completion
    expect(h.cache.showingPreview.value).toBe(true)
    h.install('A', []) // authoritative reset/empty wins
    expect(h.cache.messages.value).toEqual([])
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('clones mutable nested content at departure', () => {
    const h = harness()
    const original = h.messages.value[0]!
    original.reasoning = { text: 'completed reasoning', seconds: 2 }
    h.switchTo('B')
    original.reasoning.text = 'late mutation'
    h.switchTo('A')
    expect(h.cache.messages.value[0]!.reasoning?.text).toBe('completed reasoning')
    h.scope.stop()
  })

  it.each(['gateway-two:owner', null])('retires all cached views when delivery scope becomes %s', identity => {
    const h = harness()
    h.switchTo('B')
    h.install('B', [row('b')])
    h.deliveryIdentity.value = identity
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([])
    expect(h.cache.showingPreview.value).toBe(false)
    // Neither old A nor the old B may be captured under the new identity.
    h.switchTo('B')
    expect(h.cache.messages.value).toEqual([])
    h.scope.stop()
  })

  it('invalidates a deleted inactive session and a missing/reset current session', () => {
    const h = harness()
    h.switchTo('B')
    h.cache.invalidate('A')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.install('A', [row('new-a')])
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(true)
    h.cache.invalidate('A')
    expect(h.cache.messages.value).toEqual([])
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('keeps matching epochs but discards a snapshot from an earlier session incarnation', () => {
    const h = harness()
    h.switchTo('B')
    h.switchTo('A')
    h.cache.observeEpoch('A', 0)
    expect(h.cache.showingPreview.value).toBe(true)
    h.cache.observeEpoch('B', 9) // late foreign snapshot
    expect(h.cache.showingPreview.value).toBe(true)
    h.cache.observeEpoch('A', 1)
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.generation.value).toBe(1)
    h.scope.stop()
  })

  it('keeps freshly installed history when a matching live epoch arrives afterwards', () => {
    const h = harness()
    h.switchTo('B')
    h.switchTo('A')
    h.install('A', [row('a', 'confirmed new history')])
    h.cache.observeEpoch('A', 0)
    expect(h.cache.messages.value).toEqual([row('a', 'confirmed new history')])
    expect(h.cache.generation.value).toBe(0)
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.messages.value).toEqual([row('a', 'confirmed new history')])
    h.scope.stop()
  })

  it('does not recache history installed before a late changed epoch until history is confirmed again', () => {
    const h = harness()
    h.switchTo('B')
    h.switchTo('A')
    h.install('A', [row('a', 'history arrived before new epoch')])
    h.cache.observeEpoch('A', 1)
    // The cache never clears the authoritative message owner. It retires its
    // old incarnation and cannot bless an earlier response for the new epoch.
    expect(h.cache.messages.value).toEqual([row('a', 'history arrived before new epoch')])
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.generation.value).toBe(1)
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.messages.value).toEqual([])
    h.cache.observeEpoch('A', 1)
    h.install('A', [row('a-new-epoch', 'confirmed after epoch change')])
    h.switchTo('B')
    h.switchTo('A')
    h.cache.observeEpoch('A', 1)
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.messages.value).toEqual([row('a-new-epoch', 'confirmed after epoch change')])
    h.scope.stop()
  })

  it('clears all views on transport-generation reset without reusing stream cursors', () => {
    const h = harness()
    h.switchTo('B')
    h.install('B', [row('b')])
    h.cache.invalidate()
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([])
    h.scope.stop()
  })

  it('evicts least recently visited sessions and bounds the saved message window', () => {
    const h = harness({ maxSessions: 2, maxMessages: 2 })
    h.install('A', [row('a1'), row('a2'), row('a3')])
    h.switchTo('B')
    h.install('B', [row('b')])
    h.switchTo('A')
    expect(h.cache.messages.value.map(m => m.messageId)).toEqual(['a2', 'a3'])
    h.switchTo('C')
    h.install('C', [row('c')])
    h.switchTo('D') // C saved; B is LRU
    h.switchTo('B')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('enforces one byte budget across sessions and skips oversized projections', () => {
    const h = harness({ maxBytes: 220 })
    h.install('A', [row('a', '字'.repeat(20))])
    h.switchTo('B')
    h.install('B', [row('b', '字'.repeat(20))])
    h.switchTo('C')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.install('A', [row('oversized', 'x'.repeat(500))])
    h.switchTo('C')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })
})
