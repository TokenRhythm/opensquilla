import { effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { ModelCatalogResult, ModelDescriptor } from '@/modules/providerConfiguration'
import type { ModelRoutingMode } from '@/types/modelRouting'
import { useNewTaskModelSelection } from './useNewTaskModelSelection'

const MODEL: ModelDescriptor = {
  id: 'test-model', name: 'Test model', provider: 'test-provider', contextWindow: 1000,
  maxOutputTokens: 100, capabilities: [], pricing: { inputPer1k: 0, outputPer1k: 0 },
  source: 'gateway', reasoningFormat: '', metadata: null,
}
const PIN = { model: MODEL.id, provider: MODEL.provider }
function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
}
function harness(input: { catalogAvailable?: boolean; capable?: boolean; storage?: ReturnType<typeof memoryStorage>; list?: () => Promise<ModelCatalogResult> } = {}) {
  const sessionKey = ref('agent:main:webchat:one')
  const draft = ref(true)
  const capable = ref(input.capable ?? true)
  const catalogAvailable = input.catalogAvailable === undefined ? undefined : ref(input.catalogAvailable)
  const routingMode = ref<ModelRoutingMode>('off')
  const busy = ref(false)
  const connectionEpoch = ref(1)
  const storage = input.storage ?? memoryStorage()
  const list = vi.fn(input.list ?? (async () => ({ models: [MODEL], errors: [] })))
  const scope = effectScope()
  const api = scope.run(() => useNewTaskModelSelection({
    catalog: { list }, sessionKey, isDraft: () => draft.value,
    capable, catalogAvailable, routingMode, busy, connectionEpoch, storage,
  }))!
  return { api, list, sessionKey, draft, capable, catalogAvailable, routingMode, busy, connectionEpoch, storage, scope }
}

describe('new-task model selection', () => {
  it('shares one catalog with existing tasks without enabling a draft pin there', async () => {
    const h = harness({ catalogAvailable: true })
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledTimes(1)
    h.api.select(PIN)
    h.draft.value = false
    await nextTick()
    expect(h.api.available.value).toBe(false)
    expect(h.api.selection.value).toBeNull()
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.list).toHaveBeenCalledTimes(1)
    expect(await h.api.selectWithRouting(PIN, async () => true)).toBe(false)
    expect(h.api.select(PIN)).toBe(false)
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledTimes(2)
    h.scope.stop()
  })

  it('invalidates a shared catalog on disconnect and rejects its late response', async () => {
    let accept!: (value: ModelCatalogResult) => void
    const h = harness({ catalogAvailable: true, list: () => new Promise(resolve => { accept = resolve }) })
    h.draft.value = false
    h.catalogAvailable!.value = false
    await nextTick()
    accept({ models: [MODEL], errors: [] })
    await nextTick()
    expect(h.api.models.value).toEqual([])
    expect(h.api.loading.value).toBe(false)
    expect(h.api.selection.value).toBeNull()
    h.scope.stop()
  })

  it('does not query an unsupported gateway, and loads when its capability appears', async () => {
    const h = harness({ capable: false })
    expect(h.list).not.toHaveBeenCalled()
    expect(h.api.select(PIN)).toBe(false)
    h.capable.value = true
    await nextTick()
    await h.api.refresh()
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.list).toHaveBeenCalledWith({ scope: 'configured', signal: expect.any(AbortSignal) })
    expect(h.api.select(PIN)).toBe(true)
    h.scope.stop()
  })

  it('keeps partial provider failures alongside the usable catalog and selection', async () => {
    const errors = [{ provider: 'offline', kind: 'unavailable', detail: 'Provider offline' }]
    const h = harness({ list: async () => ({ models: [MODEL], errors }) })
    await h.api.refresh()
    expect(h.api.providerErrors.value).toEqual(errors)
    expect(h.api.select(PIN)).toBe(true)
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('retains and reselects a saved model omitted from discovery, including from router mode', async () => {
    const storage = memoryStorage()
    const original = harness({ storage })
    original.api.select(PIN)
    original.scope.stop()
    const h = harness({ storage, list: async () => ({ models: [], errors: [] }) })
    await h.api.refresh()
    expect(h.api.models.value).toEqual([])
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.select(PIN)).toBe(true)
    h.routingMode.value = 'squilla_router'
    expect(await h.api.selectWithRouting(PIN, async mode => {
      h.routingMode.value = mode
      return true
    })).toBe(true)
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.conflict.value).toBeNull()
    h.scope.stop()
  })

  it('keeps the last catalog and selected pin when discovery fails', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    h.list.mockRejectedValueOnce(new Error('Provider discovery failed'))
    await h.api.refresh()
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.error.value).toBe('Provider discovery failed')
    expect(h.api.select(PIN)).toBe(true)
    h.scope.stop()
  })

  it('does not change route or silently discard the model when routing changes', async () => {
    const h = harness()
    await h.api.refresh()
    expect(h.api.select(PIN)).toBe(true)
    h.routingMode.value = 'llm_ensemble'
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.conflict.value).toBe('routing')
    expect(h.api.select(PIN)).toBe(false)
    expect(h.api.select(null)).toBe(true)
    expect(h.routingMode.value).toBe('llm_ensemble')
    expect(h.api.conflict.value).toBeNull()
    h.scope.stop()
  })

  it('clears a pin only after an explicit successful router or ensemble choice', async () => {
    const h = harness()
    await h.api.refresh()
    for (const mode of ['squilla_router', 'llm_ensemble'] as const) {
      h.routingMode.value = 'off'
      h.api.select(PIN)
      const setMode = vi.fn(async (next: ModelRoutingMode) => { h.routingMode.value = next; return true })
      expect(await h.api.selectRoutingMode(mode, setMode)).toBe(true)
      expect(setMode).toHaveBeenCalledWith(mode)
      expect(h.api.selection.value).toBeNull()
      expect(h.routingMode.value).toBe(mode)
    }
    h.routingMode.value = 'off'
    h.api.select(PIN)
    await h.api.selectRoutingMode('off', async () => true)
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('selects a model from router mode only after switching the draft to direct', async () => {
    const h = harness()
    await h.api.refresh()
    h.routingMode.value = 'squilla_router'
    const setMode = vi.fn(async (mode: ModelRoutingMode) => {
      expect(h.api.selection.value).toBeNull()
      h.routingMode.value = mode
      return true
    })
    expect(await h.api.selectWithRouting(PIN, setMode)).toBe(true)
    expect(setMode).toHaveBeenCalledExactlyOnceWith('off')
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.conflict.value).toBeNull()
    expect(await h.api.selectWithRouting(null, async () => true)).toBe(true)
    expect(h.api.selection.value).toBeNull()
    h.scope.stop()
  })

  it('does not switch routing for invalid, busy, or unavailable model choices', async () => {
    const h = harness()
    await h.api.refresh()
    const setMode = vi.fn(async () => true)
    expect(await h.api.selectWithRouting({ ...PIN, provider: ' invalid ' }, setMode)).toBe(false)
    h.busy.value = true
    expect(await h.api.selectWithRouting(PIN, setMode)).toBe(false)
    h.busy.value = false
    h.capable.value = false
    expect(await h.api.selectWithRouting(PIN, setMode)).toBe(false)
    expect(setMode).not.toHaveBeenCalled()
    h.scope.stop()
  })

  it('preserves the old choice when switching direct fails', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    expect(await h.api.selectWithRouting(null, async () => false)).toBe(false)
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('lets Gateway default clear an unavailable draft pin without writing a route or changing global state', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    h.capable.value = false
    h.routingMode.value = 'squilla_router'
    await nextTick()
    const setMode = vi.fn(async () => false)
    expect(h.api.conflict.value).toBe('unavailable')
    expect(await h.api.selectWithRouting(null, setMode)).toBe(true)
    expect(h.api.selection.value).toBeNull()
    expect(h.api.conflict.value).toBeNull()
    expect(h.routingMode.value).toBe('squilla_router')
    expect(setMode).not.toHaveBeenCalled()
    h.scope.stop()
  })

  it.each(['navigate', 'reconnect', 'send', 'newer-mode', 'newer-model', 'clear-model', 'dispose'] as const)(
    'does not apply a delayed model choice after %s', async cause => {
      const h = harness()
      await h.api.refresh()
      let resolve!: (updated: boolean) => void
      const pending = h.api.selectWithRouting(PIN, () => new Promise(r => { resolve = r }))
      if (cause === 'navigate') {
        h.sessionKey.value = 'agent:main:webchat:two'
        h.sessionKey.value = 'agent:main:webchat:one'
      } else if (cause === 'reconnect') h.connectionEpoch.value += 1
      else if (cause === 'send') h.busy.value = true
      else if (cause === 'newer-mode') await h.api.selectRoutingMode('squilla_router', async () => true)
      else if (cause === 'newer-model') h.api.select({ ...PIN, model: 'other-model' })
      else if (cause === 'clear-model') h.api.select(null)
      else h.scope.stop()
      resolve(true)
      expect(await pending).toBe(false)
      expect(h.api.selection.value).toEqual(cause === 'newer-model'
        ? { ...PIN, model: 'other-model' } : null)
      h.scope.stop()
    },
  )

  it('does not clear a pin when a route choice from an old connection resolves', async () => {
    const h = harness()
    h.api.select(PIN)
    let resolve!: (updated: boolean) => void
    const pending = h.api.selectRoutingMode('squilla_router', () => new Promise(r => { resolve = r }))
    h.connectionEpoch.value += 1
    h.routingMode.value = 'squilla_router'
    resolve(true)
    await pending
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('preserves the pin on a rejected or failed strategy change and during unacknowledged sending', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    expect(await h.api.selectRoutingMode('squilla_router', async () => false)).toBe(false)
    expect(h.api.selection.value).toEqual(PIN)
    await expect(h.api.selectRoutingMode('llm_ensemble', async () => { throw new Error('offline') })).rejects.toThrow('offline')
    expect(h.api.selection.value).toEqual(PIN)
    h.busy.value = true
    const setMode = vi.fn(async () => true)
    expect(await h.api.selectRoutingMode('squilla_router', setMode)).toBe(false)
    expect(setMode).not.toHaveBeenCalled()
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('does not clear a new draft pin when an old strategy operation resolves late', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    let resolve!: (updated: boolean) => void
    const pending = h.api.selectRoutingMode('squilla_router', () => new Promise(r => { resolve = r }))
    h.sessionKey.value = 'agent:main:webchat:two'
    await nextTick()
    h.api.select(PIN)
    h.routingMode.value = 'squilla_router'
    resolve(true)
    await pending
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('keeps existing-session next-turn routing available while a response streams', async () => {
    const h = harness()
    h.draft.value = false
    h.busy.value = true
    const setMode = vi.fn(async () => true)
    expect(h.api.disabledReason.value).toBe('unavailable')
    expect(await h.api.selectRoutingMode('squilla_router', setMode)).toBe(true)
    expect(setMode).toHaveBeenCalledOnce()
    h.scope.stop()
  })

  it('retains the draft pin through disconnection and prevents changing it during acceptance', async () => {
    const h = harness()
    await h.api.refresh()
    h.api.select(PIN)
    h.busy.value = true
    expect(h.api.select(null)).toBe(false)
    h.capable.value = false
    await nextTick()
    expect(h.api.selection.value).toEqual(PIN)
    expect(h.api.conflict.value).toBe('unavailable')
    expect(h.api.disabledReason.value).toBe('busy')
    h.busy.value = false
    expect(h.api.select(null)).toBe(true)
    expect(h.api.conflict.value).toBeNull()
    h.scope.stop()
  })

  it('recovers the same draft across remount but clears only after acceptance or a new draft', async () => {
    const storage = memoryStorage()
    const first = harness({ storage })
    await first.api.refresh()
    first.api.select(PIN)
    first.scope.stop()
    const second = harness({ storage })
    expect(second.api.selection.value).toEqual(PIN)
    second.draft.value = false
    second.sessionKey.value = 'agent:main:webchat:existing'
    await nextTick()
    expect(second.api.selection.value).toBeNull()
    second.sessionKey.value = 'agent:main:webchat:one'
    second.draft.value = true
    await nextTick()
    expect(second.api.selection.value).toEqual(PIN)
    second.draft.value = false
    await nextTick()
    second.draft.value = true
    expect(second.api.selection.value).toBeNull()
    await second.api.refresh()
    second.api.select(PIN)
    second.sessionKey.value = 'agent:main:webchat:new'
    await nextTick()
    expect(second.api.selection.value).toBeNull()
    second.scope.stop()
  })

  it('ignores a late catalog response after gateway reconnection and deduplicates open/retry calls', async () => {
    let resolveOld!: (result: ModelCatalogResult) => void
    let call = 0
    const h = harness({ list: () => ++call === 1
      ? new Promise(resolve => { resolveOld = resolve })
      : Promise.resolve({ models: [MODEL], errors: [] }) })
    const old = h.api.refresh()
    expect(h.api.refresh()).toBe(old)
    h.connectionEpoch.value = 2
    await nextTick()
    resolveOld({ models: [], errors: [] })
    await old
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.list).toHaveBeenCalledTimes(2)
    h.scope.stop()
  })

  it('exposes a load error and permits a successful explicit retry', async () => {
    let first = true
    const h = harness({ list: async () => {
      if (first) { first = false; throw new Error('catalog unavailable') }
      return { models: [MODEL], errors: [] }
    } })
    await h.api.refresh()
    expect(h.api.error.value).toBe('catalog unavailable')
    await h.api.refresh()
    expect(h.api.error.value).toBeNull()
    expect(h.api.models.value).toEqual([MODEL])
    h.scope.stop()
  })
})
