import { computed, reactive, ref, type InjectionKey } from 'vue'
import type { ModelCapacity, ModelCapacityTarget, ProviderConfiguration } from '@/modules/providerConfiguration'

export type CapacityField = 'contextWindow' | 'maxOutputTokens'
export interface CapacityValues { contextWindow: string; maxOutputTokens: string }
interface Draft { target: ModelCapacityTarget; values: CapacityValues; baseline: CapacityValues; scope: string }
export const capacityKey = (target: ModelCapacityTarget) => JSON.stringify([target.provider.trim().toLowerCase(), target.model.trim()])
export function parseCapacity(value: string): number | null {
  if (!value.trim()) return null
  if (!/^\d+$/.test(value.trim())) throw new Error('positiveInteger')
  const number = Number(value)
  if (!Number.isSafeInteger(number) || number < 1) throw new Error('positiveInteger')
  return number
}
const fields: CapacityField[] = ['contextWindow', 'maxOutputTokens']
const wire = { contextWindow: 'context_window', maxOutputTokens: 'max_output_tokens' } as const
const same = (a: CapacityValues, b: CapacityValues) => fields.every(field => a[field] === b[field])

export function useModelCapacityForm(provider: ProviderConfiguration) {
  const rows = reactive(new Map<string, ModelCapacity>())
  const drafts = reactive(new Map<string, Draft>())
  const pending = reactive(new Set<string>())
  const failed = reactive(new Set<string>())
  const supported = computed(() => provider.capacitySupported === true && Boolean(provider.resolveCapacity))
  const generation = ref(0)
  const queued = new Map<string, ModelCapacityTarget>()
  let scheduled = false

  function ensure(target: ModelCapacityTarget) {
    const normalized = { provider: target.provider.trim().toLowerCase(), model: target.model.trim() }
    const key = capacityKey(normalized)
    if (!supported.value || !normalized.provider || !normalized.model || rows.has(key) || pending.has(key)) return
    pending.add(key)
    failed.delete(key)
    queued.set(key, normalized)
    if (scheduled) return
    scheduled = true
    queueMicrotask(async () => {
      scheduled = false
      const batch = [...queued.entries()]
      queued.clear()
      const epoch = generation.value
      try {
        for (let offset = 0; offset < batch.length; offset += 128) {
          const result = await provider.resolveCapacity!(batch.slice(offset, offset + 128).map(([, item]) => item))
          if (epoch !== generation.value) return
          const requested = new Set(batch.slice(offset, offset + 128).map(([key]) => key))
          for (const row of result.models) {
            const key = capacityKey(row)
            if (requested.delete(key)) rows.set(key, row)
          }
          for (const key of requested) failed.add(key)
        }
      } catch {
        if (epoch === generation.value) for (const [key] of batch) failed.add(key)
      } finally {
        if (epoch === generation.value) for (const [key] of batch) pending.delete(key)
      }
    })
  }
  function values(target: ModelCapacityTarget): CapacityValues {
    const key = capacityKey(target)
    const draft = drafts.get(key)
    if (draft) return { ...draft.values }
    const row = rows.get(key)
    return {
      contextWindow: row?.contextWindow.override == null ? '' : String(row.contextWindow.override),
      maxOutputTokens: row?.maxOutputTokens.override == null ? '' : String(row.maxOutputTokens.override),
    }
  }
  function update(target: ModelCapacityTarget, next: CapacityValues, scope: string) {
    const key = capacityKey(target)
    const row = rows.get(key)
    if (!row) return
    const safe = { ...next }
    for (const field of fields) if (!row[field].editable) safe[field] = values(target)[field]
    const baseline = drafts.get(key)?.baseline || values(target)
    if (same(safe, baseline)) drafts.delete(key)
    else drafts.set(key, { target: { provider: target.provider.trim().toLowerCase(), model: target.model.trim() }, values: safe, baseline, scope })
  }
  const dirty = (scope: string) => [...drafts.values()].some(draft => draft.scope === scope)
  function valid(scope: string) {
    try {
      for (const draft of drafts.values()) if (draft.scope === scope) fields.forEach(field => parseCapacity(draft.values[field]))
      return true
    } catch { return false }
  }
  function discard(scope: string) {
    for (const [key, draft] of drafts) if (draft.scope === scope) drafts.delete(key)
  }
  function captureProviderDrafts(providerId: string): () => void {
    const provider = providerId.trim().toLowerCase()
    const snapshot = new Map([...drafts].filter(([, draft]) => draft.target.provider === provider))
    return () => {
      for (const [key, draft] of drafts) if (draft.target.provider === provider) drafts.delete(key)
      for (const [key, draft] of snapshot) drafts.set(key, draft)
    }
  }
  function patch(scope: string): Record<string, unknown> | null {
    const models: Record<string, Record<string, Record<string, number | null>>> = Object.create(null)
    for (const draft of drafts.values()) {
      if (draft.scope !== scope) continue
      const changes: Record<string, number | null> = {}
      for (const field of fields) if (draft.values[field] !== draft.baseline[field]) changes[wire[field]] = parseCapacity(draft.values[field])
      ;(models[draft.target.provider] ||= Object.create(null))[draft.target.model] = changes
    }
    return Object.keys(models).length ? { models } : null
  }
  async function save(scope: string, persist: (patch: Record<string, unknown>) => Promise<unknown>) {
    const changes = patch(scope)
    if (!changes) return false
    const snapshot = [...drafts.entries()].filter(([, draft]) => draft.scope === scope)
    await persist(changes)
    for (const [key, draft] of snapshot) {
      if (drafts.get(key) === draft) drafts.delete(key)
      rows.delete(key)
      ensure(draft.target)
    }
    return true
  }
  function invalidate() {
    generation.value++
    rows.clear(); pending.clear(); failed.clear(); queued.clear()
  }
  return { rows, drafts, pending, failed, supported, generation, ensure, values, update, dirty, valid, discard, captureProviderDrafts, patch, save, invalidate }
}
export type ModelCapacityForm = ReturnType<typeof useModelCapacityForm>
export const MODEL_CAPACITY_KEY: InjectionKey<ModelCapacityForm> = Symbol('ModelCapacityForm')
