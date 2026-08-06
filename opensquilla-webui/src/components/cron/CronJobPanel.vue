<template>
  <Teleport to="body">
    <Transition name="panel">
      <div v-if="open" class="cron-panel-overlay">
        <div class="cron-panel__scrim" :class="{ 'is-open': open }" @click="emit('close')" />
        <div
          ref="drawerRef"
          class="cron-panel"
          :class="{ 'is-open': open }"
          role="dialog"
          aria-modal="true"
          :aria-label="editingJob ? t('cronSkills.panel.ariaEdit') : t('cronSkills.panel.ariaCreate')"
        >
          <div class="cron-panel__head">
            <div>
              <span class="cron-panel__eyebrow">{{ editingJob ? t('cronSkills.panel.eyebrowEdit') : t('cronSkills.panel.eyebrowNew') }}</span>
              <h3 class="cron-panel__title">{{ editingJob ? t('cronSkills.panel.titleEdit') : t('cronSkills.panel.titleCreate') }}</h3>
            </div>
            <button class="cron-iconbtn" :aria-label="t('common.close')" @click="emit('close')">
              <Icon name="x" :size="16" />
            </button>
          </div>
          <div class="cron-panel__body">
            <div class="cron-field">
              <label class="cron-field__label" for="cp-name">{{ t('cronSkills.panel.name') }}</label>
              <input id="cp-name" v-model="form.name" class="cron-field__input" type="text" :placeholder="t('cronSkills.panel.friendlyNamePlaceholder')" autocomplete="off">
            </div>

            <div class="cron-field">
              <label class="cron-field__label" for="cp-type">{{ t('cronSkills.panel.scheduleType') }}</label>
              <select id="cp-type" v-model="form.type" class="cron-field__input">
                <option value="cron">{{ t('cronSkills.panel.friendlyTypeCron') }}</option>
                <option value="every">{{ t('cronSkills.panel.friendlyTypeEvery') }}</option>
                <option value="at">{{ t('cronSkills.panel.friendlyTypeAt') }}</option>
              </select>
            </div>

            <div v-show="form.type === 'cron'" class="cron-field">
              <label class="cron-field__label" for="cp-repeat-kind">{{ t('cronSkills.panel.executionFrequency') }}</label>
              <select id="cp-repeat-kind" v-model="friendlyScheduleKind" class="cron-field__input"><option value="daily">{{ t('cronSkills.panel.daily') }}</option><option value="weekdays">{{ t('cronSkills.panel.weekdays') }}</option><option value="weekly">{{ t('cronSkills.panel.weekly') }}</option><option value="monthly">{{ t('cronSkills.panel.monthly') }}</option><option value="custom">{{ t('cronSkills.panel.customAdvancedTime') }}</option></select>
              <div v-if="friendlyScheduleKind !== 'custom'" class="cron-friendly-time-row">
                <div v-if="friendlyScheduleKind === 'weekly'" class="cron-friendly-time-field"><label class="cron-field__label" for="cp-weekday">{{ t('cronSkills.panel.weekday') }}</label><select id="cp-weekday" v-model="friendlyWeekday" class="cron-field__input"><option value="1">{{ t('cronSkills.explain.dow.mon') }}</option><option value="2">{{ t('cronSkills.explain.dow.tue') }}</option><option value="3">{{ t('cronSkills.explain.dow.wed') }}</option><option value="4">{{ t('cronSkills.explain.dow.thu') }}</option><option value="5">{{ t('cronSkills.explain.dow.fri') }}</option><option value="6">{{ t('cronSkills.explain.dow.sat') }}</option><option value="0">{{ t('cronSkills.explain.dow.sun') }}</option></select></div>
                <div v-if="friendlyScheduleKind === 'monthly'" class="cron-friendly-time-field"><label class="cron-field__label" for="cp-month-day">{{ t('cronSkills.panel.date') }}</label><select id="cp-month-day" v-model="friendlyMonthDay" class="cron-field__input"><option v-for="day in 28" :key="day" :value="String(day)">{{ t('cronSkills.panel.monthlyDay', { day }) }}</option></select></div>
                <div class="cron-friendly-time-field"><label class="cron-field__label" for="cp-friendly-time">{{ t('cronSkills.panel.specificTime') }}</label><input id="cp-friendly-time" v-model="friendlyTime" class="cron-field__input cron-friendly-time-input" type="time" step="60"></div>
              </div>
              <div v-else class="cron-field__hint cron-custom-time-hint"><span>{{ t('cronSkills.panel.customTimeHint') }}</span><button type="button" class="btn btn--ghost" @click="openAdvancedSchedule">{{ t('cronSkills.panel.openAdvancedTime') }}</button></div>
            </div>
            <div v-show="form.type === 'every'" class="cron-field"><label class="cron-field__label" for="cp-every-friendly">{{ t('cronSkills.panel.everyHowOften') }}</label><div class="cron-friendly-time-row"><input id="cp-every-friendly" v-model.number="friendlyEveryAmount" class="cron-field__input" type="number" min="1"><select v-model="friendlyEveryUnit" class="cron-field__input" :aria-label="t('cronSkills.panel.timeUnit')" @change="syncFriendlyEvery"><option value="minutes">{{ t('cronSkills.panel.minutes') }}</option><option value="hours">{{ t('cronSkills.panel.hours') }}</option><option value="days">{{ t('cronSkills.panel.days') }}</option></select></div></div>
            <div v-show="form.type === 'at'" class="cron-field"><label class="cron-field__label" for="cp-at-friendly">{{ t('cronSkills.panel.dateAndTime') }}</label><input id="cp-at-friendly" v-model="friendlyAt" class="cron-field__input" type="datetime-local"></div>
            <div class="cron-field">
              <label class="cron-field__label" for="cp-payload-kind-simple">{{ t('cronSkills.panel.jobMode') }}</label>
              <select id="cp-payload-kind-simple" v-model="form.payloadKind" class="cron-field__input" @change="emit('payloadKindChange')"><option value="reminder">{{ t('cronSkills.panel.modeReminder') }}</option><option value="agent_turn">{{ t('cronSkills.panel.modeAgentTurn') }}</option><option value="system_event">{{ t('cronSkills.panel.modeSystemEvent') }}</option></select>
              <div class="cron-field__hint">{{ jobModeHint }}</div>
            </div>
            <div v-if="form.payloadKind === 'agent_turn'" class="cron-field">
              <label class="cron-field__label" for="cp-workspace">
                {{ t('cronSkills.panel.projectWorkspace') }}
                <span v-if="form.workspaceRequired" aria-hidden="true">*</span>
              </label>
              <select
                id="cp-workspace"
                v-model="form.workspaceId"
                class="cron-field__input"
                :required="form.workspaceRequired"
                :disabled="projectWorkspacesLoading"
              >
                <option value="" :disabled="form.workspaceRequired">{{ t('cronSkills.panel.noWorkspace') }}</option>
                <option
                  v-for="workspace in projectWorkspaces"
                  :key="workspace.id"
                  :value="workspace.id"
                  :disabled="!workspace.available"
                >
                  {{ workspace.name }}{{ workspace.available ? '' : ` · ${t('cronSkills.panel.workspaceUnavailable')}` }}
                </option>
              </select>
              <div class="cron-field__hint">
                {{ form.workspaceRequired ? t('cronSkills.panel.workspaceRequiredHint') : t('cronSkills.panel.workspaceOptionalHint') }}
              </div>
            </div>
            <div class="cron-field"><label class="cron-field__label" for="cp-message">{{ messageLabel }}</label><textarea id="cp-message" v-model="form.message" class="cron-field__input cron-field__input--textarea" rows="4" :placeholder="t('cronSkills.panel.friendlyMessagePlaceholder')" /></div>
            <details ref="runtimeSettingsRef" class="cron-advanced cron-advanced--runtime">
              <summary class="cron-advanced__summary">{{ t('cronSkills.panel.moreRuntimeSettings') }}</summary>
              <div class="cron-advanced__body">
                <div class="cron-field"><label class="cron-field__label" for="cp-cron">{{ t('cronSkills.panel.cronExpression') }}</label><input id="cp-cron" v-model="form.cron" class="cron-field__input cron-field__input--mono" type="text" placeholder="0 9 * * 1-5" autocomplete="off" spellcheck="false" @input="emit('cronInput')"><div class="cron-field__hint">{{ t('cronSkills.panel.advancedTimeHint') }}</div><div v-if="cronExplainHuman" class="cron-explain" :class="{ 'is-valid': cronExplainValid, 'is-invalid': cronExplainInvalid }"><div class="cron-explain__human">{{ cronExplainHuman }}</div></div></div>
                <div class="cron-field"><label class="cron-field__label" for="cp-tz">{{ t('cronSkills.panel.timezone') }}</label><input id="cp-tz" v-model="form.tz" class="cron-field__input cron-field__input--mono" type="text" placeholder="Asia/Shanghai" autocomplete="off" spellcheck="false"><div class="cron-field__hint">{{ t('cronSkills.panel.timezoneSimpleHint') }}</div></div>
                <div class="cron-field"><label class="cron-field__label" for="cp-agent-id">{{ t('cronSkills.panel.agentId') }}</label><input id="cp-agent-id" v-model="form.agentId" class="cron-field__input" type="text" placeholder="main"></div>
                <div v-show="form.payloadKind === 'agent_turn'" class="cron-field"><label class="cron-field__label" for="cp-session-target">{{ t('cronSkills.panel.sessionTarget') }}</label><select id="cp-session-target" v-model="form.sessionTarget" class="cron-field__input" @change="emit('sessionTargetChange')"><option value="main">{{ t('cronSkills.panel.targetMain') }}</option><option value="current">{{ t('cronSkills.panel.targetCurrent') }}</option><option value="isolated">{{ t('cronSkills.panel.targetIsolated') }}</option><option value="session">{{ t('cronSkills.panel.targetNamed') }}</option></select><div class="cron-field__hint">{{ sessionTargetHint }}</div></div>
                <div v-show="showTargetSessionRow" class="cron-field"><label class="cron-field__label" for="cp-target-session-key">{{ targetSessionLabel }}</label><input id="cp-target-session-key" v-model="form.targetSessionKey" class="cron-field__input" type="text" placeholder="agent:main:webchat:abc123"><div class="cron-field__hint">{{ targetSessionHint }}</div></div>                <details class="cron-advanced">
                <summary class="cron-advanced__summary">{{ t('cronSkills.panel.advancedSummary') }}</summary>
                <div class="cron-advanced__body">
                <div class="cron-field">
                <label class="cron-field__label" for="cp-wake-mode">{{ t('cronSkills.panel.wakeMode') }}</label>
                <select id="cp-wake-mode" v-model="form.wakeMode" class="cron-field__input">
                <option value="now">{{ t('cronSkills.panel.wakeNow') }}</option>
                <option value="next-heartbeat">{{ t('cronSkills.panel.wakeNextHeartbeat') }}</option>
                </select>
                <i18n-t keypath="cronSkills.panel.wakeModeHint" tag="div" class="cron-field__hint">
                <template #code><code>next-heartbeat</code></template>
                </i18n-t>
                </div>

                <div class="cron-field">
                <label class="cron-field__label" for="cp-delivery-mode">{{ t('cronSkills.panel.deliveryMode') }}</label>
                <select id="cp-delivery-mode" v-model="form.deliveryMode" class="cron-field__input">
                <option value="">{{ t('cronSkills.panel.deliveryDefault') }}</option>
                <option value="none">{{ t('cronSkills.panel.deliveryNone') }}</option>
                <option value="announce">{{ t('cronSkills.panel.deliveryAnnounce') }}</option>
                <option value="webhook">{{ t('cronSkills.panel.deliveryWebhook') }}</option>
                </select>
                </div>

                <div v-show="form.deliveryMode === 'announce'" class="cron-field">
                <label class="cron-field__label" for="cp-delivery-channel">{{ t('cronSkills.panel.channel') }}</label>
                <input id="cp-delivery-channel" v-model="form.deliveryChannel" class="cron-field__input" type="text" placeholder="slack" autocomplete="off">
                </div>
                <div v-show="form.deliveryMode === 'announce'" class="cron-field">
                <label class="cron-field__label" for="cp-delivery-to">{{ t('cronSkills.panel.recipient') }}</label>
                <input id="cp-delivery-to" v-model="form.deliveryTo" class="cron-field__input" type="text" placeholder="C-team-alerts" autocomplete="off">
                </div>
                <div v-show="form.deliveryMode === 'announce'" class="cron-field">
                <label class="cron-field__label" for="cp-delivery-account">{{ t('cronSkills.panel.accountId') }}</label>
                <input id="cp-delivery-account" v-model="form.deliveryAccount" class="cron-field__input" type="text" autocomplete="off">
                </div>

                <div v-show="form.deliveryMode === 'webhook'" class="cron-field">
                <label class="cron-field__label" for="cp-delivery-webhook-url">{{ t('cronSkills.panel.webhookUrl') }}</label>
                <input id="cp-delivery-webhook-url" v-model="form.deliveryWebhookUrl" class="cron-field__input cron-field__input--mono" type="url" placeholder="https://hooks.example/cron" autocomplete="off">
                </div>
                <div v-show="form.deliveryMode === 'webhook'" class="cron-field">
                <label class="cron-field__label" for="cp-delivery-webhook-token">{{ t('cronSkills.panel.webhookToken') }}</label>
                <input id="cp-delivery-webhook-token" v-model="form.deliveryWebhookToken" class="cron-field__input" type="password" :placeholder="t('cronSkills.panel.webhookTokenPlaceholder')" autocomplete="off">
                </div>

                <label v-show="form.deliveryMode === 'announce' || form.deliveryMode === 'webhook'" class="cron-toggle">
                <ControlSwitch v-model:checked="form.deliveryBestEffort" :aria-label="t('cronSkills.panel.bestEffort')" />
                <span class="cron-toggle__label">{{ t('cronSkills.panel.bestEffort') }}</span>
                </label>

                <details class="cron-advanced cron-advanced--nested">
                <summary class="cron-advanced__summary">{{ t('cronSkills.panel.failureDestination') }}</summary>
                <div class="cron-advanced__body">
                <div class="cron-field">
                <label class="cron-field__label" for="cp-fd-mode">{{ t('cronSkills.panel.routeFailuresTo') }}</label>
                <select id="cp-fd-mode" v-model="form.fdMode" class="cron-field__input">
                <option value="">{{ t('cronSkills.panel.fdDisabled') }}</option>
                <option value="channel">{{ t('cronSkills.panel.fdChannel') }}</option>
                <option value="webhook">{{ t('cronSkills.panel.fdWebhook') }}</option>
                </select>
                </div>
                <div v-show="form.fdMode === 'channel'" class="cron-field">
                <label class="cron-field__label" for="cp-fd-channel">{{ t('cronSkills.panel.channel') }}</label>
                <input id="cp-fd-channel" v-model="form.fdChannel" class="cron-field__input" type="text" placeholder="slack" autocomplete="off">
                </div>
                <div v-show="form.fdMode === 'channel'" class="cron-field">
                <label class="cron-field__label" for="cp-fd-to">{{ t('cronSkills.panel.recipient') }}</label>
                <input id="cp-fd-to" v-model="form.fdTo" class="cron-field__input" type="text" placeholder="C-ops-alerts" autocomplete="off">
                </div>
                <div v-show="form.fdMode === 'channel'" class="cron-field">
                <label class="cron-field__label" for="cp-fd-account">{{ t('cronSkills.panel.accountId') }}</label>
                <input id="cp-fd-account" v-model="form.fdAccount" class="cron-field__input" type="text" autocomplete="off">
                </div>
                <div v-show="form.fdMode === 'webhook'" class="cron-field">
                <label class="cron-field__label" for="cp-fd-webhook-url">{{ t('cronSkills.panel.webhookUrl') }}</label>
                <input id="cp-fd-webhook-url" v-model="form.fdWebhookUrl" class="cron-field__input cron-field__input--mono" type="url" placeholder="https://hooks.example/alert" autocomplete="off">
                </div>
                <div v-show="form.fdMode === 'webhook'" class="cron-field">
                <label class="cron-field__label" for="cp-fd-webhook-token">{{ t('cronSkills.panel.webhookToken') }}</label>
                <input id="cp-fd-webhook-token" v-model="form.fdWebhookToken" class="cron-field__input" type="password" :placeholder="t('cronSkills.panel.webhookTokenPlaceholder')" autocomplete="off">
                </div>
                </div>
                </details>
                </div>
                </details>
              </div>
            </details>

            <label class="cron-toggle">
              <ControlSwitch v-model:checked="form.enabled" :aria-label="t('cronSkills.panel.enabled')" />
              <span class="cron-toggle__label">{{ t('cronSkills.panel.enabled') }}</span>
            </label>

            <div class="cron-panel__actions">
              <button class="btn btn--primary" @click="emit('save')">{{ t('cronSkills.panel.saveSchedule') }}</button>
              <button class="btn btn--ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'
import type { CronJob, CronJobFormModel } from '@/types/cron'
import type { ProjectWorkspaceItem } from '@/types/rpc'
import {
  atScheduleValueFromLocalInput,
  localDateTimeInputValue,
} from '@/utils/cron/atSchedule'
import { useDialogA11y } from '@/composables/useDialogA11y'

const { t } = useI18n()

const props = defineProps<{
  open: boolean
  editingJob: CronJob | null
  cronExplainHuman: string
  cronExplainValid: boolean
  cronExplainInvalid: boolean
  cronExplainUpcoming: Date[]
  jobModeHint: string
  sessionTargetHint: string
  showTargetSessionRow: boolean
  targetSessionLabel: string
  targetSessionHint: string
  messageLabel: string
  projectWorkspaces: ProjectWorkspaceItem[]
  projectWorkspacesLoading: boolean
}>()

const form = defineModel<CronJobFormModel>('form', { required: true })
const friendlyEveryUnit = ref<'minutes' | 'hours' | 'days'>('minutes')
const everyUnitSeconds = computed(() => friendlyEveryUnit.value === 'days' ? 86400 : friendlyEveryUnit.value === 'hours' ? 3600 : 60)
const friendlyEveryAmount = computed({ get: () => Math.max(1, Math.round((Number(form.value.every) || 60) / everyUnitSeconds.value)), set: value => { form.value.every = String(Math.max(1, Number(value) || 1) * everyUnitSeconds.value) } })
const friendlyAt = computed({
  get: () => localDateTimeInputValue(form.value.at),
  set: value => {
    form.value.at = atScheduleValueFromLocalInput(value, form.value.at)
  },
})
function cronParts(): string[] { const parts = (form.value.cron || '').trim().split(/\s+/); return parts.length === 5 ? parts : ['0', '9', '*', '*', '*'] }
function setFriendlyCron(kind: string, time = friendlyTime.value, weekday = friendlyWeekday.value, monthDay = friendlyMonthDay.value) { if (kind === 'custom') return; const [hourText, minuteText] = (time || '09:00').split(':'); const hour = String(Number(hourText) || 0); const minute = String(Number(minuteText) || 0); if (kind === 'weekdays') form.value.cron = `${minute} ${hour} * * 1-5`; else if (kind === 'weekly') form.value.cron = `${minute} ${hour} * * ${weekday}`; else if (kind === 'monthly') form.value.cron = `${minute} ${hour} ${monthDay} * *`; else form.value.cron = `${minute} ${hour} * * *`; emit('cronInput') }
const customScheduleSelected = ref(false)
const runtimeSettingsRef = ref<HTMLDetailsElement | null>(null)
const friendlyScheduleKind = computed({
  get: () => {
    if (customScheduleSelected.value) return 'custom'
    const [, , day, month, weekday] = cronParts()
    if (month !== '*') return 'custom'
    if (weekday === '1-5' && day === '*') return 'weekdays'
    if (/^[0-6]$/.test(weekday) && day === '*') return 'weekly'
    if (/^(?:[1-9]|1\d|2[0-8])$/.test(day) && weekday === '*') return 'monthly'
    if (day === '*' && weekday === '*') return 'daily'
    return 'custom'
  },
  set: value => {
    customScheduleSelected.value = value === 'custom'
    if (value === 'custom') openAdvancedSchedule()
    else setFriendlyCron(value)
  },
})
function openAdvancedSchedule() {
  customScheduleSelected.value = true
  if (runtimeSettingsRef.value) runtimeSettingsRef.value.open = true
  nextTick(() => document.querySelector<HTMLInputElement>('#cp-cron')?.focus())
}
const friendlyTime = computed({ get: () => { const [minute, hour] = cronParts(); return `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}` }, set: value => setFriendlyCron(friendlyScheduleKind.value, value) })
const friendlyWeekday = computed({ get: () => { const weekday = cronParts()[4]; return /^[0-6]$/.test(weekday) ? weekday : '1' }, set: value => setFriendlyCron('weekly', friendlyTime.value, value) })
const friendlyMonthDay = computed({ get: () => { const day = cronParts()[2]; return /^(?:[1-9]|1\d|2[0-8])$/.test(day) ? day : '1' }, set: value => setFriendlyCron('monthly', friendlyTime.value, friendlyWeekday.value, value) })
function syncFriendlyEvery() { friendlyEveryAmount.value = friendlyEveryAmount.value }

const emit = defineEmits<{
  close: []
  save: []
  cronInput: []
  preset: [cron: string]
  payloadKindChange: []
  sessionTargetChange: []
}>()

const drawerRef = ref<HTMLElement | null>(null)
const openRef = toRef(props, 'open')
watch(openRef, open => {
  if (open) customScheduleSelected.value = false
})
useDialogA11y(drawerRef, openRef, () => emit('close'))
</script>
