import { computed, getCurrentScope, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import type { ModelCatalog, ModelDescriptor, ProviderListError } from '@/modules/providerConfiguration'
import type { ModelRoutingMode } from '@/types/modelRouting'

export interface NewTaskModelSelection {
  model: string
  provider: string
}

export type NewTaskModelDisabledReason = 'routing' | 'busy' | 'unavailable' | null

// One recovery pointer follows the existing single recoverable new-task draft.
// Accepted sessions use their server model; this is never a global model default.
const STORAGE_KEY = 'opensquilla.chat.new-task-model'
type SelectionStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

interface SavedSelection {
  sessionKey: string
  selection: NewTaskModelSelection
}

export interface UseNewTaskModelSelectionOptions {
  catalog: ModelCatalog
  sessionKey: Readonly<Ref<string>>
  isDraft: () => boolean
  capable: Readonly<Ref<boolean>>
  /** Shares the configured catalog with durable-session model selection. */
  catalogAvailable?: Readonly<Ref<boolean>>
  routingMode: Readonly<Ref<ModelRoutingMode>>
  busy: Readonly<Ref<boolean>>
  connectionEpoch?: Readonly<Ref<number>>
  storage?: SelectionStorage | null
}

function validSelection(value: unknown): value is NewTaskModelSelection {
  if (!value || typeof value !== 'object') return false
  const entry = value as Partial<NewTaskModelSelection>
  return [entry.model, entry.provider].every(value => (
    typeof value === 'string' && value.length > 0 && value.length <= 512
    && value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value)
  ))
}

function browserStorage(): SelectionStorage | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

/** Owns one draft model pin; existing routing and send owners keep their protocols. */
export function useNewTaskModelSelection(options: UseNewTaskModelSelectionOptions) {
  const storage = options.storage === undefined ? browserStorage() : options.storage
  const saved = shallowRef<SavedSelection | null>(null)
  try {
    const raw: unknown = JSON.parse(storage?.getItem(STORAGE_KEY) || 'null')
    if (raw && typeof raw === 'object') {
      const record = raw as Partial<SavedSelection>
      if (typeof record.sessionKey === 'string' && validSelection(record.selection)) {
        saved.value = { sessionKey: record.sessionKey, selection: { ...record.selection } }
      }
    }
  } catch { /* A corrupt or unavailable recovery record cannot block the composer. */ }

  const models = shallowRef<readonly ModelDescriptor[]>([])
  const providerErrors = shallowRef<readonly ProviderListError[]>([])
  const error = ref<string | null>(null)
  const loading = ref(false)
  const available = computed(() => options.isDraft() && options.capable.value)
  const catalogAvailable = computed(() => options.catalogAvailable?.value ?? available.value)
  const selection = computed(() => (
    options.isDraft() && saved.value?.sessionKey === options.sessionKey.value
      ? saved.value.selection : null
  ))
  const disabledReason = computed<NewTaskModelDisabledReason>(() => (
    options.isDraft() && options.busy.value ? 'busy'
      : !available.value ? 'unavailable'
        : options.routingMode.value !== 'off' ? 'routing' : null
  ))
  const conflict = computed<'routing' | 'unavailable' | null>(() => (
    !selection.value ? null
      : !available.value ? 'unavailable'
        : options.routingMode.value !== 'off' ? 'routing' : null
  ))
  const initialModel = computed(() => selection.value?.model ?? null)
  const initialProvider = computed(() => selection.value?.provider ?? null)
  let generation = 0
  let controller: AbortController | null = null
  let inFlight: Promise<void> | null = null
  let selectionOperation = 0

  function persist(next: SavedSelection | null) {
    saved.value = next
    try {
      if (next) storage?.setItem(STORAGE_KEY, JSON.stringify(next))
      else storage?.removeItem(STORAGE_KEY)
    } catch { /* Keep the in-memory draft usable when browser storage is restricted. */ }
  }

  function select(next: NewTaskModelSelection | null): boolean {
    if (!options.isDraft() || options.busy.value) return false
    // Gateway default remains an explicit escape from a model/routing conflict.
    if (next === null) { selectionOperation += 1; persist(null); return true }
    if (disabledReason.value || !validSelection(next)) return false
    // Discovery may omit a saved or manually configured model. Admission is
    // authoritative; catalog membership must not discard the user's pin.
    selectionOperation += 1
    persist({ sessionKey: options.sessionKey.value, selection: { ...next } })
    return true
  }

  async function selectRoutingMode(
    mode: ModelRoutingMode,
    setMode: (mode: ModelRoutingMode) => Promise<boolean>,
  ): Promise<boolean> {
    const key = options.sessionKey.value
    const epoch = options.connectionEpoch?.value
    const wasDraft = options.isDraft()
    // A first send waiting for admission must keep its frozen model and route.
    if (wasDraft && options.busy.value) return false
    const operation = ++selectionOperation
    const updated = await setMode(mode)
    // Only this explicit, successful strategy choice can release a model pin.
    // Passive config changes and stale writes must leave the draft untouched.
    if (updated && operation === selectionOperation
      && wasDraft && options.isDraft() && key === options.sessionKey.value
      && epoch === options.connectionEpoch?.value
      && mode !== 'off' && options.routingMode.value === mode) select(null)
    return updated
  }

  /** A model leaf is one explicit direct-mode choice, even from router mode. */
  async function selectWithRouting(
    next: NewTaskModelSelection | null,
    setMode: (mode: ModelRoutingMode) => Promise<boolean>,
  ): Promise<boolean> {
    if (!options.isDraft() || options.busy.value) return false
    // An unavailable gateway cannot acknowledge a strategy change. Dropping
    // an explicit local pin remains safe and restores gateway-default behavior.
    if (next === null && !available.value) return select(null)
    if (!available.value) return false
    if (next && !validSelection(next)) return false
    const key = options.sessionKey.value
    const epoch = options.connectionEpoch?.value
    const operation = ++selectionOperation
    const updated = await setMode('off')
    // Navigation, reconnection, acceptance, or a later explicit choice may
    // complete during routing. Never attach this selection to their draft.
    if (!updated || operation !== selectionOperation || key !== options.sessionKey.value
      || epoch !== options.connectionEpoch?.value || !available.value
      || options.busy.value || options.routingMode.value !== 'off') return false
    return select(next)
  }

  function restore(next: NewTaskModelSelection | null) {
    if (!options.isDraft() || !next || !validSelection(next) || selection.value) return
    persist({ sessionKey: options.sessionKey.value, selection: { ...next } })
  }

  function invalidateCatalog() {
    generation += 1
    controller?.abort()
    controller = null
    inFlight = null
    loading.value = false
    models.value = []
    providerErrors.value = []
    error.value = null
  }

  function refresh(): Promise<void> {
    if (!catalogAvailable.value) return Promise.resolve()
    if (inFlight) return inFlight
    const currentGeneration = generation
    const requestController = new AbortController()
    controller = requestController
    loading.value = true
    error.value = null
    const request = (async () => {
      try {
        const result = await options.catalog.list({ scope: 'configured', signal: requestController.signal })
        if (generation !== currentGeneration || !catalogAvailable.value) return
        models.value = result.models
        providerErrors.value = result.errors
      } catch (cause) {
        if (generation !== currentGeneration || requestController.signal.aborted) return
        error.value = cause instanceof Error ? cause.message : String(cause)
      } finally {
        if (generation === currentGeneration) {
          loading.value = false
          controller = null
          inFlight = null
        }
      }
    })()
    inFlight = request
    return request
  }

  watch([options.sessionKey, options.isDraft], () => { selectionOperation += 1 }, { flush: 'sync' })
  watch([options.sessionKey, options.isDraft], ([key, draft]) => {
    if (!saved.value || !key) return
    // Switching to an existing task leaves the one recoverable draft intact.
    // A new draft replaces it; acceptance retires the current draft pin.
    if ((draft && saved.value.sessionKey !== key) || (!draft && saved.value.sessionKey === key)) persist(null)
  }, { immediate: true })
  watch([catalogAvailable, () => options.connectionEpoch?.value], () => {
    invalidateCatalog()
    if (catalogAvailable.value) void refresh()
  }, { immediate: true })
  if (getCurrentScope()) onScopeDispose(() => {
    selectionOperation += 1
    invalidateCatalog()
  })

  return {
    available, selection, models, loading, error, providerErrors, disabledReason,
    conflict, initialModel, initialProvider, select, selectRoutingMode, selectWithRouting, refresh, restore,
  }
}
