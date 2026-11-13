<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { JevSnapshot } from '@/modules/jevRouting'

const props = withDefaults(defineProps<{
  snapshot: JevSnapshot
  busy?: boolean
  turnBusy?: boolean
  error?: boolean
}>(), { busy: false, turnBusy: false, error: false })
const emit = defineEmits<{
  respond: [decisionId: string, accepted: boolean]
}>()
const { t } = useI18n()
const id = useId()
const decision = computed(() => props.snapshot.enabled ? props.snapshot.recommendation : null)
const ensemble = computed(() => decision.value?.action === 'ensemble' ? decision.value.ensemble : undefined)
const handoff = computed(() => decision.value?.action === 'new_session' ? decision.value.handoff : undefined)
const pending = computed(() => decision.value?.status === 'pending' && (
  decision.value.action === 'ensemble' ? (ensemble.value?.proposers.length ?? 0) >= 2 && !!ensemble.value?.aggregator.model
    : decision.value.action === 'new_session' ? handoff.value?.sourceSessionKey === props.snapshot.sessionKey
      : decision.value.action !== 'keep' && !!decision.value.target))
const newSession = computed(() => decision.value?.action === 'new_session')
const contextPercent = computed(() => handoff.value && handoff.value.contextWindowTokens > 0
  ? Math.round(handoff.value.contextTokens / handoff.value.contextWindowTokens * 100) : null)
const number = (value: number) => new Intl.NumberFormat().format(value)
const panelElement = ref<HTMLElement | null>(null)
let focusBeforeSuggestion: HTMLElement | null = null
watch(pending, (value, previous) => {
  const active = typeof document === 'undefined' ? null : document.activeElement
  if (value && !previous) {
    focusBeforeSuggestion = typeof HTMLElement !== 'undefined' && active instanceof HTMLElement
      ? active : null
  } else if (!value && previous && panelElement.value?.contains(active)) {
    void nextTick(() => {
      if (focusBeforeSuggestion?.isConnected) focusBeforeSuggestion.focus({ preventScroll: true })
    })
  }
}, { immediate: true })
const trust = computed(() => props.snapshot.mode === 'trust')
const now = ref(Date.now())
const deadline = computed(() => pending.value && !newSession.value ? decision.value?.autoSwitchAt : undefined)
let countdownTimer: ReturnType<typeof setInterval> | undefined
watch(deadline, value => {
  if (countdownTimer !== undefined) clearInterval(countdownTimer)
  countdownTimer = undefined
  now.value = Date.now()
  if (value !== undefined) countdownTimer = setInterval(() => { now.value = Date.now() }, 250)
}, { immediate: true })
onScopeDispose(() => { if (countdownTimer !== undefined) clearInterval(countdownTimer) })
const countdown = computed(() => Math.max(0, Math.ceil(((deadline.value ?? 0) - now.value) / 1000)))
const waitingForOutcome = computed(() => deadline.value !== undefined && countdown.value === 0)
const choicesDisabled = computed(() => props.busy || waitingForOutcome.value)
const countdownKey = computed(() => trust.value
  ? countdown.value > 0 ? 'chat.jev.trustCountdown' : 'chat.jev.trustPending'
  : countdown.value > 0 ? 'chat.jev.suggestCountdown' : 'chat.jev.suggestPending')
const scopeKey = computed(() => newSession.value
  ? props.turnBusy ? 'chat.jev.sessionBusyHint' : 'chat.jev.sessionContextNote'
  : props.turnBusy && !props.snapshot.blocked ? 'chat.jev.safeBoundaryNote' : 'chat.jev.keepContextNote')
const confirmDisabled = computed(() => choicesDisabled.value || (newSession.value && props.turnBusy))

function respond(accepted: boolean) {
  if (!decision.value || !pending.value || choicesDisabled.value || (accepted && confirmDisabled.value)) return
  emit('respond', decision.value.id, accepted)
}
</script>

<template>
  <Transition name="jev-suggestion">
    <section
      v-if="pending && decision"
      ref="panelElement"
      class="jev-panel"
      :aria-labelledby="`${id}-title`"
      :aria-describedby="`${id}-reason`"
      :aria-busy="busy"
    >
      <div class="jev-panel__grip" aria-hidden="true"></div>
      <header class="jev-panel__header">
        <span class="jev-panel__avatar" aria-hidden="true">🐣</span>
        <div class="jev-panel__identity">
          <h3 :id="`${id}-title`">{{ t('chat.jev.suggestionTitle') }}</h3>
          <p>{{ t(newSession ? 'chat.jev.sessionSubtitle' : ensemble ? 'chat.jev.ensembleSubtitle' : 'chat.jev.suggestionSubtitle') }}</p>
        </div>
        <span v-if="trust && !newSession" class="jev-panel__badge">{{ t('chat.jev.trustBadge') }}</span>
      </header>

      <div class="jev-panel__recommendation" aria-live="polite" aria-atomic="true">
        <div v-if="newSession && handoff" class="jev-panel__handoff">
          <strong>{{ t('chat.jev.sessionTitle') }}</strong>
          <p v-if="handoff.completedTurns !== undefined" class="jev-panel__cost">{{ t('chat.jev.sessionTurns', { count: handoff.completedTurns }) }}</p>
          <p class="jev-panel__cost">{{ t(contextPercent === null ? 'chat.jev.sessionTokens' : 'chat.jev.sessionUsage', {
            tokens: number(handoff.contextTokens), window: number(handoff.contextWindowTokens), percent: contextPercent,
          }) }}</p>
          <p class="jev-panel__cost">{{ t('chat.jev.sessionSummaryHint') }}</p>
        </div>
        <template v-else-if="ensemble">
          <div class="jev-panel__models">
            <span :title="`${decision.current.provider} · ${decision.current.model}`">{{ decision.current.model }}</span>
            <Icon name="chevronRight" :size="14" aria-hidden="true" />
            <strong>{{ t('chat.jev.ensembleTitle') }}</strong>
          </div>
          <div class="jev-panel__plan">
            <p>{{ t('chat.jev.ensembleProposers') }}</p>
            <ul><li v-for="(model, index) in ensemble.proposers" :key="`${model.provider}:${model.model}:${index}`">{{ model.model }} <span>· {{ model.provider }}</span></li></ul>
            <p>{{ t('chat.jev.ensembleAggregator') }} <strong>{{ ensemble.aggregator.model }}</strong> <span>· {{ ensemble.aggregator.provider }}</span></p>
          </div>
        </template>
        <div v-else-if="decision.target" class="jev-panel__models">
          <span :title="`${decision.current.provider} · ${decision.current.model}`">{{ decision.current.model }}</span>
          <Icon name="chevronRight" :size="14" aria-hidden="true" />
          <strong :title="`${decision.target.provider} · ${decision.target.model}`">{{ decision.target.model }}</strong>
        </div>
        <p :id="`${id}-reason`" class="jev-panel__reason">{{ decision.reason }}</p>
        <p v-if="decision.costHint" class="jev-panel__cost">{{ decision.costHint }}</p>
      </div>

      <details v-if="decision.evidence.length" class="jev-panel__evidence">
        <summary>{{ t('chat.jev.evidence') }}</summary>
        <ul><li v-for="(item, index) in decision.evidence" :key="index">{{ item }}</li></ul>
      </details>

      <p v-if="snapshot.blocked && !newSession" class="jev-panel__paused" role="status">{{ t(trust ? 'chat.jev.trustPausedHint' : 'chat.jev.suggestPausedHint') }}</p>
      <p v-if="error" class="jev-panel__error" role="alert">{{ t('chat.jev.updateFailed') }}</p>

      <div class="jev-panel__actions">
        <button type="button" class="jev-panel__dismiss" :disabled="choicesDisabled" @click="respond(false)">{{ t(newSession ? 'chat.jev.keepSession' : 'chat.jev.keepCurrent') }}</button>
        <button type="button" class="jev-panel__accept" :disabled="confirmDisabled" @click="respond(true)">{{ t(newSession ? 'chat.jev.acceptSession' : ensemble ? 'chat.jev.acceptEnsemble' : 'chat.jev.accept', { model: decision.target?.model || '' }) }}</button>
      </div>
      <footer class="jev-panel__footer">
        <p v-if="deadline !== undefined" class="jev-panel__countdown" role="timer" aria-live="off">{{ t(countdownKey, { seconds: countdown }) }}</p>
        <p class="jev-panel__scope">{{ t(scopeKey) }}</p>
      </footer>
    </section>
  </Transition>
</template>

<style scoped>
.jev-panel {
  width: min(calc(100% - 48px), var(--composer-col, 820px));
  max-height: min(54vh, 480px);
  overflow: auto;
  box-sizing: border-box;
  flex-shrink: 0;
  margin: 0 auto var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid color-mix(in srgb, var(--warn) 38%, var(--border));
  border-radius: var(--radius-panel);
  background: color-mix(in srgb, var(--warn) 5%, var(--bg-surface));
  box-shadow: var(--shadow-md);
  color: var(--text);
  font-size: var(--fs-sm);
  line-height: 1.55;
}
.jev-panel__grip { display: none; }
.jev-panel__header { display: flex; align-items: center; gap: var(--sp-3); margin-bottom: var(--sp-3); }
.jev-panel__avatar { display: grid; place-items: center; flex-shrink: 0; width: 40px; height: 40px; border-radius: var(--radius-full); background: color-mix(in srgb, var(--warn) 14%, var(--bg-surface)); font-size: 27px; }
.jev-panel__identity { flex: 1; min-width: 0; }
.jev-panel__identity h3 { margin: 0; font-size: var(--fs-sm); font-weight: 650; }
.jev-panel__identity p { margin: var(--sp-1) 0 0; color: var(--text-muted); font-size: var(--fs-xs); }
.jev-panel__badge { flex-shrink: 0; padding: var(--sp-1) var(--sp-2); border-radius: var(--radius-pill); background: color-mix(in srgb, var(--warn) 12%, var(--bg-surface)); color: var(--warn); font-size: var(--fs-xs); }
.jev-panel__models { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); overflow-wrap: anywhere; font-size: var(--fs-xs); }
.jev-panel__models > span { color: var(--text-muted); }
.jev-panel__models > strong { color: var(--accent); }
.jev-panel__plan { margin-top: var(--sp-2); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.jev-panel__plan p { margin: var(--sp-1) 0; }
.jev-panel__plan ul { margin: var(--sp-1) 0 var(--sp-2); padding-inline-start: var(--sp-4); }
.jev-panel__plan span { color: var(--text-muted); }
.jev-panel__handoff > strong { color: var(--accent); }
.jev-panel__reason { margin: var(--sp-2) 0; overflow-wrap: anywhere; }
.jev-panel__cost { margin: var(--sp-1) 0; color: var(--text-muted); font-size: var(--fs-xs); }
.jev-panel__evidence { margin: var(--sp-3) 0; padding: var(--sp-2) var(--sp-3); border-radius: var(--radius-card); background: color-mix(in srgb, var(--warn) 7%, var(--bg-surface)); color: var(--text-muted); font-size: var(--fs-xs); }
.jev-panel__evidence summary { cursor: pointer; }
.jev-panel__evidence ul { margin: var(--sp-2) 0 0; padding-inline-start: var(--sp-4); overflow-wrap: anywhere; }
.jev-panel__paused { margin: var(--sp-2) 0; color: var(--text-muted); font-size: var(--fs-xs); }
.jev-panel__error { margin: var(--sp-2) 0; color: var(--danger); font-size: var(--fs-xs); }
.jev-panel__actions { display: flex; gap: var(--sp-2); margin-top: var(--sp-3); }
.jev-panel__actions button { min-height: 42px; min-width: 0; padding: var(--sp-2) var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-control); cursor: pointer; font: inherit; font-weight: 600; overflow-wrap: anywhere; transition: background var(--dur-fast) var(--ease-standard); }
.jev-panel__actions button:disabled { opacity: 0.55; cursor: default; }
.jev-panel__actions button:focus-visible, .jev-panel__evidence summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.jev-panel__dismiss { flex: 1; background: color-mix(in srgb, var(--warn) 6%, var(--bg-surface)); color: var(--text); }
.jev-panel__dismiss:not(:disabled):hover { background: var(--bg-hover); }
.jev-panel__actions .jev-panel__accept { flex: 1.6; background: var(--accent); border-color: var(--accent); color: var(--accent-foreground); }
.jev-panel__accept:not(:disabled):hover { background: var(--accent-hover); }
.jev-panel__footer { margin-top: var(--sp-2); text-align: center; color: var(--text-muted); font-size: var(--fs-xs); }
.jev-panel__footer p { margin: var(--sp-1) 0 0; }
.jev-panel__countdown { font-variant-numeric: tabular-nums; }
.jev-suggestion-enter-active { transition: opacity var(--dur-enter) var(--ease-out), transform var(--dur-enter) var(--ease-out); }
.jev-suggestion-leave-active { transition: opacity var(--dur-fast) var(--ease-out); }
.jev-suggestion-enter-from { opacity: 0; transform: translateY(8px); }
.jev-suggestion-leave-to { opacity: 0; }
@media (max-width: 600px) {
  .jev-panel { width: calc(100% - 24px); max-height: 48dvh; border-radius: var(--radius-modal) var(--radius-modal) var(--radius-card) var(--radius-card); padding: var(--sp-2) var(--sp-3) var(--sp-3); }
  .jev-panel__grip { display: block; width: 36px; height: 4px; margin: 0 auto var(--sp-3); border-radius: var(--radius-pill); background: var(--border); }
  .jev-panel__header { gap: var(--sp-2); }
  .jev-panel__actions button { min-height: 44px; font-size: var(--fs-xs); }
}
@media (prefers-reduced-motion: reduce) {
  .jev-suggestion-enter-active, .jev-suggestion-leave-active, .jev-panel__actions button { transition: none; }
  .jev-suggestion-enter-from { transform: none; }
}
</style>
