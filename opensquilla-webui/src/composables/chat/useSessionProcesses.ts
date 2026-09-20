import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { ConversationEventHub } from '@/modules/conversationEventHub'
import type { ConversationEvent } from '@/modules/conversationEvents'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import type { SessionProcess, SessionProcessIdentity, SessionProcessLog, SessionProcesses } from '@/modules/sessionProcesses'

export function useSessionProcesses(options: {
  sessionKey: Ref<string>
  gateway: Pick<GatewayAccess, 'isAvailable' | 'subscriptionEpoch'>
  processes: SessionProcesses
  events: ConversationEventHub<ConversationEvent>
}) {
  const processes = ref<SessionProcess[]>([])
  const loading = ref(false)
  const error = ref(false)
  const selectedId = ref<string | null>(null)
  const log = ref<SessionProcessLog | null>(null)
  const logLoading = ref(false)
  const logError = ref(false)
  const stopping = ref<string | null>(null)
  const stopError = ref(false)
  let generation = 0
  let request = 0
  let logRequest = 0
  let identity: SessionProcessIdentity | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposeEvents = () => {}
  const runningCount = computed(() => processes.value.filter(item => item.status === 'running').length)

  function current(scope: number, key: string) {
    return generation === scope && options.sessionKey.value === key && options.gateway.isAvailable
  }

  function matchesIdentity(result: SessionProcessIdentity) {
    return identity && result.sessionKey === identity.sessionKey
      && result.sessionId === identity.sessionId && result.sessionEpoch === identity.sessionEpoch
  }

  async function refresh() {
    const key = options.sessionKey.value
    if (!key || !options.gateway.isAvailable || !options.processes.available) return
    const scope = generation
    const revision = ++request
    clearTimeout(timer)
    loading.value = true
    try {
      const result = await options.processes.list(key)
      if (!current(scope, key) || revision !== request || result.sessionKey !== key) return
      if (identity && !matchesIdentity(result)) closeLog()
      identity = result
      processes.value = result.processes
      error.value = false
      if (selectedId.value && !result.processes.some(item => item.executionId === selectedId.value)) closeLog()
    } catch {
      if (current(scope, key) && revision === request) error.value = true
    } finally {
      if (current(scope, key) && revision === request) {
        loading.value = false
        if (runningCount.value) timer = setTimeout(() => { void refresh() }, 5000)
      }
    }
  }

  function closeLog() {
    ++logRequest
    selectedId.value = null
    log.value = null
    logLoading.value = false
    logError.value = false
  }

  async function inspect(executionId: string) {
    if (!options.gateway.isAvailable || !identity) return
    const key = options.sessionKey.value
    const scope = generation
    const revision = ++logRequest
    selectedId.value = executionId
    log.value = null
    logError.value = false
    logLoading.value = true
    try {
      const result = await options.processes.log(key, executionId)
      if (current(scope, key) && revision === logRequest && matchesIdentity(result)
        && result.executionId === executionId) log.value = result
    } catch {
      if (current(scope, key) && revision === logRequest) logError.value = true
    } finally {
      if (current(scope, key) && revision === logRequest) logLoading.value = false
    }
  }

  async function stop(executionId: string) {
    if (!options.gateway.isAvailable || !options.processes.canStop || stopping.value) return
    const key = options.sessionKey.value
    const scope = generation
    stopping.value = executionId
    stopError.value = false
    try {
      const result = await options.processes.stop(key, executionId)
      if (!current(scope, key) || !matchesIdentity(result) || result.process.executionId !== executionId) return
      await refresh()
      if (current(scope, key) && selectedId.value === executionId) await inspect(executionId)
    } catch {
      if (current(scope, key)) stopError.value = true
    } finally {
      if (current(scope, key)) stopping.value = null
    }
  }

  function invalidate() {
    ++generation
    ++request
    clearTimeout(timer)
    identity = null
    processes.value = []
    loading.value = false
    error.value = false
    stopping.value = null
    stopError.value = false
    closeLog()
  }

  watch([options.sessionKey, () => options.gateway.isAvailable, () => options.gateway.subscriptionEpoch,
    () => options.processes.available], () => {
    invalidate()
    disposeEvents()
    const key = options.sessionKey.value
    if (!key || !options.gateway.isAvailable || !options.processes.available) return
    const handle = options.events.open(key)
    handle.observe(message => {
      if (message.kind !== 'conversation' || message.event.kind !== 'known'
        || message.event.sessionKey !== key) return
      const event = message.event
      if (event.semanticKind === 'session-epoch-changed') {
        invalidate()
        void refresh()
        return
      }
      if (event.semanticKind === 'process-completed' && identity
        && (event.payload.sessionId !== identity.sessionId || event.payload.sessionEpoch !== identity.sessionEpoch)) return
      if (event.semanticKind === 'process-completed'
        || event.semanticKind === 'turn-completed'
        || (event.semanticKind === 'tool-result' && ['exec_command', 'background_process', 'process'].includes(event.payload.name ?? ''))) {
        void refresh()
      }
    })
    disposeEvents = () => handle.close()
    void refresh()
  }, { immediate: true, flush: 'sync' })

  onScopeDispose(() => {
    ++generation
    clearTimeout(timer)
    disposeEvents()
  })

  return { processes, runningCount, loading, error, selectedId, log, logLoading, logError,
    stopping, stopError, refresh, inspect, closeLog, stop }
}
