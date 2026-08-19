import { computed, ref, watch, type Ref } from 'vue'
import type {
  GatewayModelRoutingMode,
  ModelRoutingMode,
} from '@/types/modelRouting'
import {
  gatewayModelRoutingModeToUi,
  modelRoutingModeToGateway,
} from '@/types/modelRouting'
import type {
  SessionMessagesSubscribeResponse,
  SessionRoutingSnapshot,
} from '@/types/rpc'

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  on: (event: string, handler: (payload: unknown) => void) => () => void
  waitForConnection?: () => Promise<void>
}

type RoutingResponse = SessionRoutingSnapshot & Record<string, unknown>

export interface UseChatSessionRoutingOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  globalMode: Readonly<Ref<ModelRoutingMode>>
  available?: Readonly<Ref<boolean>>
  isStreaming: Readonly<Ref<boolean>>
  isDraft: () => boolean
  notifyError: (message: string) => void
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function sessionKeyFrom(value: unknown): string {
  const source = record(value)
  if (!source) return ''
  for (const key of ['key', 'sessionKey', 'session_key']) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function revisionFrom(value: unknown): number | null {
  const source = record(value)
  if (!source) return null
  for (const key of ['revision', 'routingRevision', 'routing_revision']) {
    const raw = source[key]
    if (typeof raw === 'boolean' || raw === null || raw === undefined || raw === '') continue
    const revision = Number(raw)
    if (Number.isInteger(revision) && revision >= 0) return revision
  }
  return null
}

function routingSnapshotFrom(value: unknown): Record<string, unknown> | null {
  const source = record(value)
  if (!source) return null
  for (const key of ['routing', 'modelRouting', 'model_routing', 'sessionRouting']) {
    const nested = record(source[key])
    if (nested) return nested
  }
  return source
}

function modeFrom(value: unknown): ModelRoutingMode | null {
  const source = routingSnapshotFrom(value)
  if (!source) return null
  return gatewayModelRoutingModeToUi(
    source.mode
    ?? source.routingMode
    ?? source.routing_mode
    ?? source.modelRoutingMode
    ?? source.model_routing_mode,
  )
}

/**
 * Keeps the durable route strategy for exactly one chat session.  The global
 * model-routing configuration remains the source for new-task defaults and
 * routing parameters; this composable only selects which strategy this task
 * uses on its next accepted turn.
 */
export function useChatSessionRouting(options: UseChatSessionRoutingOptions) {
  const mode = ref<ModelRoutingMode>(options.globalMode.value)
  const revision = ref(0)
  const busy = ref(false)
  const modeAppliesNextTurn = ref(false)
  // `mode` begins as a global-default placeholder. It must not participate in
  // revision conflict checks until this session has supplied an authoritative
  // routing snapshot: old sessions legitimately start at revision 0 too.
  const hasAuthoritativeSnapshot = ref(false)
  let generation = 0
  let mutationOwner: symbol | null = null
  const draftModeSelected = ref(false)

  const isAvailable = () => options.available?.value ?? true
  const controlBusy = computed(() => (
    busy.value || (options.isDraft() && options.isStreaming.value)
  ))
  const initialRoutingMode = computed<GatewayModelRoutingMode | null>(() => (
    isAvailable() && options.isDraft() && draftModeSelected.value
      ? modelRoutingModeToGateway(mode.value)
      : null
  ))

  function reset() {
    generation += 1
    mutationOwner = null
    draftModeSelected.value = false
    mode.value = options.globalMode.value
    revision.value = 0
    busy.value = false
    modeAppliesNextTurn.value = false
    hasAuthoritativeSnapshot.value = false
  }

  function applySnapshot(value: unknown, fallback?: {
    mode: ModelRoutingMode
    revision: number
  }): boolean {
    const source = routingSnapshotFrom(value)
    const incomingMode = modeFrom(source) ?? fallback?.mode
    const incomingRevision = revisionFrom(source) ?? fallback?.revision
    if (!incomingMode || incomingRevision === undefined || incomingRevision === null) return false
    if (hasAuthoritativeSnapshot.value && incomingRevision < revision.value) return false
    if (
      hasAuthoritativeSnapshot.value
      && incomingRevision === revision.value
      && incomingMode !== mode.value
    ) return false
    mode.value = incomingMode
    revision.value = incomingRevision
    hasAuthoritativeSnapshot.value = true
    return true
  }

  async function load(): Promise<boolean> {
    const key = options.sessionKey.value
    const requestGeneration = generation
    if (!isAvailable() || !key || options.isDraft()) return false
    try {
      await options.rpc.waitForConnection?.()
      const response = await options.rpc.call<RoutingResponse>('sessions.routing.get', { sessionKey: key })
      if (
        requestGeneration !== generation
        || key !== options.sessionKey.value
        || !isAvailable()
        || options.isDraft()
      ) return false
      return applySnapshot(response)
    } catch {
      // A read is best effort while a session is hydrating. The setter reports
      // actionable failures and refreshes the authoritative server state.
      return false
    }
  }

  async function setMode(nextMode: ModelRoutingMode): Promise<boolean> {
    if (!isAvailable() || busy.value) return false
    if (options.isDraft()) {
      if (options.isStreaming.value) return false
      mode.value = nextMode
      revision.value = 0
      draftModeSelected.value = true
      modeAppliesNextTurn.value = false
      return true
    }

    // A repeated click on the selected durable mode is a pure no-op. Avoid a
    // transient busy cycle so the open routing popover remains visually stable.
    if (hasAuthoritativeSnapshot.value && nextMode === mode.value) return true

    const key = options.sessionKey.value
    if (!key) return false
    const requestGeneration = generation
    const owner = Symbol('session-routing-mutation')
    mutationOwner = owner
    busy.value = true
    try {
      if (!hasAuthoritativeSnapshot.value) {
        // A same-looking global placeholder is not proof that an existing
        // session is already direct/router/ensemble. Keep the mutation lock
        // while reading so a second click or send cannot race the CAS write.
        await load()
        if (
          mutationOwner !== owner
          || requestGeneration !== generation
          || key !== options.sessionKey.value
          || options.isDraft()
        ) return false
      }
      if (nextMode === mode.value && hasAuthoritativeSnapshot.value) return true

      const expectedRevision = revision.value
      const deferred = options.isStreaming.value
      const response = await options.rpc.call<RoutingResponse>('sessions.routing.set', {
        sessionKey: key,
        mode: modelRoutingModeToGateway(nextMode),
        expectedRevision,
      })
      if (requestGeneration !== generation || key !== options.sessionKey.value) return false
      applySnapshot(response, {
        mode: nextMode,
        revision: expectedRevision + 1,
      })
      modeAppliesNextTurn.value = deferred
        && options.isStreaming.value
        && mode.value === nextMode
      return mode.value === nextMode
    } catch (error) {
      if (
        mutationOwner === owner
        && requestGeneration === generation
        && key === options.sessionKey.value
      ) {
        await load()
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
      return false
    } finally {
      if (mutationOwner === owner) {
        mutationOwner = null
        busy.value = false
      }
    }
  }

  function applyBootstrap(snapshot: SessionMessagesSubscribeResponse | unknown): boolean {
    // A draft selection is the value that will be atomically persisted with
    // its first turn. A late global/default bootstrap is not authoritative for
    // that user choice.
    if (options.isDraft() && draftModeSelected.value) return false
    return applySnapshot(snapshot)
  }

  function applyChangedEvent(payload: unknown) {
    const key = sessionKeyFrom(payload)
    if (key && key !== options.sessionKey.value) return
    applySnapshot(payload)
  }

  function subscribe(): () => void {
    return options.rpc.on('sessions.routing.changed', applyChangedEvent)
  }

  watch(options.sessionKey, () => {
    reset()
    void load()
  }, { flush: 'sync', immediate: true })
  watch(options.globalMode, nextMode => {
    // Drafts have no durable session setting yet. Their first send captures
    // the current global default unless the user chose one of the three modes.
    if (options.isDraft() && !busy.value && !draftModeSelected.value) mode.value = nextMode
  })
  if (options.available) {
    watch(options.available, available => {
      // Capability/connection loss must cancel an in-flight mutation without
      // erasing an explicit new-chat choice or a read-only bootstrap snapshot.
      // `available` gates active get/set calls, not snapshots already delivered
      // through the authorized session subscription.
      generation += 1
      mutationOwner = null
      busy.value = false
      modeAppliesNextTurn.value = false
      if (available) void load()
    }, { flush: 'sync' })
  }
  watch(options.isStreaming, streaming => {
    if (!streaming) modeAppliesNextTurn.value = false
  })
  watch(() => options.isDraft(), draft => {
    if (!draft) void load()
  })

  return {
    mode,
    revision,
    busy: controlBusy,
    modeAppliesNextTurn,
    hasAuthoritativeSnapshot,
    initialRoutingMode,
    applyBootstrap,
    load,
    reset,
    setMode,
    subscribe,
  }
}
