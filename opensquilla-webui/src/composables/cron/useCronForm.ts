import { computed, nextTick, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import { useProjectWorkspaces } from '@/composables/useProjectWorkspaces'
import type { CronJob, CronJobFormModel, CronPanelTemplate } from '@/types/cron'
import { buildDeliveryFromValues, normalizeDeliveryFields } from '@/utils/cron/delivery'
import { explainCron, nextRuns, parseCron } from '@/utils/cron/schedule'
import { canonicalSessionKey } from '@/utils/chat/sessionKeys'

interface UseCronFormOptions {
  afterSaved: () => void
}

export function useCronForm(options: UseCronFormOptions) {
  const rpc = useRpcStore()
  const route = useRoute()
  const { pushToast } = useToasts()
  const projectWorkspaces = useProjectWorkspaces()
  const t = i18n.global.t
  const panelOpen = ref(false)
  const editingJob = ref<CronJob | null>(null)
  const cronExplainHuman = ref(t('cronSkills.form.cronPreviewPlaceholder'))
  const cronExplainValid = ref(false)
  const cronExplainInvalid = ref(false)
  const cronExplainUpcoming = ref<Date[]>([])
  let previewTimer: ReturnType<typeof setTimeout> | null = null

  const form = reactive<CronJobFormModel>({
    templateId: '',
    name: '',
    type: 'cron',
    cron: '',
    every: '',
    at: '',
    tz: '',
    payloadKind: 'reminder',
    agentId: 'main',
    workspaceId: '',
    workspaceRequired: false,
    sessionTarget: 'isolated',
    targetSessionKey: '',
    message: '',
    wakeMode: 'now',
    deliveryMode: '',
    deliveryChannel: '',
    deliveryTo: '',
    deliveryAccount: '',
    deliveryWebhookUrl: '',
    deliveryWebhookToken: '',
    deliveryBestEffort: false,
    fdMode: '',
    fdChannel: '',
    fdTo: '',
    fdAccount: '',
    fdWebhookUrl: '',
    fdWebhookToken: '',
    enabled: true,
  })

  const jobModeHint = computed(() => {
    if (form.payloadKind === 'system_event') return t('cronSkills.form.jobModeHint.systemEvent')
    if (form.payloadKind === 'reminder') return t('cronSkills.form.jobModeHint.reminder')
    return t('cronSkills.form.jobModeHint.agentTurn')
  })

  const sessionTargetHint = computed(() => {
    if (form.payloadKind === 'system_event') return t('cronSkills.form.sessionTargetHint.systemEvent')
    if (form.payloadKind === 'reminder') return t('cronSkills.form.sessionTargetHint.reminder')
    if (form.sessionTarget === 'current') return t('cronSkills.form.sessionTargetHint.current')
    if (form.sessionTarget === 'isolated') return t('cronSkills.form.sessionTargetHint.isolated')
    if (form.sessionTarget === 'session') return t('cronSkills.form.sessionTargetHint.session')
    return t('cronSkills.form.sessionTargetHint.default')
  })

  const showTargetSessionRow = computed(() => form.payloadKind === 'agent_turn' && (form.sessionTarget === 'current' || form.sessionTarget === 'session'))
  const targetSessionLabel = computed(() => form.sessionTarget === 'current' ? t('cronSkills.form.targetSessionLabel.current') : t('cronSkills.form.targetSessionLabel.named'))
  const targetSessionHint = computed(() => form.sessionTarget === 'current'
    ? t('cronSkills.form.targetSessionHint.current')
    : t('cronSkills.form.targetSessionHint.named'))
  const messageLabel = computed(() => {
    if (form.payloadKind === 'system_event') return t('cronSkills.form.messageLabel.systemEvent')
    if (form.payloadKind === 'reminder') return t('cronSkills.form.messageLabel.reminder')
    return t('cronSkills.form.messageLabel.agentTurn')
  })

  function openPanel(job: CronJob | null, template?: CronPanelTemplate) {
    editingJob.value = job
    panelOpen.value = true
    const tpl = template || {}
    form.templateId = job ? (job.templateId || '') : (tpl.id || '')
    const payloadKind = job ? (job.payloadKind || 'agent_turn') : (tpl.payloadKind || 'reminder')
    const sessionTarget = job
      ? (job.sessionTarget || job.session_target || 'isolated')
      : (tpl.sessionTarget || (payloadKind === 'system_event' ? 'main' : 'isolated'))

    form.name = job ? (job.name || '') : (tpl.name || '')
    form.message = job ? (job.message || job.prompt || '') : (tpl.message || '')
    form.type = job ? (job.scheduleKind || job.schedule_kind || 'cron') : (tpl.scheduleKind || tpl.schedule_kind || 'cron')
    form.cron = job ? (job.expression || '') : (tpl.expression || '')
    form.enabled = job ? !!job.enabled : true
    form.agentId = job ? (job.agentId || 'main') : (tpl.agentId || 'main')
    form.workspaceId = job ? (job.workspaceId || '') : (tpl.workspaceId || '')
    form.workspaceRequired = tpl.requiresWorkspace === true ||
      ['weekly-report', 'project-risk', 'knowledge-review'].includes(form.templateId)
    form.payloadKind = payloadKind
    form.sessionTarget = sessionTarget
    form.targetSessionKey = job ? jobSessionKey(job) : (tpl.targetSessionKey || activeChatSessionKey() || '')
    form.every = form.type === 'every' ? (job ? (job.scheduleRaw || job.schedule_raw || '') : String(tpl.every_seconds || '')) : ''
    form.at = form.type === 'at' ? (job ? (job.scheduleRaw || job.schedule_raw || '') : (tpl.at || '')) : ''
    form.tz = job
      ? (job.tz || '')
      : (tpl.tz || Intl.DateTimeFormat().resolvedOptions().timeZone || '')
    form.wakeMode = job ? (job.wakeMode || job.wake_mode || 'now') : (tpl.wakeMode || 'now')

    Object.assign(form, normalizeDeliveryFields(job))
    void projectWorkspaces.loadWorkspaces().catch(() => undefined)
    onPayloadKindChange()
    renderCronExplain(form.cron)
    nextTick(() => document.getElementById('cp-name')?.focus())
  }

  function closePanel() {
    panelOpen.value = false
    editingJob.value = null
  }

  function onPayloadKindChange() {
    if (form.payloadKind === 'system_event') {
      form.sessionTarget = 'main'
    } else if (form.payloadKind === 'reminder') {
      form.sessionTarget = 'isolated'
    } else {
      const active = activeChatSessionKey()
      if (active && !form.targetSessionKey.trim()) form.targetSessionKey = active
      if (form.sessionTarget === 'current' && !form.targetSessionKey.trim()) form.targetSessionKey = active || jobSessionKey(editingJob.value)
    }
  }

  function onSessionTargetChange() {
    if (form.payloadKind !== 'agent_turn') return
    if (form.sessionTarget === 'current' && !form.targetSessionKey.trim()) {
      form.targetSessionKey = activeChatSessionKey() || jobSessionKey(editingJob.value)
    }
  }

  function applyPreset(cron: string) {
    form.cron = cron
    renderCronExplain(cron)
    nextTick(() => document.getElementById('cp-cron')?.focus())
  }

  function renderCronExplain(expr: string) {
    const trimmed = expr.trim()
    if (!trimmed) {
      cronExplainValid.value = false
      cronExplainInvalid.value = false
      cronExplainHuman.value = t('cronSkills.form.cronPreviewPlaceholder')
      cronExplainUpcoming.value = []
      return
    }
    const parsed = parseCron(trimmed)
    if (!parsed) {
      cronExplainValid.value = false
      cronExplainInvalid.value = true
      cronExplainHuman.value = t('cronSkills.form.cronParseError')
      cronExplainUpcoming.value = []
      return
    }
    cronExplainInvalid.value = false
    cronExplainValid.value = true
    cronExplainHuman.value = explainCron(trimmed) || t('cronSkills.form.cronCustomCadence')
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = setTimeout(() => {
      cronExplainUpcoming.value = nextRuns(parsed, 3)
    }, 60)
  }

  async function saveJob() {
    const name = form.name.trim()
    if (!name) {
      pushToast(t('cronSkills.form.toastNameRequired'), { tone: 'danger' })
      return
    }
    const payloadKind = form.payloadKind
    const sessionTarget = payloadKind === 'system_event'
      ? 'main'
      : payloadKind === 'reminder'
        ? 'isolated'
        : form.sessionTarget
    const payload: Record<string, unknown> = {
      name,
      enabled: form.enabled,
      payloadKind,
      agentId: form.agentId.trim() || 'main',
      sessionTarget,
      text: form.message.trim(),
      workspaceId: form.workspaceId.trim(),
      templateId: form.templateId,
    }
    if (payloadKind === 'agent_turn' && form.workspaceRequired && !form.workspaceId.trim()) {
      pushToast(t('cronSkills.form.toastWorkspaceRequired'), { tone: 'danger' })
      return
    }

    if (form.type === 'cron') {
      payload.schedule = { kind: 'cron', expr: form.cron.trim() }
    } else if (form.type === 'every') {
      const everySeconds = Number(form.every)
      if (!Number.isInteger(everySeconds) || everySeconds < 1) {
        pushToast(t('cronSkills.form.toastIntervalInvalid'), { tone: 'danger' })
        return
      }
      payload.schedule = { kind: 'every', every_seconds: everySeconds }
    } else if (form.type === 'at') {
      const at = form.at.trim()
      if (!at) {
        pushToast(t('cronSkills.form.toastIsoTimeRequired'), { tone: 'danger' })
        return
      }
      payload.schedule = { kind: 'at', at }
    }

    const tz = form.tz.trim()
    if (tz) {
      payload.tz = tz
      const sched = payload.schedule as Record<string, unknown>
      if (sched?.kind === 'cron') sched.tz = tz
    }
    if (form.wakeMode && form.wakeMode !== 'now') payload.wakeMode = form.wakeMode

    const deliveryResult = buildDeliveryFromValues({
      deliveryMode: form.deliveryMode,
      deliveryChannel: form.deliveryChannel,
      deliveryTo: form.deliveryTo,
      deliveryAccount: form.deliveryAccount,
      deliveryWebhookUrl: form.deliveryWebhookUrl,
      deliveryWebhookToken: form.deliveryWebhookToken,
      deliveryBestEffort: form.deliveryBestEffort,
      fdMode: form.fdMode,
      fdChannel: form.fdChannel,
      fdTo: form.fdTo,
      fdAccount: form.fdAccount,
      fdWebhookUrl: form.fdWebhookUrl,
      fdWebhookToken: form.fdWebhookToken,
    })
    if (deliveryResult.error) {
      pushToast(deliveryResult.error, { tone: 'danger' })
      return
    }
    if (deliveryResult.delivery !== null) payload.delivery = deliveryResult.delivery

    const targetSessionKey = form.targetSessionKey.trim()
    if (sessionTarget === 'current') {
      const boundSessionKey = targetSessionKey || activeChatSessionKey() || jobSessionKey(editingJob.value)
      if (!boundSessionKey) {
        pushToast(t('cronSkills.form.toastCurrentSessionRequired'), { tone: 'danger' })
        return
      }
      payload.sessionKey = boundSessionKey
      payload.targetSessionKey = boundSessionKey
      payload.originSessionKey = boundSessionKey
    }
    if (payloadKind === 'reminder' && activeChatSessionKey()) payload.originSessionKey = activeChatSessionKey()
    if (sessionTarget === 'session') {
      if (!targetSessionKey) {
        pushToast(t('cronSkills.form.toastNamedSessionRequired'), { tone: 'danger' })
        return
      }
      payload.targetSessionKey = targetSessionKey
    }

    if (editingJob.value) payload.id = editingJob.value.id
    try {
      await rpc.call(editingJob.value ? 'cron.update' : 'cron.create', payload)
      pushToast(editingJob.value ? t('cronSkills.form.toastUpdated') : t('cronSkills.form.toastCreated'), { tone: 'ok' })
      closePanel()
      options.afterSaved()
    } catch (err) {
      pushToast(t('cronSkills.form.toastSaveFailed', { error: err instanceof Error ? err.message : String(err) }), { tone: 'danger' })
    }
  }

  onUnmounted(() => {
    if (previewTimer) clearTimeout(previewTimer)
  })

  return {
    panelOpen,
    editingJob,
    form,
    cronExplainHuman,
    cronExplainValid,
    cronExplainInvalid,
    cronExplainUpcoming,
    jobModeHint,
    sessionTargetHint,
    showTargetSessionRow,
    targetSessionLabel,
    targetSessionHint,
    messageLabel,
    projectWorkspaces: projectWorkspaces.workspaces,
    projectWorkspacesLoading: projectWorkspaces.isLoading,
    projectWorkspacesLoaded: projectWorkspaces.hasLoaded,
    loadProjectWorkspaces: projectWorkspaces.loadWorkspaces,
    openPanel,
    closePanel,
    onPayloadKindChange,
    onSessionTargetChange,
    applyPreset,
    renderCronExplain,
    saveJob,
  }

  function activeChatSessionKey(): string {
    const routeSession = typeof route.query.session === 'string' ? canonicalOptionalSessionKey(route.query.session) : ''
    if (routeSession) return routeSession
    try {
      return canonicalOptionalSessionKey(localStorage.getItem('opensquilla_active_session') || '')
    } catch {
      return ''
    }
  }
}

export function jobSessionKey(job: CronJob | null): string {
  if (!job) return ''
  return job.originSessionKey ||
    job.origin_session_key ||
    job.targetSessionKey ||
    job.target_session_key ||
    job.sessionKey ||
    job.session_key ||
    ''
}

function canonicalOptionalSessionKey(key: string): string {
  const value = key.trim()
  return value ? canonicalSessionKey(value) : ''
}
