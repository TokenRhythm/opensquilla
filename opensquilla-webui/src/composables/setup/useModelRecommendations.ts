import { computed, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { setupSaveFailedMessage } from '@/utils/setupErrorPresentation'
import { ProviderConfigurationError, type ModelRecommendation, type ProviderConfiguration, type RecommendationKind, type RecommendationRow } from '@/modules/providerConfiguration'

export interface RecommendationPreview {
  plan: ModelRecommendation
  before: RecommendationRow[]
  hasDraft: boolean
  error: string
}

interface Context {
  active: Readonly<Ref<boolean>>
  relevantProviders: Readonly<Ref<string[]>>
  draft(kind: RecommendationKind): { rows: RecommendationRow[]; dirty: boolean }
  applied(kind: RecommendationKind): Promise<void>
  uncertain(error: unknown): Promise<void>
}

const copyRows = (rows: RecommendationRow[]) => rows.map(row => ({ ...row }))
const target = (plan: ModelRecommendation) => ({ providerId: plan.providerId, kind: plan.kind, revision: plan.revision })
const identity = (plan: ModelRecommendation) => `${plan.providerId}:${plan.kind}:${plan.revision}`

/** Reads only recommendation state. Background refresh never reloads editable forms. */
export function useModelRecommendations(api: ProviderConfiguration, context: Context) {
  const plans = ref<ModelRecommendation[]>([])
  const preview = ref<RecommendationPreview | null>(null)
  const saving = ref(false)
  const supported = computed(() => api.recommendationsSupported === true)
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const notified = new Set<string>()
  let timer: ReturnType<typeof setInterval> | undefined
  let mounted = false
  let request: Promise<void> | null = null
  let controller: AbortController | null = null
  let resolvePreview: ((saved: boolean) => void) | null = null
  let opening = false

  function find(provider: string, kind: RecommendationKind) {
    return plans.value.find(plan => plan.providerId === provider && plan.kind === kind)
  }

  function notify() {
    if (!mounted || !context.active.value || document.visibilityState === 'hidden') return
    for (const plan of plans.value) {
      if (!context.relevantProviders.value.includes(plan.providerId) || !plan.hasUpdate || plan.notified || notified.has(identity(plan))) continue
      notified.add(identity(plan))
      pushToast(t('setup.modelStrategy.recommendations.available', {
        provider: plan.providerId === 'tokenrhythm' ? 'TokenRhythm' : 'OpenRouter',
        kind: t(`setup.modelStrategy.recommendations.${plan.kind}`),
      }), { tone: 'info' })
      // The Gateway persists this per instance; never mix instances in localStorage.
      void api.acknowledgeRecommendation?.(target(plan)).catch(() => { /* Retry on a later settings visit. */ })
    }
  }

  async function refresh(): Promise<void> {
    if (!supported.value || !api.recommendations) return
    if (request) return request
    const abort = new AbortController()
    controller = abort
    request = (async () => {
      try {
        const result = await api.recommendations!({ signal: abort.signal })
        if (abort.signal.aborted) return
        plans.value = result.plans
        notify()
      } finally {
        if (controller === abort) {
          controller = null
          request = null
        }
      }
    })()
    return request
  }

  function refreshAfterMutation() {
    // An older in-flight read may describe the state before the confirmed
    // write. Discard it instead of letting it restore an already-consumed notice.
    controller?.abort()
    request = null
    return refresh()
  }

  function tick() {
    if (context.active.value && document.visibilityState !== 'hidden') void refresh().catch(() => { /* Last valid recommendation remains available. */ })
  }

  function close() {
    if (saving.value) return
    preview.value = null
    resolvePreview?.(false)
    resolvePreview = null
  }

  async function open(provider: string, kind: RecommendationKind): Promise<boolean> {
    if (preview.value || saving.value || opening || !supported.value) return false
    opening = true
    try {
      if (!find(provider, kind)) await refresh()
      const plan = find(provider, kind)
      if (!plan) {
        pushToast(t('setup.modelStrategy.recommendations.unavailable'), { tone: 'warn' })
        return false
      }
      const draft = context.draft(kind)
      preview.value = {
        plan: { ...plan, rows: copyRows(plan.rows), currentRows: copyRows(plan.currentRows) },
        before: copyRows(draft.dirty ? draft.rows : plan.currentRows),
        hasDraft: draft.dirty,
        error: '',
      }
      return await new Promise<boolean>(resolve => { resolvePreview = resolve })
    } catch (error) {
      pushToast(setupSaveFailedMessage(error), { tone: 'danger' })
      return false
    } finally {
      opening = false
    }
  }

  async function apply() {
    const snapshot = preview.value
    if (!snapshot || saving.value || !api.applyRecommendation) return
    saving.value = true
    let saved = false
    try {
      // Freeze the revision reviewed by the user, even if polling found a newer one.
      await api.applyRecommendation(target(snapshot.plan))
      saved = true
      await context.applied(snapshot.plan.kind)
      pushToast(t('setup.modelStrategy.recommendations.saved'), { tone: 'ok' })
    } catch (error) {
      if (saved) pushToast(t('setup.modelStrategy.savedRefreshFailed'), { tone: 'warn' })
      else if (error instanceof ProviderConfigurationError && error.code === 'conflict') {
        await refreshAfterMutation().catch(() => {})
        const latest = find(snapshot.plan.providerId, snapshot.plan.kind)
        if (latest) {
          const draft = context.draft(snapshot.plan.kind)
          preview.value = { plan: { ...latest, rows: copyRows(latest.rows), currentRows: copyRows(latest.currentRows) }, before: copyRows(draft.dirty ? draft.rows : latest.currentRows), hasDraft: draft.dirty, error: t('setup.modelStrategy.recommendations.changed') }
        } else snapshot.error = t('setup.modelStrategy.recommendations.unavailable')
      } else {
        snapshot.error = setupSaveFailedMessage(error)
        await context.uncertain(error)
      }
    } finally {
      saving.value = false
      if (saved) {
        plans.value = plans.value.map(plan => (
          identity(plan) === identity(snapshot.plan)
            ? { ...plan, currentRows: copyRows(snapshot.plan.rows), hasUpdate: false, notified: true }
            : plan
        ))
        preview.value = null
        resolvePreview?.(true)
        resolvePreview = null
        await refreshAfterMutation().catch(() => {})
      }
    }
  }

  onMounted(() => {
    mounted = true
    tick()
    timer = setInterval(tick, 30_000)
    document.addEventListener('visibilitychange', tick)
  })
  watch([context.active, supported], () => { if (mounted) tick() })
  watch(context.relevantProviders, notify)
  onUnmounted(() => {
    mounted = false
    clearInterval(timer)
    controller?.abort()
    document.removeEventListener('visibilitychange', tick)
    resolvePreview?.(false)
    resolvePreview = null
  })

  return { plans, preview, saving, supported, find, refresh, open, close, apply }
}
