import { effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { ModelCatalogResult, ModelDescriptor } from '@/modules/providerConfiguration'
import type { ModelRoutingMode } from '@/types/modelRouting'
import { readDraftRoutingMode, useNewTaskModelSelection, type NewTaskModelSelection } from './useNewTaskModelSelection'
import { useChatSessionRouting } from './useChatSessionRouting'
import { useLastManualChatSelection } from './useLastManualChatSelection'
import type { SessionRouting } from '@/modules/sessionRouting'
import { modelRoutingModeToGateway } from '@/types/modelRouting'

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
function harness(input: { catalogAvailable?: boolean; capable?: boolean; autoRefresh?: boolean; storage?: ReturnType<typeof memoryStorage>; initialSelection?: () => NewTaskModelSelection | null; list?: () => Promise<ModelCatalogResult> } = {}) {
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
    autoRefresh: input.autoRefresh,
    initialSelection: input.initialSelection,
  }))!
  return { api, list, sessionKey, draft, capable, catalogAvailable, routingMode, busy, connectionEpoch, storage, scope }
}

describe('new-task model selection', () => {
  it.each(['off', 'squilla_router', 'llm_ensemble'] as const)(
    'recovers a draft with explicit %s mode after another session changes the new-chat preference', async chosenMode => {
      const storage = memoryStorage()
      const preference = useLastManualChatSelection({ storage })
      await preference.remember({ mode: 'off', model: PIN })
      const key = 'agent:main:webchat:recover-mode'
      const mount = (connected: boolean) => {
        const scope = effectScope()
        const api = scope.run(() => {
          const sessionKey = ref(key)
          const available = ref(connected)
          const draft = ref(true)
          const routing = useChatSessionRouting({
            routing: { get: vi.fn(), set: vi.fn() } as unknown as SessionRouting,
            sessionKey, globalMode: ref('off'), available,
            lastManualMode: () => preference.readLatest()?.mode ?? null,
            recoveredDraftMode: () => readDraftRoutingMode(sessionKey.value, storage),
            globalImageInputAdmission: ref('unknown'), globalImageInputAdmissionReason: ref('unknown'),
            capabilitiesByMode: ref(null), isStreaming: ref(false), isDraft: () => draft.value,
            notifyError: vi.fn(),
          })
          const models = useNewTaskModelSelection({
            catalog: { list: vi.fn(async () => ({ models: [], errors: [] })) },
            sessionKey, isDraft: () => draft.value, capable: available,
            routingMode: routing.mode, busy: ref(false), storage, autoRefresh: false,
            initialSelection: () => {
              const latest = preference.readLatest()
              return latest?.mode === 'off' ? latest.model : null
            },
          })
          return { routing, models, available, draft, sessionKey }
        })!
        return { scope, ...api }
      }
      const first = mount(true)
      if (chosenMode === 'off') {
        expect(await first.models.selectWithRouting(null, first.routing.setMode)).toBe(true)
      } else {
        expect(await first.models.selectRoutingMode(chosenMode, first.routing.setMode)).toBe(true)
      }
      expect(first.models.selection.value).toBeNull()
      first.scope.stop()

      // A successful picker action in an existing session changes only the
      // preference. It does not replace the recoverable draft's record.
      const laterMode = chosenMode === 'off' ? 'llm_ensemble' : 'off'
      await preference.remember({ mode: laterMode, model: laterMode === 'off' ? PIN : null })
      const restored = mount(false)
      expect(restored.routing.mode.value).toBe(chosenMode)
      expect(restored.models.selection.value).toBeNull()
      expect(restored.routing.applyBootstrap({ mode: modelRoutingModeToGateway(laterMode), revision: 0 })).toBe(false)
      restored.available.value = true
      await nextTick()
      expect(restored.routing.initialRoutingMode.value).toBe(modelRoutingModeToGateway(chosenMode))
      expect(await restored.routing.restoreDraftModelMode()).toBe(false)
      expect(restored.routing.mode.value).toBe(chosenMode)
      expect(preference.readLatest()?.mode).toBe(laterMode)

      // A genuinely new draft still takes the latest explicit selection.
      restored.sessionKey.value = 'agent:main:webchat:fresh-mode'
      await nextTick()
      expect(restored.routing.mode.value).toBe(laterMode)
      expect(restored.models.selection.value).toEqual(laterMode === 'off' ? PIN : null)
      restored.draft.value = false
      await nextTick()
      expect(readDraftRoutingMode(restored.sessionKey.value, storage)).toBeNull()
      restored.scope.stop()
    },
  )

  it('reads recovery modes by draft key without guessing legacy null selections', () => {
    const storage = memoryStorage()
    const key = 'agent:main:webchat:recovery'
    storage.setItem('opensquilla.chat.new-task-model', JSON.stringify({ sessionKey: key, selection: null }))
    expect(readDraftRoutingMode(key, storage)).toBeNull()
    storage.setItem('opensquilla.chat.new-task-model', JSON.stringify({ sessionKey: key, selection: PIN }))
    expect(readDraftRoutingMode(key, storage)).toBe('off')
    expect(readDraftRoutingMode('agent:main:webchat:other', storage)).toBeNull()
    storage.setItem('opensquilla.chat.new-task-model', '{broken')
    expect(readDraftRoutingMode(key, storage)).toBeNull()
    expect(readDraftRoutingMode(key, null)).toBeNull()
    expect(readDraftRoutingMode(key, { ...storage, getItem: () => { throw new Error('unavailable') } })).toBeNull()
  })

  it('records switching back to direct mode while keeping the current model pin', async () => {
    const h = harness({ autoRefresh: false })
    h.api.select(PIN)
    expect(await h.api.selectRoutingMode('squilla_router', async mode => { h.routingMode.value = mode; return true })).toBe(true)
    expect(readDraftRoutingMode(h.sessionKey.value, h.storage)).toBe('squilla_router')
    expect(await h.api.selectRoutingMode('off', async mode => { h.routingMode.value = mode; return true })).toBe(true)
    expect(readDraftRoutingMode(h.sessionKey.value, h.storage)).toBe('off')
    expect(h.api.selection.value).toBeNull()
    h.api.select(PIN)
    expect(await h.api.selectRoutingMode('off', async () => true)).toBe(true)
    expect(h.api.selection.value).toEqual(PIN)
    expect(readDraftRoutingMode(h.sessionKey.value, h.storage)).toBe('off')
    h.scope.stop()
  })

  it('seeds each new draft with the latest manually selected model and provider', async () => {
    let remembered = { ...PIN }
    const h = harness({ initialSelection: () => remembered, autoRefresh: false })
    expect(h.api.initialModel.value).toBe(PIN.model)
    expect(h.api.initialProvider.value).toBe(PIN.provider)

    remembered = { model: 'another-model', provider: 'another-provider' }
    await nextTick()
    expect(h.api.selection.value).toEqual(PIN)
    h.sessionKey.value = 'agent:main:webchat:next'
    await nextTick()
    expect(h.api.selection.value).toEqual(remembered)
    remembered.model = 'later-mutation'
    expect(h.api.initialModel.value).toBe('another-model')
    expect(h.list).not.toHaveBeenCalled()
    h.scope.stop()
  })

  it('preserves a recovered draft pin over the latest manual selection', () => {
    const storage = memoryStorage()
    const first = harness({ storage, autoRefresh: false })
    first.api.select(PIN)
    first.scope.stop()
    const restored = harness({
      storage, autoRefresh: false,
      initialSelection: () => ({ model: 'other-model', provider: 'other-provider' }),
    })
    expect(restored.api.selection.value).toEqual(PIN)
    restored.scope.stop()
  })

  it('does not reapply the remembered model over a manual default choice on reconnect', async () => {
    const h = harness({ initialSelection: () => PIN, autoRefresh: false })
    expect(h.api.select(null)).toBe(true)
    h.connectionEpoch.value += 1
    await nextTick()
    expect(h.api.selection.value).toBeNull()
    expect(h.api.initialModel.value).toBeNull()
    h.scope.stop()
  })

  it('reads a new default only after an existing session becomes a fresh draft', async () => {
    const initialSelection = vi.fn(() => PIN)
    const h = harness({ initialSelection, autoRefresh: false })
    h.draft.value = false
    h.sessionKey.value = 'agent:main:webchat:history'
    await nextTick()
    initialSelection.mockClear()
    expect(h.api.selection.value).toBeNull()
    h.sessionKey.value = 'agent:main:webchat:next'
    expect(initialSelection).not.toHaveBeenCalled()
    h.draft.value = true
    await nextTick()
    expect(initialSelection).toHaveBeenCalledOnce()
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })

  it('recovers a manual Default choice instead of reapplying the remembered pin', () => {
    const storage = memoryStorage()
    const first = harness({ storage, initialSelection: () => PIN, autoRefresh: false })
    expect(first.api.select(null)).toBe(true)
    first.scope.stop()
    const restored = harness({ storage, initialSelection: () => PIN, autoRefresh: false })
    expect(restored.api.selection.value).toBeNull()
    expect(restored.api.initialModel.value).toBeNull()
    restored.scope.stop()
  })

  it('keeps configured-default behavior when no remembered model is available', () => {
    const h = harness({ initialSelection: () => null, autoRefresh: false })
    expect(h.api.selection.value).toBeNull()
    expect(h.api.initialModel.value).toBeNull()
    expect(h.api.initialProvider.value).toBeNull()
    h.scope.stop()
  })

  it('does not discover provider models until the explicit picker refresh', async () => {
    const h = harness({ catalogAvailable: true, autoRefresh: false })
    await nextTick()
    expect(h.list).not.toHaveBeenCalled()
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledTimes(1)
    h.scope.stop()
  })

  it('revalidates an explicitly opened picker after reconnect', async () => {
    const h = harness({ catalogAvailable: true, autoRefresh: false })
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledTimes(1)
    h.connectionEpoch.value = 2
    await vi.waitFor(() => expect(h.list).toHaveBeenCalledTimes(2))
    expect(h.list.mock.calls[1]).toEqual([expect.objectContaining({ cacheOnly: true })])
    h.scope.stop()
  })

  it('uses an older gateway response once without requiring snapshot metadata', async () => {
    const h = harness()
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledExactlyOnceWith({
      scope: 'configured', cacheOnly: true, signal: expect.any(AbortSignal),
    })
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.api.loading.value).toBe(false)
    h.scope.stop()
  })

  it('does not refresh a fresh empty snapshot', async () => {
    const h = harness({ list: async () => ({ models: [], errors: [],
      catalog: { cacheHit: true, stale: false, lastSyncedAt: '2026-01-01T00:00:00Z' } }) })
    await h.api.refresh()
    expect(h.list).toHaveBeenCalledTimes(1)
    expect(h.api.models.value).toEqual([])
    expect(h.api.loading.value).toBe(false)
    h.scope.stop()
  })

  it.each(['transport_transient', 'provider_overloaded', 'rate_limited', 'probe_timeout'])(
    'keeps the previous provider list on %s and replaces it on successful empty', async kind => {
      const h = harness()
      await h.api.refresh()
      h.api.select(PIN)
      h.list.mockResolvedValueOnce({ models: [], errors: [{ provider: MODEL.provider, kind, detail: '' }] })
      await h.api.refresh()
      expect(h.api.models.value).toEqual([MODEL])
      expect(h.api.providerErrors.value[0]?.kind).toBe(kind)
      h.list.mockResolvedValueOnce({ models: [], errors: [] })
      await h.api.refresh()
      expect(h.api.models.value).toEqual([])
      expect(h.api.selection.value).toEqual(PIN)
      h.scope.stop()
    },
  )

  it('renders a stale snapshot before refresh and preserves an explicit pin after rejection', async () => {
    let finish!: (value: ModelCatalogResult) => void
    let calls = 0
    const h = harness({ list: async () => {
      if (++calls === 1) return { models: [MODEL], errors: [],
        catalog: { cacheHit: true, stale: true, lastSyncedAt: null } }
      return new Promise(resolve => { finish = resolve })
    } })
    await vi.waitFor(() => expect(calls).toBe(2))
    expect(h.list.mock.calls[0]).toEqual([expect.objectContaining({ cacheOnly: true })])
    expect(h.api.models.value).toEqual([MODEL])
    expect(h.api.loading.value).toBe(true)
    h.api.select(PIN)
    finish({ models: [], errors: [{ provider: MODEL.provider, kind: 'auth_invalid', detail: '' }] })
    await h.api.refresh()
    expect(h.api.models.value).toEqual([])
    expect(h.api.selection.value).toEqual(PIN)
    h.scope.stop()
  })
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
