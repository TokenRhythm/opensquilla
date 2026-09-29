import { computed, inject, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { OBSERVABILITY_KEY, type TurnTraceSummary } from '@/modules/observability'
import { agentTraceEnabled } from '@/modules/agentTracePreference'
import type { TraceDetails, TraceProjection, TraceSpan } from '@/types/traceView'

function isAccessDenied(cause: unknown): boolean {
  if (!cause || typeof cause !== 'object') return false
  const error = cause as { code?: unknown; data?: { code?: unknown } }
  const code = error.code ?? error.data?.code
  return typeof code === 'string' && ['UNAUTHORIZED', 'FORBIDDEN', 'PERMISSION_DENIED'].includes(code.toUpperCase())
}

/** The mounted panel owns polling and cancellation for one exact conversation turn. */
export function useChatTrace(options: {
  sessionKey: () => string; turnId: () => string; running: () => boolean
}) {
  const injectedObservability = inject(OBSERVABILITY_KEY)
  if (!injectedObservability) throw new Error('Observability was not provided')
  const observability = injectedObservability
  const traces = ref<TurnTraceSummary[]>([])
  const selectedTraceId = ref<string | null>(null)
  const activeTraceId = ref<string | null>(null)
  const projection = ref<TraceProjection | null>(null)
  const details = ref<TraceDetails | null>(null)
  const loading = ref(false)
  const error = ref('')
  const rawEnabled = ref<boolean | null>(null)
  const selectedRow = ref<TraceSpan | null>(null)
  const payload = ref<{ input?: unknown; output?: unknown } | null>(null)
  const payloadLoading = ref(false)
  const payloadError = ref(false)
  const payloadUnavailable = ref(false)
  const payloadRestricted = ref(false)
  const hasTurn = computed(() => !!options.sessionKey() && !!options.turnId())
  let mounted = false
  let generation = 0
  let payloadGeneration = 0
  let finalRetries = 0
  const trackedLive = ref(false)
  const traceStillRunning = ref(false)
  const live = computed(() => agentTraceEnabled.value && (options.running() || (trackedLive.value && traceStillRunning.value)))
  let timer: ReturnType<typeof setTimeout> | undefined
  let request: AbortController | undefined
  let payloadRequest: AbortController | undefined

  function stopTimer() { if (timer) clearTimeout(timer); timer = undefined }
  function clearPayload() {
    payloadGeneration += 1
    payloadRequest?.abort()
    payloadRequest = undefined
    payload.value = null
    payloadLoading.value = false
    payloadError.value = false
    payloadUnavailable.value = false
    payloadRestricted.value = false
  }
  function clearTrace() {
    activeTraceId.value = null
    projection.value = null
    details.value = null
    selectedRow.value = null
    clearPayload()
  }

  function disableTrace() {
    generation += 1
    request?.abort()
    request = undefined
    stopTimer()
    traces.value = []
    selectedTraceId.value = null
    rawEnabled.value = false
    traceStillRunning.value = false
    loading.value = false
    error.value = ''
    clearTrace()
  }

  async function refresh() {
    if (!mounted || !hasTurn.value || !agentTraceEnabled.value) return
    stopTimer()
    request?.abort()
    const controller = new AbortController()
    request = controller
    const current = ++generation
    const sessionKey = options.sessionKey()
    const turnId = options.turnId()
    const valid = () => mounted && current === generation && sessionKey === options.sessionKey() && turnId === options.turnId()
    const callOptions = { signal: controller.signal }
    loading.value = true
    error.value = ''
    try {
      const response = await observability.turnTraces(sessionKey, turnId, callOptions)
      if (!valid()) return
      // The server is authoritative for raw trace visibility. A stale panel
      // must not retain historical data when capture is disabled remotely.
      if (response.raw_enabled !== true) {
        disableTrace()
        return
      }
      const incomingTraces = response.traces || []
      // Do not unmount an already visible live trace while the lookup catches
      // up with newly written records for this same session and turn.
      if (incomingTraces.length || !live.value || !activeTraceId.value) traces.value = incomingTraces
      rawEnabled.value = response.raw_enabled
      const chosen = traces.value.find(trace => trace.trace_id === selectedTraceId.value) || traces.value[traces.value.length - 1]
      if (!chosen) { traceStillRunning.value = false; clearTrace(); return }
      traceStillRunning.value = !chosen.complete && chosen.status === 'running'
      if (activeTraceId.value !== chosen.trace_id) { clearTrace(); activeTraceId.value = chosen.trace_id }
      const [projected, detailed] = await Promise.allSettled([
        observability.traceProjection(chosen.trace_id, callOptions),
        observability.traceDetails(chosen.trace_id, { ...callOptions, limit: 1000 }),
      ])
      if (!valid()) return
      if (projected.status === 'fulfilled') projection.value = projected.value || projection.value
      if (detailed.status === 'fulfilled') {
        const nextDetails = detailed.value
        if (nextDetails) {
          details.value = nextDetails
          if (nextDetails.reason === 'raw_diagnostics_disabled') {
            selectedRow.value = null
            clearPayload()
          } else if (selectedRow.value) {
            const updated = nextDetails.rows.find(row => row.id === selectedRow.value?.id)
            if (updated) selectRow(updated)
          }
        }
      } else if (isAccessDenied(detailed.reason)) {
        details.value = { traceId: chosen.trace_id, available: false, reason: 'access_denied', rows: [], count: 0, total: 0 }
        selectedRow.value = null
        clearPayload()
      }
      const failure = projected.status === 'rejected'
        ? projected.reason
        : detailed.status === 'rejected' && !isAccessDenied(detailed.reason) ? detailed.reason : null
      if (failure) error.value = failure instanceof Error ? failure.message : String(failure)
      if (chosen.complete || projection.value?.complete) { finalRetries = 0; traceStillRunning.value = false }
    } catch (cause) {
      if (valid() && !controller.signal.aborted) error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      if (valid()) {
        loading.value = false
        const retryFinal = finalRetries > 0
        if (retryFinal) finalRetries -= 1
        if (live.value || retryFinal) timer = setTimeout(() => { void refresh() }, 1500)
      }
    }
  }

  function resetBinding() {
    generation += 1
    request?.abort()
    stopTimer()
    traces.value = []
    selectedTraceId.value = null
    rawEnabled.value = null
    error.value = ''
    loading.value = false
    finalRetries = 0
    trackedLive.value = options.running()
    traceStillRunning.value = false
    clearTrace()
    void refresh()
  }

  function selectTrace(traceId: string) {
    selectedTraceId.value = traceId
    clearTrace()
    void refresh()
  }

  function selectRow(row: TraceSpan) {
    const unchanged = selectedRow.value?.id === row.id && selectedRow.value.seq === row.seq
    selectedRow.value = row
    if (unchanged) return
    clearPayload()
  }

  async function loadPayload() {
    const row = selectedRow.value
    const traceId = activeTraceId.value
    if (!mounted || !row || !traceId || row.seq == null) return
    clearPayload()
    const current = payloadGeneration
    const controller = new AbortController()
    payloadRequest = controller
    payloadLoading.value = true
    const valid = () => mounted && payloadGeneration === current && activeTraceId.value === traceId
    const callOptions = { signal: controller.signal }
    const inputSeq = row.inputSeq ?? (row.input != null && row.output == null ? row.seq : undefined)
    const outputSeq = inputSeq === row.seq ? undefined : row.seq
    try {
      const [input, output] = await Promise.all([
        inputSeq == null ? undefined : observability.tracePayload(traceId, inputSeq, callOptions),
        outputSeq == null ? undefined : observability.tracePayload(traceId, outputSeq, callOptions),
      ])
      if (!valid()) return
      payloadUnavailable.value = input?.available === false || output?.available === false
      payload.value = { ...(input?.available ? { input: input.payload } : {}), ...(output?.available ? { output: output.payload } : {}) }
    } catch (cause) {
      if (valid() && !controller.signal.aborted) {
        if (isAccessDenied(cause)) payloadRestricted.value = true
        else payloadError.value = true
      }
    } finally {
      if (valid()) payloadLoading.value = false
    }
  }

  watch([options.sessionKey, options.turnId], resetBinding)
  watch(agentTraceEnabled, enabled => {
    if (!enabled) disableTrace()
    else rawEnabled.value = null
  }, { flush: 'sync' })
  watch(options.running, (running, previous) => {
    if (running) trackedLive.value = true
    if (previous && !running) finalRetries = 3
    void refresh()
  })
  onMounted(() => { mounted = true; resetBinding() })
  onBeforeUnmount(() => {
    mounted = false
    generation += 1
    stopTimer()
    request?.abort()
    clearPayload()
  })

  return { traces, activeTraceId, projection, details, live, loading, error, rawEnabled, hasTurn, refresh, selectTrace, selectedRow, selectRow, payload, payloadLoading, payloadError, payloadUnavailable, payloadRestricted, loadPayload }
}
