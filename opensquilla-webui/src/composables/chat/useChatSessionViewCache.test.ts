import { effectScope, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
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

  it('gives viewport capture only the retained history window while leaving canonical history intact', () => {
    const scope = effectScope()
    const sessionKey = ref('A')
    const deliveryIdentity = ref<string | null>('gateway-one:owner')
    const messages = ref<ChatMessage[]>([row('a1'), row('a2'), row('a3'), row('a4'), row('a5')])
    const capturedWindows: string[][] = []
    const cache = scope.run(() => useChatSessionViewCache({
      sessionKey, deliveryIdentity, messages, maxMessages: 3,
      captureViewport: retainedMessages => {
        capturedWindows.push(retainedMessages.map(message => message.messageId!))
        return { anchor: retainedMessages[0]?.messageId }
      },
    }))!
    cache.historyInstalled('A')
    sessionKey.value = 'B'
    expect(capturedWindows).toEqual([['a3', 'a4', 'a5']])
    expect(messages.value.map(message => message.messageId)).toEqual(['a1', 'a2', 'a3', 'a4', 'a5'])
    messages.value = []
    sessionKey.value = 'A'
    expect(cache.messages.value.map(message => message.messageId)).toEqual(['a3', 'a4', 'a5'])
    expect(cache.restoredViewport.value).toEqual({ anchor: 'a3' })
    expect(capturedWindows).toHaveLength(1) // unresolved B has no view worth capturing
    scope.stop()
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

  it('rejects huge tool content before capturing or serializing the rest of a departing view', () => {
    const scope = effectScope()
    const sessionKey = ref('A')
    const messages = ref<ChatMessage[]>([row('huge', 'x'.repeat(2 * 1024 * 1024))])
    const presentation = vi.fn(() => ({ text: 'live tail' }))
    const viewport = vi.fn(() => ({ anchor: 'huge' }))
    const cache = scope.run(() => useChatSessionViewCache({
      sessionKey, deliveryIdentity: ref('owner'), messages, maxBytes: 1024,
      capturePresentation: presentation, captureViewport: viewport,
    }))!
    cache.historyInstalled('A')
    sessionKey.value = 'B'
    expect(presentation).not.toHaveBeenCalled()
    expect(viewport).not.toHaveBeenCalled()
    expect(messages.value[0]!.text).toHaveLength(2 * 1024 * 1024)
    messages.value = []
    sessionKey.value = 'A'
    expect(cache.showingPreview.value).toBe(false)
    scope.stop()
  })

  it('falls back to authoritative history for cyclic presentation without blocking navigation', () => {
    const h = harness()
    const malformed = h.messages.value[0] as ChatMessage & { nested?: unknown }
    malformed.nested = malformed
    expect(() => h.switchTo('B')).not.toThrow()
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })
})

interface Presentation {
  live: { text: string; thinking: string[] }
  model: string
}

function unprovenIdentityHarness() {
  const scope = effectScope()
  const sessionKey = ref('A')
  const deliveryIdentity = ref<string | null>(null)
  const messages = ref<ChatMessage[]>([row('a')])
  let authority: object | null = {}
  const cache = scope.run(() => useChatSessionViewCache({
    sessionKey, deliveryIdentity, messages, waitForLive: true,
    currentReadAuthority: () => authority,
  }))!
  return {
    scope, cache, sessionKey, deliveryIdentity, messages,
    replaceAuthority: (next: object | null = {}) => { authority = next },
  }
}

describe('useChatSessionViewCache first delivery proof', () => {
  it.each([
    ['history', 'live', 0], ['history', 'live', 1], ['history', 'live', 2],
    ['live', 'history', 0], ['live', 'history', 1], ['live', 'history', 2],
  ] as const)('keeps current read receipts with %s then %s and proof after %s installs', (first, second, proofAfter) => {
    const h = unprovenIdentityHarness()
    h.cache.beginLiveRead('A')
    h.cache.observeEpoch('A', 1)
    const install = (phase: 'history' | 'live') => phase === 'history'
      ? h.cache.historyInstalled('A') : h.cache.liveInstalled('A')
    if (proofAfter === 0) h.deliveryIdentity.value = 'gateway-one:owner'
    install(first)
    expect(h.cache.ready.value).toBe(false)
    if (proofAfter === 1) h.deliveryIdentity.value = 'gateway-one:owner'
    install(second)
    if (proofAfter === 2) h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.showingPreview.value).toBe(false)
    // The retained epoch remains part of the same authority, so a real reset
    // still invalidates both barriers after the initial identity is proven.
    const generation = h.cache.generation.value
    h.cache.observeEpoch('A', 1)
    expect(h.cache.generation.value).toBe(generation)
    h.cache.observeEpoch('A', 2)
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.generation.value).toBe(generation + 1)
    h.scope.stop()
  })

  it.each([false, true])('rejects receipts from a replaced or missing read lease (missing=%s)', missing => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.replaceAuthority(missing ? null : {})
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(false)
    h.scope.stop()
  })

  it('adopts only the matching half when the read lease changes between receipts', () => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A')
    h.replaceAuthority()
    h.cache.liveInstalled('A')
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(false)
    h.cache.historyInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })

  it('keeps same-lease readable history on first proof without promoting it to a navigation snapshot', () => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A', false)
    h.cache.liveInstalled('A')
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(true)
    h.sessionKey.value = 'B'
    h.messages.value = []
    h.sessionKey.value = 'A'
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.messages.value).toEqual([])
    h.scope.stop()
  })

  it.each([false, true])('rejects readable history from a replaced or missing first-proof lease (missing=%s)', missing => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A', false)
    h.replaceAuthority(missing ? null : {})
    h.cache.liveInstalled('A')
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(false)
    h.scope.stop()
  })

  it('does not bless an unconfirmed live receipt when identity arrives', () => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A', false)
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(false)
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })

  it('requires new receipts after revocation and a second proof even if reads finish while unproven', () => {
    const h = unprovenIdentityHarness()
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A')
    h.deliveryIdentity.value = 'gateway-one:owner'
    expect(h.cache.ready.value).toBe(true)
    h.deliveryIdentity.value = null
    expect(h.cache.ready.value).toBe(false)
    h.replaceAuthority()
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A')
    h.deliveryIdentity.value = 'gateway-two:owner'
    expect(h.cache.ready.value).toBe(false)
    h.cache.historyInstalled('A')
    expect(h.cache.ready.value).toBe(false)
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })
})

interface Viewport {
  scrollTop: number
  anchor?: string
}

function liveHarness(limits: { maxBytes?: number; maxSessions?: number } = {}) {
  const scope = effectScope()
  const sessionKey = ref('A')
  const deliveryIdentity = ref<string | null>('gateway-one:owner')
  const messages = ref<ChatMessage[]>([row('a')])
  const presentation = ref<Presentation>({ live: { text: 'A is writing', thinking: ['A reasoning'] }, model: 'model-A' })
  const viewport = ref<Viewport>({ scrollTop: 320, anchor: 'a' })
  const cache = scope.run(() => useChatSessionViewCache({
    sessionKey, deliveryIdentity, messages, waitForLive: true, ...limits,
    capturePresentation: () => presentation.value,
    captureViewport: () => viewport.value,
  }))!
  cache.observeEpoch('A', 0)
  cache.historyInstalled('A')
  cache.liveInstalled('A')
  function switchTo(key: string) {
    sessionKey.value = key
    messages.value = []
    presentation.value = { live: { text: '', thinking: [] }, model: '' }
    viewport.value = { scrollTop: 0 }
  }
  function returnToA() {
    switchTo('B')
    switchTo('A')
  }
  return { cache, scope, sessionKey, deliveryIdentity, messages, presentation, viewport, switchTo, returnToA }
}

describe('useChatSessionViewCache complete presentation handoff', () => {
  it('retains a readable degraded view through repeated same-session recovery without caching it for navigation', () => {
    const h = liveHarness()
    h.cache.historyInstalled('A', false)
    expect(h.cache.ready.value).toBe(true)
    h.cache.beginLiveRead('A')
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    expect(h.cache.restoredViewport.value).toBeUndefined()
    h.messages.value = [row('partial')]
    h.presentation.value.live.text = 'partial replay'
    h.cache.liveInstalled('A', false)
    h.cache.beginLiveRead('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.messages.value).toEqual([])
    expect(h.cache.presentation.value).toBeUndefined()
    h.scope.stop()
  })

  it('uses the latest readable degraded view for recovery while preserving the older authoritative navigation snapshot', () => {
    const h = liveHarness()
    h.returnToA()
    h.messages.value = [row('readable-history')]
    h.presentation.value.live.text = 'readable live tail'
    h.cache.historyInstalled('A', false)
    h.cache.liveInstalled('A')
    h.cache.beginLiveRead('A')
    expect(h.cache.messages.value).toEqual([row('readable-history')])
    expect(h.cache.presentation.value?.live.text).toBe('readable live tail')
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    h.scope.stop()
  })

  it('releases a same-session degraded preview after confirmed recovery and can cache the repaired view', () => {
    const h = liveHarness()
    h.cache.historyInstalled('A', false)
    h.cache.beginLiveRead('A')
    h.messages.value = [row('repaired')]
    h.presentation.value.live.text = 'repaired live'
    h.cache.historyInstalled('A')
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.showingPreview.value).toBe(false)
    h.returnToA()
    expect(h.cache.messages.value).toEqual([row('repaired')])
    expect(h.cache.presentation.value?.live.text).toBe('repaired live')
    h.scope.stop()
  })

  it.each(['epoch', 'identity', 'revoked', 'reset', 'terminal'] as const)(
    'retires a same-session degraded preview on %s without recapturing it', cause => {
      const h = liveHarness()
      h.cache.historyInstalled('A', false)
      h.cache.beginLiveRead('A')
      expect(h.cache.showingPreview.value).toBe(true)
      if (cause === 'epoch') h.cache.observeEpoch('A', 1)
      else if (cause === 'identity') h.deliveryIdentity.value = 'gateway-two:owner'
      else if (cause === 'revoked') h.deliveryIdentity.value = null
      else if (cause === 'terminal') h.cache.retirePreview('A')
      else h.cache.invalidate('A')
      expect(h.cache.showingPreview.value).toBe(false)
      h.cache.beginLiveRead('A')
      expect(h.cache.showingPreview.value).toBe(false)
      h.returnToA()
      expect(h.cache.showingPreview.value).toBe(false)
      h.scope.stop()
    },
  )

  it.each([false, true])(
    'keeps already readable live output through failed retries without granting navigation authority (history confirmed=%s)',
    historyConfirmed => {
      const h = liveHarness()
      h.switchTo('C')
      h.messages.value = [row('readable-history')]
      h.presentation.value.live.text = 'Accepted live output after a degraded read'
      h.cache.historyInstalled('C', historyConfirmed)
      h.cache.liveInstalled('C', false)
      expect(h.cache.ready.value).toBe(true)

      for (let attempt = 0; attempt < 3; attempt++) {
        h.cache.beginLiveRead('C')
        expect(h.cache.ready.value).toBe(false)
        expect(h.cache.showingPreview.value).toBe(true)
        expect(h.cache.messages.value).toEqual([row('readable-history')])
        expect(h.cache.presentation.value?.live.text).toBe('Accepted live output after a degraded read')
        expect(h.cache.restoredViewport.value).toBeUndefined()
        h.messages.value = [row(`partial-${attempt}`)]
        h.presentation.value.live.text = `Partial replay ${attempt}`
        h.cache.liveInstalled('C', false)
        expect(h.cache.ready.value).toBe(false)
        expect(h.cache.presentation.value?.live.text).toBe('Accepted live output after a degraded read')
      }

      h.switchTo('B')
      h.switchTo('C')
      expect(h.cache.showingPreview.value).toBe(false)
      expect(h.cache.messages.value).toEqual([])
      expect(h.cache.presentation.value).toBeUndefined()
      h.scope.stop()
    },
  )

  it('replaces an unconfirmed readable recovery preview only after confirmed live installation', () => {
    const h = liveHarness()
    h.switchTo('C')
    h.messages.value = [row('readable-history')]
    h.presentation.value.live.text = 'Accepted live output'
    h.cache.historyInstalled('C')
    h.cache.liveInstalled('C', false)
    h.cache.beginLiveRead('C')
    h.messages.value = [row('recovered-history')]
    h.presentation.value.live.text = 'Recovered live output'
    h.cache.historyInstalled('C')
    expect(h.cache.presentation.value?.live.text).toBe('Accepted live output')
    h.cache.liveInstalled('C')
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.ready.value).toBe(true)
    h.switchTo('B')
    h.switchTo('C')
    expect(h.cache.messages.value).toEqual([row('recovered-history')])
    expect(h.cache.presentation.value?.live.text).toBe('Recovered live output')
    h.scope.stop()
  })

  it('bounds the temporary degraded preview with the same byte budget', () => {
    const h = liveHarness({ maxBytes: 300 })
    h.cache.historyInstalled('A', false)
    h.presentation.value.live.text = '字'.repeat(200)
    h.cache.beginLiveRead('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('freezes the complete same-session view before recovery and never captures a partial reread', () => {
    const h = liveHarness()
    h.cache.beginLiveRead('A')
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    // A same-session reread does not ask the view to restore an old scroll
    // position: the existing viewport stays in place throughout recovery.
    expect(h.cache.restoredViewport.value).toBeUndefined()
    h.messages.value = [row('partial')]
    h.presentation.value.live.text = 'incomplete recovery'
    h.cache.beginLiveRead('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    h.scope.stop()
  })

  it('keeps installed history and an existing viewport through same-session recovery', () => {
    const h = liveHarness()
    h.returnToA()
    h.messages.value = [row('a-current')]
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A')
    const restoredViewport = h.cache.restoredViewport.value
    h.cache.beginLiveRead('A')
    expect(h.cache.restoredViewport.value).toBe(restoredViewport)
    expect(h.cache.ready.value).toBe(false)
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('retires a terminal task preview without reviving it or declaring the next live read ready', () => {
    const h = liveHarness()
    h.returnToA()
    const restoredViewport = h.cache.restoredViewport.value
    h.messages.value = [row('a-completed')]
    h.cache.historyInstalled('A')
    h.cache.retirePreview('A')
    expect(h.cache.messages.value).toEqual([row('a-completed')])
    expect(h.cache.presentation.value).toBeUndefined()
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.restoredViewport.value).toBe(restoredViewport)
    h.cache.beginLiveRead('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it('keeps terminal history installed while waiting for the replacement live confirmation', () => {
    const h = liveHarness()
    h.returnToA()
    h.cache.historyInstalled('A')
    h.cache.retirePreview('A')
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })

  it('ignores foreign recovery callbacks and lets a cold recovery show readable history', () => {
    const h = liveHarness()
    h.cache.beginLiveRead('B')
    h.cache.retirePreview('B')
    expect(h.cache.ready.value).toBe(true)
    h.switchTo('C')
    h.cache.beginLiveRead('C')
    h.messages.value = [row('c')]
    h.cache.historyInstalled('C')
    h.cache.liveInstalled('C', false)
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.messages.value).toEqual([row('c')])
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it.each(['history', 'live'] as const)('keeps one detached view until both sides are installed (%s first)', first => {
    const h = liveHarness()
    h.returnToA()
    expect(h.cache.presentation.value?.model).toBe('model-A')
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    expect(h.cache.restoredViewport.value).toEqual({ scrollTop: 320, anchor: 'a' })
    expect(h.messages.value).toEqual([])
    expect(h.presentation.value.live.text).toBe('')
    expect(h.cache.ready.value).toBe(false)

    h.messages.value = [row('a', 'authoritative history'), row('a-new')]
    h.presentation.value = { live: { text: 'new authoritative tail', thinking: [] }, model: 'model-A2' }
    if (first === 'history') h.cache.historyInstalled('A')
    else h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.messages.value).toEqual([row('a')])
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')

    if (first === 'history') h.cache.liveInstalled('A')
    else h.cache.historyInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.presentation.value).toBeUndefined()
    expect(h.cache.messages.value).toEqual(h.messages.value)
    h.scope.stop()
  })

  it('does not mark a cold session ready when only its live tail has arrived', () => {
    const h = liveHarness()
    h.switchTo('C')
    h.cache.liveInstalled('C')
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.presentation.value).toBeUndefined()
    h.cache.historyInstalled('A') // late response for another session
    expect(h.cache.ready.value).toBe(false)
    h.messages.value = [row('c')]
    h.cache.historyInstalled('C')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })

  it('preserves the previous complete view when history fails and a late live response arrives', () => {
    const h = liveHarness()
    h.returnToA()
    // A failed history request never calls historyInstalled. Its live sibling
    // may still succeed, but cannot replace or recapture a partial render.
    h.cache.liveInstalled('A')
    h.messages.value = [row('partial')]
    h.presentation.value.live.text = 'new live tail without its user message'
    expect(h.cache.ready.value).toBe(false)
    expect(h.cache.messages.value).toEqual([row('a')])
    h.switchTo('B')
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A')
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([row('a')])
    expect(h.cache.presentation.value?.live.text).toBe('A is writing')
    expect(h.cache.ready.value).toBe(false)
    h.scope.stop()
  })

  it.each(['history', 'live'] as const)(
    'keeps the complete view while a rejected live read recovers (%s first)', first => {
      const h = liveHarness()
      h.returnToA()
      h.messages.value = [row('new-history')]
      h.presentation.value.live.text = 'incomplete recovery'
      if (first === 'history') h.cache.historyInstalled('A')
      else h.cache.liveInstalled('A', false)
      expect(h.cache.ready.value).toBe(false)
      expect(h.cache.showingPreview.value).toBe(true)
      if (first === 'history') h.cache.liveInstalled('A', false)
      else h.cache.historyInstalled('A')
      expect(h.cache.ready.value).toBe(false)
      expect(h.cache.messages.value).toEqual([row('a')])
      expect(h.cache.showingPreview.value).toBe(true)
      h.switchTo('B')
      h.switchTo('A')
      expect(h.cache.messages.value).toEqual([row('a')])
      expect(h.cache.presentation.value?.live.text).toBe('A is writing')
      expect(h.cache.presentation.value?.model).toBe('model-A')
      h.scope.stop()
    },
  )

  it('still releases readable cold history when live recovery fails', () => {
    const h = liveHarness()
    h.switchTo('C')
    h.messages.value = [row('c')]
    h.cache.historyInstalled('C')
    h.cache.liveInstalled('C', false)
    expect(h.cache.ready.value).toBe(true)
    expect(h.cache.messages.value).toEqual([row('c')])
    expect(h.cache.showingPreview.value).toBe(false)
    h.switchTo('B')
    h.switchTo('C')
    expect(h.cache.showingPreview.value).toBe(false)
    h.scope.stop()
  })

  it.each(['history', 'live'] as const)(
    'releases failed history display without overwriting the last complete snapshot (%s first)', first => {
      const h = liveHarness()
      h.returnToA()
      h.messages.value = [row('incomplete-history')]
      h.presentation.value.live.text = 'live tail with failed history'
      if (first === 'history') h.cache.historyInstalled('A', false)
      else h.cache.liveInstalled('A')
      expect(h.cache.ready.value).toBe(false)
      expect(h.cache.showingPreview.value).toBe(true)
      if (first === 'history') h.cache.liveInstalled('A')
      else h.cache.historyInstalled('A', false)
      expect(h.cache.ready.value).toBe(true)
      expect(h.cache.showingPreview.value).toBe(false)
      expect(h.cache.messages.value).toEqual([row('incomplete-history')])
      // An additional live confirmation cannot promote failed history to a
      // complete cache entry. The next visit still restores the prior view.
      h.cache.liveInstalled('A')
      h.switchTo('B')
      h.switchTo('A')
      expect(h.cache.messages.value).toEqual([row('a')])
      expect(h.cache.presentation.value?.live.text).toBe('A is writing')
      expect(h.cache.restoredViewport.value).toEqual({ scrollTop: 320, anchor: 'a' })
      h.scope.stop()
    },
  )

  it('can save the repaired complete view after an earlier degraded display', () => {
    const h = liveHarness()
    h.returnToA()
    h.messages.value = [row('recovered-history')]
    h.cache.historyInstalled('A')
    h.cache.liveInstalled('A', false)
    h.presentation.value = { live: { text: 'recovered live', thinking: [] }, model: 'model-recovered' }
    h.cache.liveInstalled('A', true)
    h.switchTo('B')
    h.switchTo('A')
    expect(h.cache.messages.value).toEqual([row('recovered-history')])
    expect(h.cache.presentation.value?.live.text).toBe('recovered live')
    expect(h.cache.presentation.value?.model).toBe('model-recovered')
    h.scope.stop()
  })

  it.each(['epoch', 'identity', 'reset'] as const)(
    'retires both handoff sides after %s invalidation and cannot bless old history with a late live reply', cause => {
      const h = liveHarness()
      h.returnToA()
      h.messages.value = [row('old-scope-history')]
      h.cache.historyInstalled('A')
      if (cause === 'epoch') h.cache.observeEpoch('A', 1)
      else if (cause === 'identity') h.deliveryIdentity.value = 'gateway-two:owner'
      else h.cache.invalidate('A')
      expect(h.cache.ready.value).toBe(false)
      expect(h.cache.presentation.value).toBeUndefined()
      expect(h.cache.restoredViewport.value).toBeUndefined()
      h.cache.liveInstalled('A')
      expect(h.cache.ready.value).toBe(false)
      h.switchTo('B')
      h.switchTo('A')
      expect(h.cache.showingPreview.value).toBe(false)
      h.scope.stop()
    },
  )

  it('does not pair a live response from the old epoch with newly confirmed history', () => {
    const h = liveHarness()
    h.returnToA()
    h.cache.liveInstalled('A')
    h.cache.observeEpoch('A', 1)
    h.messages.value = [row('new-epoch-history')]
    h.cache.historyInstalled('A')
    expect(h.cache.ready.value).toBe(false)
    h.cache.liveInstalled('A')
    expect(h.cache.ready.value).toBe(true)
    h.scope.stop()
  })

  it('detaches live/model/viewport values without restoring any authoritative owner state', () => {
    const h = liveHarness()
    const previous = h.presentation.value
    const previousViewport = h.viewport.value
    h.switchTo('B')
    previous.live.text = 'late mutation'
    previous.live.thinking.push('late reasoning')
    previous.model = 'late model'
    previousViewport.scrollTop = 9999
    h.switchTo('A')
    expect(h.cache.presentation.value).toEqual({ live: { text: 'A is writing', thinking: ['A reasoning'] }, model: 'model-A' })
    expect(h.cache.restoredViewport.value).toEqual({ scrollTop: 320, anchor: 'a' })
    expect(h.messages.value).toEqual([])
    expect(h.presentation.value).toEqual({ live: { text: '', thinking: [] }, model: '' })
    expect(h.viewport.value).toEqual({ scrollTop: 0 })
    h.scope.stop()
  })

  it.each(['presentation', 'viewport'] as const)('charges %s payloads to the same byte budget', field => {
    const h = liveHarness({ maxBytes: 300 })
    if (field === 'presentation') h.presentation.value.live.text = '字'.repeat(200)
    else h.viewport.value.anchor = '字'.repeat(200)
    h.returnToA()
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.presentation.value).toBeUndefined()
    expect(h.cache.restoredViewport.value).toBeUndefined()
    h.scope.stop()
  })

  it('evicts snapshots using the combined history, live/model and viewport size across sessions', () => {
    const h = liveHarness({ maxBytes: 450 })
    h.presentation.value.live.text = 'x'.repeat(120)
    h.switchTo('B')
    h.messages.value = [row('b')]
    h.presentation.value = { live: { text: 'x'.repeat(120), thinking: [] }, model: 'model-B' }
    h.cache.historyInstalled('B')
    h.cache.liveInstalled('B')
    h.switchTo('C')
    h.switchTo('A')
    expect(h.cache.showingPreview.value).toBe(false)
    h.switchTo('B')
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.presentation.value?.model).toBe('model-B')
    h.scope.stop()
  })
})
