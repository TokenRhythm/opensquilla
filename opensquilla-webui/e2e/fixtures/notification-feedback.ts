import { createApp, defineComponent, h, nextTick, reactive, ref } from 'vue'
import ToastHost from '@/components/ToastHost.vue'
import ConfirmModal from '@/components/ConfirmModal.vue'
import ChannelMembersPanel from '@/components/channels/ChannelMembersPanel.vue'
import { useToasts, type ToastActionResult, type ToastTone } from '@/composables/useToasts'
import { useCronJobs } from '@/composables/cron/useCronJobs'
import { useChannelMembers } from '@/composables/channels/useChannelMembers'
import { notifyCronRunFinished } from '@/utils/cron/notifications'
import type { AppSettings } from '@/modules/appSettings'
import type { ChannelAdministration, ChannelPairing } from '@/modules/channelAdministration'
import type { CronRunFinished, CronRunOutcome, CronScheduler } from '@/modules/cronScheduler'
import i18n, { loadLocaleMessages } from '@/i18n'
import { getManifest, selectableValueThemes } from '@/themes/registry'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/apple-modern.css'

// Production components and composables, with in-memory domain adapters only.
// This page performs no Gateway requests and never touches a user profile.
const query = new URLSearchParams(location.search)
const language = query.get('lang') === 'en' ? 'en' : 'zh-Hans'
await loadLocaleMessages(language)
i18n.global.locale.value = language
document.documentElement.lang = language
const theme = ref('light')
async function setTheme(id: string) {
  const manifest = getManifest(id)
  if (!manifest || manifest.kind !== 'value') return
  theme.value = id
  document.documentElement.dataset.theme = id
  document.documentElement.style.colorScheme = manifest.capabilities.colorScheme
  await Promise.all([manifest.world?.styles?.(), ...(manifest.world?.fonts?.map(font => font.load()) ?? [])])
  await nextTick()
}
await setTheme(query.get('theme') ?? 'light')

const calls = reactive({ approve: 0, setAdmin: 0, actions: 0, runNow: 0 })
const destination = ref('尚未打开任务')
const mobileKeyboardOpen = ref(false)
const actionMode = ref<'success' | 'reject' | 'stale' | 'pending'>('success')
let resolveAction: ((value: ToastActionResult) => void) | undefined
let rejectAction: ((reason: Error) => void) | undefined
let finishRun: ((result: CronRunOutcome) => void) | undefined
let fixtureSequence = 0
const listeners = new Set<(event: CronRunFinished) => void>()
const pairing: ChannelPairing = { pairingId: 'pairing-fixture', channelName: 'ops', senderId: 'fixture-member', senderName: '验收成员', status: 'pending' }
let pairingApproved = false
let adminGranted = false
let memberReadFailure = false
const unused = async (): Promise<never> => { throw new Error('Unexpected fixture adapter operation') }
const settings: AppSettings = {
  read: async () => ({ ops: adminGranted ? [pairing.senderId] : [] }),
  readAll: unused, readEffective: unused, patch: unused, patchSafe: unused, merge: unused, setTelemetryConsent: unused,
}
const channel: ChannelAdministration = {
  status: async () => [], get: unused, probe: unused, restart: unused, logout: unused,
  subscribeStatus: () => ({ close() {} }),
  listPairings: async () => {
    if (memberReadFailure) throw new Error('Synthetic member refresh failure')
    return [{ ...pairing, status: pairingApproved ? 'approved' : 'pending' }]
  },
  approvePairing: async () => { calls.approve++; pairingApproved = true; return { adminGranted: false } },
  revokePairing: unused,
  setAdmin: async () => { calls.setAdmin++; adminGranted = true },
}
const scheduler: CronScheduler = {
  listJobs: async () => [
    { id: 'job-fixture', name: '验收任务', enabled: true, payloadKind: 'agent_turn' },
    { id: 'reminder-fixture', name: '验收提醒', enabled: true, payloadKind: 'reminder' },
  ],
  saveJob: unused, setEnabled: unused, remove: unused, listRuns: async () => [],
  runNow: () => { calls.runNow++; return new Promise(resolve => { finishRun = resolve }) },
  subscribe: listener => { listeners.add(listener); return { close: () => listeners.delete(listener) } },
}

const Preview = defineComponent({
  setup() {
    const { toasts, pushToast, dismissToast } = useToasts()
    const openJob = (id: string) => { destination.value = `job:${id}` }
    const openSession = (id: string) => { destination.value = `session:${id}` }
    const jobs = useCronJobs(scheduler, { openJob })
    const members = useChannelMembers(settings, channel)
    void members.load('ops')
    const emitFinished = (event: CronRunFinished) => {
      for (const listener of listeners) listener(event)
      return notifyCronRunFinished(event, { pushToast, t: i18n.global.t, openJob, openSession })
    }
    function clear() { for (const toast of [...toasts.value]) dismissToast(toast.id) }
    function showAction(mode: typeof actionMode.value = 'success', duration = 5000) {
      actionMode.value = mode
      pushToast('操作未完成，可从原位置继续。', {
        tone: 'danger', duration,
        action: { label: '重试', onClick: async () => {
          calls.actions++
          if (actionMode.value === 'reject') throw new Error('Synthetic transport failure')
          if (actionMode.value === 'stale') return { handled: false, removeAction: true, tone: 'info', message: '草稿已变化，请从输入框继续。' }
          if (actionMode.value === 'pending') return new Promise<ToastActionResult>((resolve, reject) => { resolveAction = resolve; rejectAction = reject })
          destination.value = 'action:completed'
        } },
      })
    }
    async function runOrder(order: 'rpc-first' | 'event-first', success = false) {
      const runId = `fixture-order-${++fixtureSequence}`
      const run = jobs.runJob('job-fixture')
      const event = { runId, jobId: 'job-fixture', jobName: '验收任务', success, payloadKind: 'agent_turn' }
      const outcome = success ? { runId, reply: '完成' } : { runId, error: '验收网络错误' }
      if (order === 'event-first') emitFinished(event)
      finishRun?.(outcome)
      await run
      if (order === 'rpc-first') emitFinished(event)
      return runId
    }
    async function runReminderOrder(order: 'rpc-first' | 'event-first') {
      await jobs.loadData()
      const runId = `fixture-reminder-order-${++fixtureSequence}`
      const run = jobs.runJob('reminder-fixture')
      const event = { runId, jobId: 'reminder-fixture', jobName: '验收提醒', success: true, payloadKind: 'reminder', summary: '请参加项目会议', sessionKey: 'fixture-reminder' }
      if (order === 'event-first') emitFinished(event)
      finishRun?.({ runId, reply: '请参加项目会议' })
      await run
      const countBeforeEvent = toasts.value.length
      if (order === 'rpc-first') emitFinished(event)
      return { countBeforeEvent, runId }
    }
    const api = {
      setTheme, clear, showAction, emitFinished, runOrder, runReminderOrder,
      setMobileKeyboardOpen: (value: boolean) => { mobileKeyboardOpen.value = value },
      setMemberReadFailure: (value: boolean) => { memberReadFailure = value },
      notify: (message: string, tone: ToastTone = 'info', duration = 5000, dedupeKey?: string) => pushToast(message, { tone, duration, dedupeKey }),
      resolveAction: (ok = true) => { resolveAction?.(ok); resolveAction = undefined },
      rejectAction: () => { rejectAction?.(new Error('Synthetic transport failure')); rejectAction = undefined },
      snapshot: () => ({ calls: { ...calls }, destination: destination.value, toasts: toasts.value.map(({ id, message, tone, busy, action }) => ({ id, message, tone, busy, action: action?.label })), pairingApproved, adminGranted }),
    }
    Object.assign(window, { notificationFeedback: api })
    const button = (label: string, onClick: () => unknown) => h('button', { type: 'button', onClick }, label)
    return () => h('main', { class: 'notification-fixture' }, [
      h('h1', '通知与操作反馈 · 正式组件验收'),
      h('p', '加载源码中的 ToastHost、ConfirmModal、ChannelMembersPanel、useCronJobs 与通知处理函数。任务事件、网络响应和导航目标使用内存替身；不连接真实 Gateway、不读写真实资料。'),
      h('div', { class: 'fixture-controls' }, [
        h('select', { 'aria-label': '验收主题', value: theme.value, onChange: (event: Event) => { void setTheme((event.target as HTMLSelectElement).value) } }, selectableValueThemes.map(item => h('option', { value: item.id }, item.id === 'light' || item.id === 'dark' ? i18n.global.t(`chrome.themeMode.${item.id}`) : item.name))),
        button('清空通知', clear),
      ]),
      h('h2', '任务触发与去重'),
      h('div', { class: 'fixture-controls' }, [
        button('普通完成', () => emitFinished({ runId: `routine-${++fixtureSequence}`, jobId: 'job-fixture', success: true, payloadKind: 'agent_turn' })),
        button('任务失败', () => emitFinished({ runId: `failed-${++fixtureSequence}`, jobId: 'job-fixture', jobName: '验收任务', success: false })),
        button('到点提醒', () => emitFinished({ runId: `reminder-${++fixtureSequence}`, jobId: 'job-fixture', jobName: '验收任务', success: true, payloadKind: 'reminder', summary: '请参加项目会议', sessionKey: 'fixture-reminder' })),
        button('请求先完成', () => runOrder('rpc-first')),
        button('事件先完成', () => runOrder('event-first')),
      ]),
      h('h2', '通知操作与计时'),
      h('div', { class: 'fixture-controls' }, [
        button('可恢复失败', () => showAction()),
        button('异步操作', () => showAction('pending')),
        button('完成异步', () => api.resolveAction()),
        button('异步失败', () => api.rejectAction()),
        button('原操作已失效', () => showAction('stale')),
      ]),
      h('output', { 'data-testid': 'fixture-destination' }, destination.value),
      h('h2', '成员已批准、管理员授权失败'),
      h(ChannelMembersPanel, { members, channelName: 'ops' }),
      h('output', { 'data-testid': 'fixture-calls' }, `批准 ${calls.approve} 次；授权 ${calls.setAdmin} 次；通知操作 ${calls.actions} 次`),
      // Same prop contract as App; the real tab bar/viewport integration is
      // separately exercised through App in notification-feedback.spec.ts.
      h(ToastHost, { mobileTabbarVisible: !mobileKeyboardOpen.value }), h(ConfirmModal),
    ])
  },
})

createApp(Preview).use(i18n).mount('#app')
