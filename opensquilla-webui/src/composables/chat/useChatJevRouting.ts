import { computed, getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import { createMutationClientRequestId } from '@/utils/mutationRequestIdentity'
import type { JevConfiguration, JevRouting, JevSnapshot, JevSessionNavigation } from '@/modules/jevRouting'

export function useChatJevRouting(options: {
  routing: JevRouting | null
  sessionKey: Readonly<Ref<string>>
  available: Readonly<Ref<boolean>>
  connectionEpoch?: Readonly<Ref<unknown>>
  draftText?: Readonly<Ref<string>>
  composing?: Readonly<Ref<boolean>>
  draftConsumed?: Readonly<Ref<number>>
  onOpenSession?: (navigation: JevSessionNavigation) => void
}) {
  const snapshot = ref<JevSnapshot | null>(null)
  const busy = ref(false)
  const error = ref(false)
  let acceptedHandoff: { id: string; source: string } | null = null
  const openedHandoffs = new Set<string>()
  let generation = 0
  let updateSequence = 0
  let disposed = false
  let draftGeneration = 0
  let draftTimer: ReturnType<typeof setTimeout> | undefined
  let draftRequest: AbortController | undefined
  let currentDraftId = ''
  let backendDraftActive = false
  let backendDraftKey = ''
  let invalidation: Promise<void> = Promise.resolve()
  const available = computed(() => !disposed && options.available.value
    && options.routing?.available() === true && !!options.sessionKey.value)

  function apply(value: JevSnapshot, key: string, owner: number) {
    if (!available.value || owner !== generation || key !== options.sessionKey.value
      || value.sessionKey !== key) return false
    const typing = !!options.draftText?.value.trim()
    if (typing && !value.blocked && value.recommendation?.source === 'input'
      && value.recommendation.action !== 'new_session'
      && value.draftId !== currentDraftId) return false
    snapshot.value = value
    const acceptance = acceptedHandoff
    if (acceptance?.source === key && !openedHandoffs.has(acceptance.id)) {
      const applied = [value.recommendation, ...value.decisions].find(item => item?.id === acceptance.id
        && item.sessionKey === key && item.action === 'new_session' && item.status === 'applied'
        && item.handoff?.sourceSessionKey === key && !!item.handoff.targetSessionKey)
      const target = applied?.handoff?.targetSessionKey
      if (target && target !== key) {
        openedHandoffs.add(acceptance.id)
        acceptedHandoff = null
        options.onOpenSession?.({ decisionId: acceptance.id, sourceSessionKey: key, targetSessionKey: target })
      }
    }
    return true
  }

  async function load() {
    if (!available.value || !options.routing) return
    const owner = generation
    const sequence = updateSequence
    const key = options.sessionKey.value
    try {
      const value = await options.routing.get(key)
      // An event or user response delivered while the read was in flight is newer.
      if (sequence === updateSequence) apply(value, key, owner)
    } catch {
      // Optional recommendations never block chat startup or sending.
    }
  }

  async function mutate(operation: (routing: JevRouting, key: string) => Promise<JevSnapshot>) {
    if (!available.value || !options.routing || busy.value) return false
    const owner = generation
    const key = options.sessionKey.value
    busy.value = true
    error.value = false
    updateSequence += 1
    try {
      const result = await operation(options.routing, key)
      if (owner === generation && key === options.sessionKey.value) updateSequence += 1
      return apply(result, key, owner)
    } catch {
      if (generation === owner && key === options.sessionKey.value) error.value = true
      return false
    } finally {
      if (generation === owner) busy.value = false
    }
  }

  const subscription = options.routing?.subscribe(value => {
    if (value.sessionKey !== options.sessionKey.value || !available.value) return
    updateSequence += 1
    apply(value, options.sessionKey.value, generation)
  })
  const stop = watch([
    options.sessionKey,
    options.available,
    () => options.connectionEpoch?.value,
  ], () => {
    generation += 1
    acceptedHandoff = null
    updateSequence += 1
    busy.value = false
    error.value = false
    snapshot.value = null
    void load()
  }, { immediate: true })


  function cancelDraft() {
    draftGeneration += 1
    if (draftTimer !== undefined) clearTimeout(draftTimer)
    draftTimer = undefined
    draftRequest?.abort()
    draftRequest = undefined
  }

  function invalidateDraft() {
    if (!backendDraftActive || !options.routing?.preview) return
    const key = backendDraftKey
    const draftId = createMutationClientRequestId('jev-draft')
    backendDraftActive = false
    // Wait for invalidation before another preview, so a slow cancellation
    // cannot erase the next draft's already completed assessment.
    invalidation = invalidation.then(async () => {
      try { await options.routing!.preview!(key, draftId, '') } catch {
        // Reconnect and send-time admission still fence their own generations.
      }
    })
  }

  const stopDraft = watch([
    () => options.draftText?.value || '',
    () => options.composing?.value === true,
    available,
    () => snapshot.value?.enabled === true,
    options.sessionKey,
    () => options.connectionEpoch?.value,
    () => options.draftConsumed?.value ?? 0,
  ], ([text, composing, , , , , consumed], previous) => {
    cancelDraft()
    currentDraftId = createMutationClientRequestId('jev-draft')
    const consumedClear = !text && previous !== undefined && consumed !== previous[6]
    if (!consumedClear) invalidateDraft()
    else backendDraftActive = false
    if (text && !snapshot.value?.blocked && snapshot.value?.recommendation?.source === 'input'
      && snapshot.value.recommendation.action !== 'new_session') {
      snapshot.value = { ...snapshot.value, recommendation: null }
    }
    if (typeof text !== 'string' || !text.trim() || text.length > 12000 || composing || !available.value
      || !snapshot.value?.enabled || !options.routing?.preview
      || options.routing.previewAvailable?.() === false) return
    const owner = generation
    const draftOwner = draftGeneration
    const key = options.sessionKey.value
    const draftId = currentDraftId
    draftTimer = setTimeout(async () => {
      draftTimer = undefined
      await invalidation
      if (owner !== generation || draftOwner !== draftGeneration || !available.value) return
      const controller = new AbortController()
      draftRequest = controller
      backendDraftActive = true
      backendDraftKey = key
      const sequence = updateSequence
      try {
        const value = await options.routing!.preview!(key, draftId, text, controller.signal)
        if (!controller.signal.aborted && draftOwner === draftGeneration && sequence === updateSequence) {
          apply(value, key, owner)
        }
      } catch {
        // Draft assessment is optional and never consumes or prevents sending input.
      } finally {
        if (draftRequest === controller) draftRequest = undefined
      }
    }, 650)
  }, { immediate: true, flush: 'post' })

  function dispose() {
    if (disposed) return
    disposed = true
    generation += 1
    stop()
    stopDraft()
    cancelDraft()
    invalidateDraft()
    subscription?.close()
  }
  if (getCurrentScope()) onScopeDispose(dispose)

  return {
    snapshot,
    busy,
    error,
    configure: (settings: JevConfiguration) => mutate((routing, key) => routing.configure(key, settings)),
    respond: async (decisionId: string, accepted: boolean) => {
      if (busy.value || !available.value) return false
      const decision = snapshot.value?.recommendation
      if (accepted && decision?.id === decisionId && decision.action === 'new_session') {
        acceptedHandoff = { id: decisionId, source: options.sessionKey.value }
      } else if (acceptedHandoff?.id === decisionId) acceptedHandoff = null
      return mutate((routing, key) => routing.respond(key, decisionId, accepted))
    },
    reload: load,
    dispose,
  }
}
