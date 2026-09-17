<template>
  <!-- Collapsed outcome row after a decision -->
  <div
    v-if="resolution"
    class="approval-outcome"
    :class="[outcomeClass, { 'approval-outcome--timeline': timeline }]"
    data-testid="approval-outcome"
    role="status"
  >
    <Icon :name="outcomeIcon" :size="14" />
    <span class="approval-outcome__text">{{ outcomeText }}</span>
    <code v-if="summary" class="approval-outcome__summary" :title="summary">{{ summary }}</code>
  </div>

  <!-- Pending approval card -->
  <article
    v-else
    class="approval-card"
    data-testid="approval-card"
    :data-approval-id="approval.id"
    tabindex="-1"
    role="group"
    :aria-label="t('chat.approval.requiredFor', { action: semanticTitle })"
    :class="{
      'approval-card--timeline': timeline,
      'approval-card--danger': approval.irreversible,
    }"
  >
    <!-- Concise live announcement: screen readers hear only this line, not the full card body -->
    <div
      class="approval-card__announce"
      aria-live="assertive"
      aria-atomic="true"
    >{{ t('chat.approval.neededFor', { action: semanticTitle }) }}</div>
    <header class="approval-card__head">
      <span class="approval-card__icon" aria-hidden="true">
        <Icon name="shield" :size="18" />
      </span>
      <div class="approval-card__heading">
        <span class="approval-card__eyebrow">{{ t('chat.approval.required') }}</span>
        <h3 class="approval-card__title">{{ semanticTitle }}</h3>
      </div>
    </header>

    <div class="approval-card__body">
      <dl v-if="showTarget" class="approval-card__context">
        <div class="approval-card__context-row">
          <dt>{{ t('chat.approval.target') }}</dt>
          <dd><code class="approval-card__target">{{ approval.displayTarget }}</code></dd>
        </div>
      </dl>
      <template v-if="showCommand">
        <div class="approval-card__label">{{ t('chat.approval.command') }}</div>
        <pre class="approval-card__pre approval-card__pre--cmd">{{ displayCommand }}</pre>
      </template>
      <section
        v-if="riskTitle"
        class="approval-card__risk"
        :class="riskClass"
        role="note"
      >
        <span class="approval-card__risk-icon" aria-hidden="true">
          <Icon :name="riskIcon" :size="14" />
        </span>
        <div class="approval-card__risk-copy">
          <strong>{{ riskTitle }}</strong>
          <p v-if="riskBody">{{ riskBody }}</p>
          <p v-if="riskSecondary">{{ riskSecondary }}</p>
        </div>
      </section>
      <p v-if="visibleWarning" class="approval-card__warning">{{ visibleWarning }}</p>
    </div>

    <footer class="approval-card__footer">
      <div
        v-if="showCountdown"
        class="approval-card__timer"
        :class="{ 'approval-card__timer--warn': timeIsLow }"
      >
        <span
          class="approval-card__timer-text"
          :aria-live="timeIsLow ? 'assertive' : 'polite'"
        >{{ countdownText }}</span>
        <button
          v-if="timeIsLow"
          class="btn btn--ghost approval-card__extend"
          type="button"
          :disabled="busy"
          @click="$emit('extend')"
        >
          {{ t('chat.approval.extend') }}
        </button>
      </div>
      <div class="approval-card__actions">
        <button class="btn btn--primary" type="button" :disabled="busy" @click="$emit('allow-once')">
          {{ allowLabel }}
        </button>
        <button
          v-if="isSandboxApproval"
          class="btn btn--ghost"
          type="button"
          :disabled="busy"
          @click="$emit('allow-always')"
        >
          {{ t('chat.approval.allowSameType') }}
        </button>
        <button
          class="btn approval-card__deny"
          type="button"
          :disabled="busy"
          @click="emitDeny"
        >
          {{ t('chat.approval.deny') }}
        </button>
      </div>
      <p v-if="error" class="approval-card__error" role="alert">{{ error }}</p>
    </footer>
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatApprovalItem, ChatApprovalResolution } from '@/composables/chat/useChatApprovals'
import { formatCountdown } from '@/composables/chat/useChatApprovals'

const WARN_THRESHOLD_SECONDS = 60

const { t } = useI18n()

const props = defineProps<{
  approval: ChatApprovalItem
  resolution: ChatApprovalResolution | null
  busy?: boolean
  error?: string
  timeline?: boolean
}>()

const emit = defineEmits<{
  'allow-once': []
  'allow-always': []
  deny: []
  extend: []
}>()

const now = ref(Date.now())
let mounted = false
let tick: ReturnType<typeof setInterval> | null = null

const deadline = computed(() => {
  const value = Number(props.approval.deadline)
  return Number.isFinite(value) && value > 0 ? value : null
})
const remainingSeconds = computed(() =>
  deadline.value === null
    ? null
    : Math.max(0, Math.round(deadline.value - now.value / 1000)))
const showCountdown = computed(() =>
  !props.resolution && remainingSeconds.value !== null)
const timeIsLow = computed(() =>
  remainingSeconds.value !== null
  && remainingSeconds.value <= WARN_THRESHOLD_SECONDS)
const countdownText = computed(() =>
  remainingSeconds.value === null
    ? ''
    : t('chat.approval.expiresIn', {
        time: formatCountdown(remainingSeconds.value),
      }))

function stopTick() {
  if (tick) clearInterval(tick)
  tick = null
}

function syncTick() {
  now.value = Date.now()
  if (!mounted || !showCountdown.value) {
    stopTick()
    return
  }
  if (!tick) {
    tick = setInterval(() => {
      if (!document.hidden) now.value = Date.now()
    }, 1000)
  }
}

function onVisibilityChange() {
  if (!document.hidden) now.value = Date.now()
}

watch(
  () => [props.approval.deadline, props.resolution],
  syncTick,
)
onMounted(() => {
  mounted = true
  document.addEventListener('visibilitychange', onVisibilityChange)
  syncTick()
})
onBeforeUnmount(() => {
  mounted = false
  stopTick()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

const DISPLAY_KIND_KEYS: Record<string, string> = {
  delete: 'delete',
  modify: 'modify',
  create: 'create',
  run_command: 'runCommand',
  run_code: 'runCode',
  network_access: 'networkAccess',
  path_access: 'pathAccess',
  plugin_permission: 'pluginPermission',
  sensitive_operation: 'sensitiveOperation',
}

const semanticTitle = computed(() => {
  const key = DISPLAY_KIND_KEYS[String(props.approval.displayKind || '')]
    || 'sensitiveOperation'
  return t(`chat.approval.kinds.${key}`)
})

const showTarget = computed(() =>
  Boolean(props.approval.displayTarget)
  && props.approval.displayKind !== 'run_command')

const displayCommand = computed(() =>
  props.approval.displayKind === 'run_command'
    ? props.approval.displayTarget || props.approval.command
    : '')

const showCommand = computed(() =>
  Boolean(displayCommand.value))

const isSandboxApproval = computed(() =>
  !props.approval.destructive
  && String(props.approval.approvalKind || '').startsWith('sandbox_'))

const riskTitle = computed(() => {
  if (props.approval.backupState === 'enabled') return t('chat.approval.backup.enabledTitle')
  if (props.approval.backupState === 'disabled') return t('chat.approval.backup.disabledTitle')
  if (props.approval.backupState === 'unavailable_requires_confirmation') {
    return t('chat.approval.backup.unavailableTitle')
  }
  if (props.approval.irreversible) return t('chat.approval.irreversibleTitle')
  return ''
})

const riskBody = computed(() => {
  if (props.approval.backupState === 'enabled') return t('chat.approval.backup.enabledBody')
  if (props.approval.backupState === 'disabled') return t('chat.approval.backup.disabledBody')
  if (props.approval.backupState === 'unavailable_requires_confirmation') {
    return t('chat.approval.backup.unavailableBody')
  }
  if (props.approval.irreversible) return t('chat.approval.irreversibleBody')
  return ''
})

const riskSecondary = computed(() =>
  props.approval.backupState === 'disabled'
    ? t('chat.approval.backup.settingsHint')
    : '')

const riskClass = computed(() =>
  props.approval.backupState === 'enabled'
    ? 'approval-card__risk--backup'
    : 'approval-card__risk--danger')

const riskIcon = computed(() =>
  props.approval.backupState === 'enabled' ? 'check' : 'info')

const visibleWarning = computed(() =>
  props.approval.destructive ? '' : props.approval.warning)

const allowLabel = computed(() =>
  props.approval.backupState === 'unavailable_requires_confirmation'
    ? t('chat.approval.continueWithoutBackup')
    : t('chat.approval.allowOnce'))

const outcomeText = computed(() => {
  if (props.resolution === 'unavailable') return t('chat.approval.outcomeUnavailable')
  if (props.resolution === 'expired') return t('chat.approval.outcomeExpired')
  if (props.resolution === 'denied') return t('chat.approval.outcomeDenied')
  return t('chat.approval.outcomeApproved')
})

const outcomeClass = computed(() => {
  if (props.resolution === 'unavailable') return 'approval-outcome--unavailable'
  if (props.resolution === 'expired') return 'approval-outcome--expired'
  if (props.resolution === 'denied') return 'approval-outcome--denied'
  return 'approval-outcome--approved'
})

const outcomeIcon = computed(() => {
  if (props.resolution === 'unavailable') return 'info'
  if (props.resolution === 'expired') return 'clock'
  if (props.resolution === 'denied') return 'x'
  return 'check'
})

const summary = computed(() => {
  if (props.timeline && props.resolution) {
    const safe = props.approval.toolName || props.approval.approvalKind
    return safe.length > 60 ? safe.slice(0, 60) + '…' : safe
  }
  const text = props.approval.displayKind === 'run_command'
    ? displayCommand.value
    : props.approval.displayTarget || ''
  return text.length > 60 ? text.slice(0, 60) + '…' : text
})

function emitDeny() {
  if (props.busy) return
  emit('deny')
}
</script>

<style scoped>
/* Visually-hidden but announced by screen readers */
.approval-card__announce {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.approval-card {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  max-width: 780px;
  margin: var(--sp-2) auto;
  background: color-mix(in srgb, var(--bg-surface) 97%, var(--bg));
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* Direct child of the .chat-thread flex column: overflow:hidden drops the
     automatic min-height, so without this the card collapses when the thread
     scrolls. */
  flex-shrink: 0;
}

.approval-card--danger {
  border-color: color-mix(in srgb, var(--border) 82%, transparent);
}

.approval-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

.approval-card__head {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5) 0;
}

.approval-card__icon {
  align-items: center;
  background: color-mix(in srgb, var(--warn) 9%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 20%, var(--border));
  border-radius: var(--radius-sm);
  color: var(--warn);
  display: inline-flex;
  flex: 0 0 auto;
  height: 30px;
  justify-content: center;
  width: 30px;
}

.approval-card--danger .approval-card__icon {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 18%, var(--border));
  color: var(--danger);
}

.approval-card__heading {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.approval-card__eyebrow {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-eyebrow);
  letter-spacing: var(--eyebrow-track);
}

.approval-card__title {
  color: var(--text);
  font-size: var(--fs-md);
  font-weight: 650;
  line-height: 1.35;
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-card__body {
  max-height: 260px;
  overflow: auto;
  padding: var(--sp-3) var(--sp-5) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.approval-card__label {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.approval-card__pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: 0;
  padding: var(--sp-3);
  white-space: pre-wrap;
  word-break: break-word;
}

.approval-card__pre--cmd {
  background: color-mix(in srgb, var(--warn) 6%, var(--bg));
}

.approval-card__warning {
  color: var(--warn);
  font-size: var(--fs-sm);
  margin: 0;
}

.approval-card__risk {
  align-items: start;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  display: grid;
  gap: 10px;
  grid-template-columns: 22px minmax(0, 1fr);
  padding: 10px var(--sp-3);
}

.approval-card__risk-icon {
  align-items: center;
  border-radius: var(--radius-sm);
  display: inline-flex;
  height: 22px;
  justify-content: center;
  width: 22px;
}

.approval-card__risk-copy {
  min-width: 0;
}

.approval-card__risk strong {
  display: block;
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: 1.4;
}

.approval-card__risk p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 3px 0 0;
}

.approval-card__risk--backup {
  background: color-mix(in srgb, var(--ok) 4%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--ok) 16%, var(--border));
}

.approval-card__risk--backup .approval-card__risk-icon {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok);
}

.approval-card__risk--danger {
  background: color-mix(in srgb, var(--danger) 4%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 16%, var(--border));
}

.approval-card__risk--danger .approval-card__risk-icon {
  background: color-mix(in srgb, var(--danger) 9%, transparent);
  color: var(--danger);
}

.approval-card__risk--danger strong {
  color: var(--danger);
}

.approval-card__context {
  margin: 0;
}

.approval-card__context-row {
  align-items: start;
  background: color-mix(in srgb, var(--bg) 58%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: 58px minmax(0, 1fr);
  padding: 10px var(--sp-3);
}

.approval-card__context-row dt {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 600;
  line-height: 1.5;
}

.approval-card__context-row dd {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.approval-card__target {
  background: transparent;
  color: inherit;
  font: inherit;
  padding: 0;
}

/* Sticky action bar: the body above scrolls, this footer stays visible. */
.approval-card__footer {
  position: sticky;
  bottom: 0;
  background: color-mix(in srgb, var(--bg-surface) 97%, var(--bg));
  border-top: 1px solid var(--hairline);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: 10px var(--sp-5) var(--sp-3);
}

.approval-card__timer {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  justify-content: space-between;
}

.approval-card__timer--warn {
  color: var(--warn);
  font-weight: 600;
}

.approval-card__timer-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-card__extend {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
  color: var(--warn);
  flex-shrink: 0;
}

.approval-card__extend:hover:not(:disabled) {
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-surface));
}

.approval-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
}

.approval-card__deny {
  border-color: transparent;
  color: var(--text-muted);
}

.approval-card__deny:hover:not(:disabled),
.approval-card__deny:focus-visible:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 7%, var(--bg-surface));
  color: var(--danger);
}

.approval-card__error {
  color: var(--danger);
  font-size: var(--fs-sm);
  margin: 0;
}

.approval-outcome {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: var(--sp-1) auto;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  min-width: 0;
}

.approval-outcome--approved {
  color: var(--ok);
}

.approval-outcome--denied {
  color: var(--danger);
}

.approval-outcome--expired {
  color: var(--text-muted);
}

.approval-outcome--unavailable {
  color: var(--text-muted);
}

.approval-outcome__text {
  flex-shrink: 0;
}

.approval-outcome__summary {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-outcome--timeline {
  width: 100%;
  margin: 0;
  padding: 7px 8px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, currentColor 5%, transparent);
}

.approval-card--timeline {
  width: 100%;
  max-width: none;
  margin: var(--sp-2) 0;
  box-shadow: none;
}

@media (max-width: 768px) {
  .approval-card {
    width: calc(100% - 24px);
  }

  .approval-card__head {
    padding: var(--sp-3) var(--sp-3) 0;
  }

  .approval-card__body {
    padding: var(--sp-3);
  }

  .approval-card__context-row {
    gap: var(--sp-1);
    grid-template-columns: minmax(0, 1fr);
  }

  .approval-card__footer {
    padding: 10px var(--sp-3) var(--sp-3);
  }

  .approval-card__actions {
    flex-direction: column;
    align-items: stretch;
  }

  .approval-card__actions .btn {
    justify-content: center;
  }
}

</style>
