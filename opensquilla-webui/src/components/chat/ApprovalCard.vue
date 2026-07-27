<template>
  <!-- Collapsed outcome row after a decision -->
  <div
    v-if="resolution"
    class="approval-outcome"
    :class="outcomeClass"
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
    role="group"
    :aria-label="t('chat.approval.requiredFor', { tool: approval.toolName })"
  >
    <!-- Concise live announcement: screen readers hear only this line, not the full card body -->
    <div
      class="approval-card__announce"
      aria-live="assertive"
      aria-atomic="true"
    >{{ t('chat.approval.neededFor', { tool: approval.toolName }) }}</div>
    <header class="approval-card__head">
      <span class="approval-card__eyebrow">{{ t('chat.approval.required') }}</span>
      <span class="approval-card__tool">{{ approval.toolName }}</span>
      <span v-if="approval.namespace && approval.namespace !== 'exec'" class="approval-card__ns">
        {{ approval.namespace }}
      </span>
      <span v-if="approval.agent" class="approval-card__agent">{{ approval.agent }}</span>
    </header>

    <div class="approval-card__body">
      <template v-if="approval.command">
        <div class="approval-card__label">{{ t('chat.approval.command') }}</div>
        <pre class="approval-card__pre approval-card__pre--cmd">{{ approval.command }}</pre>
      </template>
      <dl v-if="sandboxContextRows.length" class="approval-card__context">
        <div v-for="row in sandboxContextRows" :key="row.key" class="approval-card__context-row">
          <dt>{{ t(row.labelKey) }}</dt>
          <dd>{{ row.value }}</dd>
        </div>
      </dl>
      <template v-else-if="!approval.command && formattedArgs">
        <div class="approval-card__label">{{ t('chat.approval.arguments') }}</div>
        <pre class="approval-card__pre">{{ formattedArgs }}</pre>
      </template>
      <p v-if="approval.warning" class="approval-card__warning">{{ approval.warning }}</p>
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
      <input
        v-model="denyNote"
        class="approval-card__note"
        type="text"
        :placeholder="t('chat.approval.denyReasonPlaceholder')"
        :aria-label="t('chat.approval.denyReasonLabel')"
        :disabled="busy"
      />
      <div class="approval-card__actions">
        <button class="btn btn--primary" type="button" :disabled="busy" @click="$emit('allow-once')">
          {{ t('chat.approval.allowOnce') }}
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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatApprovalItem, ChatApprovalResolution } from '@/composables/chat/useChatApprovals'
import { formatCountdown } from '@/composables/chat/useChatApprovals'

// Below this remaining time the countdown switches to the warning token and
// reveals the Extend affordance (WCAG 2.2.1: a countdown alone is not enough).
const WARN_THRESHOLD_SECONDS = 60

const { t } = useI18n()

const props = defineProps<{
  approval: ChatApprovalItem
  resolution: ChatApprovalResolution | null
  busy?: boolean
  error?: string
}>()

const emit = defineEmits<{
  'allow-once': []
  'allow-always': []
  deny: [note: string]
  extend: []
}>()

const denyNote = ref('')

// A 1s tick drives the countdown; only mounted while a pending card is shown.
// Skip ticks while the tab is hidden so background-tab CPU is not wasted and
// the visible countdown does not jump on tab restore.
const now = ref(Date.now())
let tick: ReturnType<typeof setInterval> | null = null

function startTick() {
  if (tick) return
  tick = setInterval(() => {
    if (!document.hidden) now.value = Date.now()
  }, 1000)
}

function onVisibilityChange() {
  if (!document.hidden) now.value = Date.now()
}

onMounted(() => {
  startTick()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  if (tick) clearInterval(tick)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

const remainingSeconds = computed(() => {
  if (!props.approval.deadline) return null
  return Math.max(0, Math.round(props.approval.deadline - now.value / 1000))
})

const showCountdown = computed(() => !props.resolution && remainingSeconds.value !== null)
const timeIsLow = computed(() =>
  remainingSeconds.value !== null && remainingSeconds.value <= WARN_THRESHOLD_SECONDS)
const countdownText = computed(() =>
  remainingSeconds.value === null ? '' : t('chat.approval.expiresIn', { time: formatCountdown(remainingSeconds.value) }))

const isSandboxApproval = computed(() =>
  String(props.approval.approvalKind || props.approval.args?.approvalKind || '').startsWith('sandbox_'))

const sandboxContextRows = computed(() => {
  const args = props.approval.args
  const kind = String(props.approval.approvalKind || args?.approvalKind || '')
  if (!args || !kind.startsWith('sandbox_')) return []
  const target = kind === 'sandbox_network'
    ? [args.host, args.bundle_id]
        .filter(value => value != null && ['string', 'number', 'boolean'].includes(typeof value))
        .join(' · ')
    : args.path
  return [
    { key: 'target', labelKey: 'chat.approval.target', value: target },
    { key: 'access', labelKey: 'chat.approval.access', value: args.access },
    { key: 'workspace', labelKey: 'chat.approval.workspace', value: args.workspace },
  ].filter((row): row is { key: string; labelKey: string; value: string | number | boolean } =>
    row.value != null && ['string', 'number', 'boolean'].includes(typeof row.value))
})

const formattedArgs = computed(() => {
  if (!props.approval.args) return ''
  try {
    return JSON.stringify(props.approval.args, null, 2)
  } catch {
    return String(props.approval.args)
  }
})

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
  const text = props.approval.command || props.approval.toolName || ''
  return text.length > 60 ? text.slice(0, 60) + '…' : text
})

function emitDeny() {
  if (props.busy) return
  emit('deny', denyNote.value)
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
  margin: var(--sp-2) auto;
  background: var(--bg-surface);
  border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--border));
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* Direct child of the .chat-thread flex column: overflow:hidden drops the
     automatic min-height, so without this the card collapses when the thread
     scrolls. */
  flex-shrink: 0;
  animation: card-enter var(--dur-enter) var(--ease-out) both;
}

.approval-card__head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4) 0;
}

.approval-card__eyebrow {
  color: var(--warn);
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.approval-card__tool {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-card__ns,
.approval-card__agent {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  padding: 1px var(--sp-2);
}

.approval-card__body {
  max-height: 280px;
  overflow: auto;
  padding: var(--sp-3) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
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

.approval-card__context {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
}

.approval-card__context-row {
  display: grid;
  grid-template-columns: minmax(88px, auto) 1fr;
  gap: var(--sp-3);
}

.approval-card__context-row dt {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 600;
}

.approval-card__context-row dd {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  margin: 0;
  overflow-wrap: anywhere;
}

/* Sticky action bar: the body above scrolls, this footer stays visible. */
.approval-card__footer {
  position: sticky;
  bottom: 0;
  background: var(--bg-surface);
  border-top: 1px solid var(--hairline);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
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

.approval-card__note {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--fs-sm);
  padding: var(--sp-2) var(--sp-3);
  width: 100%;
}

.approval-card__note:focus-visible {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
  outline: none;
}

.approval-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.approval-card__deny {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
  color: var(--danger);
}

.approval-card__deny:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-surface));
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

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(7px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 768px) {
  .approval-card__actions {
    flex-direction: column;
    align-items: stretch;
  }

  .approval-card__actions .btn {
    justify-content: center;
  }
}

@media (prefers-reduced-motion: reduce) {
  .approval-card {
    animation: none;
  }
}
</style>
