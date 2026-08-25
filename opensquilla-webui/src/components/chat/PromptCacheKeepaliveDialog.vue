<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="keepalive-overlay" @click="close">
        <section
          ref="dialogRef"
          class="keepalive-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="prompt-cache-keepalive-title"
          @click.stop
        >
          <header class="keepalive-dialog__header">
            <h2 id="prompt-cache-keepalive-title">{{ t('chat.promptCacheKeepalive.title') }}</h2>
            <button
              ref="closeButtonRef"
              type="button"
              class="keepalive-dialog__close"
              :aria-label="t('common.close')"
              @click="close"
            >
              <Icon name="x" :size="18" />
            </button>
          </header>

          <div class="keepalive-dialog__body">
            <p v-if="loading" class="keepalive-dialog__muted">{{ t('chat.loadingSession') }}</p>
            <template v-else>
              <label class="keepalive-dialog__toggle">
                <span>
                  <strong>{{ t('chat.promptCacheKeepalive.enable') }}</strong>
                  <small>{{ t('chat.promptCacheKeepalive.enableHint') }}</small>
                </span>
                <ControlSwitch
                  v-model:checked="draftEnabled"
                  name="prompt_cache_keepalive_enabled"
                  :aria-label="t('chat.promptCacheKeepalive.enable')"
                />
              </label>

              <div class="keepalive-dialog__timing" :class="{ 'is-disabled': !draftEnabled }">
                <div class="keepalive-dialog__field">
                  <span class="keepalive-dialog__field-label">
                    <label for="prompt-cache-keepalive-ttl">
                      <strong>{{ t('chat.promptCacheKeepalive.ttlMinutes') }}</strong>
                    </label>
                    <button
                      type="button"
                      class="keepalive-dialog__field-help"
                      :aria-label="t('chat.promptCacheKeepalive.ttlHint')"
                      aria-describedby="prompt-cache-keepalive-ttl-tip"
                    >
                      <Icon name="info" :size="14" aria-hidden="true" />
                      <span
                        id="prompt-cache-keepalive-ttl-tip"
                        class="keepalive-dialog__field-tooltip"
                        role="tooltip"
                      >
                        {{ t('chat.promptCacheKeepalive.ttlHint') }}
                      </span>
                    </button>
                  </span>
                  <span class="keepalive-dialog__input-wrap">
                    <input
                      id="prompt-cache-keepalive-ttl"
                      v-model.number="draftTtlMinutes"
                      data-testid="prompt-cache-keepalive-ttl"
                      type="number"
                      min="5"
                      max="1440"
                      step="1"
                      :disabled="!draftEnabled"
                    />
                    <span>{{ t('chat.promptCacheKeepalive.minutesUnit') }}</span>
                  </span>
                </div>

                <div class="keepalive-dialog__field">
                  <span class="keepalive-dialog__field-label">
                    <label for="prompt-cache-keepalive-idle-timeout">
                      <strong>{{ t('chat.promptCacheKeepalive.idleTimeoutMinutes') }}</strong>
                    </label>
                    <button
                      type="button"
                      class="keepalive-dialog__field-help"
                      :aria-label="t('chat.promptCacheKeepalive.idleTimeoutHint')"
                      aria-describedby="prompt-cache-keepalive-idle-tip"
                    >
                      <Icon name="info" :size="14" aria-hidden="true" />
                      <span
                        id="prompt-cache-keepalive-idle-tip"
                        class="keepalive-dialog__field-tooltip"
                        role="tooltip"
                      >
                        {{ t('chat.promptCacheKeepalive.idleTimeoutHint') }}
                      </span>
                    </button>
                  </span>
                  <span class="keepalive-dialog__input-wrap">
                    <input
                      id="prompt-cache-keepalive-idle-timeout"
                      v-model.number="draftIdleTimeoutMinutes"
                      data-testid="prompt-cache-keepalive-idle-timeout"
                      type="number"
                      min="5"
                      max="1440"
                      step="1"
                      :disabled="!draftEnabled"
                    />
                    <span>{{ t('chat.promptCacheKeepalive.minutesUnit') }}</span>
                  </span>
                </div>
              </div>

              <p v-if="draftEnabled && !validIdleTimeout" class="keepalive-dialog__validation" role="alert">
                {{ t('chat.promptCacheKeepalive.idleTimeoutInvalid', { minutes: probeMinutes }) }}
              </p>

              <p
                v-if="!draftEnabled || validConfig"
                class="keepalive-dialog__summary"
                :class="{ 'is-disabled': !draftEnabled }"
                role="note"
              >
                {{ t('chat.promptCacheKeepalive.planSummary', {
                  interval: probeMinutes,
                  duration: draftIdleTimeoutMinutes,
                  count: estimatedProbeCount,
                }) }}
              </p>

              <div v-if="draftEnabled" class="keepalive-dialog__warning" role="note">
                <Icon name="info" :size="15" />
                <span>{{ t('chat.promptCacheKeepalive.costWarning') }}</span>
              </div>

              <p
                v-if="draftEnabled && status?.enabled"
                class="keepalive-dialog__status"
                aria-live="polite"
              >
                <span>{{ statusText }}</span>
                <span v-if="status.idleExpiresAt">
                  · {{ t('chat.promptCacheKeepalive.autoPauseLabel') }}
                  {{ formatTime(status.idleExpiresAt) }}
                </span>
              </p>
            </template>
            <p v-if="error" class="keepalive-dialog__error" role="alert">{{ error }}</p>
          </div>

          <footer class="keepalive-dialog__footer">
            <button type="button" class="btn btn--ghost" :disabled="saving" @click="close">
              {{ t('common.cancel') }}
            </button>
            <button
              type="button"
              class="btn btn--primary"
              :disabled="loading || saving || !validConfig"
              @click="save"
            >
              {{ saving ? t('chat.saving') : t('common.save') }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { useRpcStore } from '@/stores/rpc'
import type {
  PromptCacheKeepaliveStatus,
  PromptCacheKeepaliveStatusUpdate,
} from '@/types/promptCacheKeepalive'

const props = defineProps<{ open: boolean; sessionKey: string }>()
const emit = defineEmits<{
  close: []
  saved: [update: PromptCacheKeepaliveStatusUpdate]
}>()
const { t } = useI18n()
const { pushToast } = useToasts()
const rpc = useRpcStore()
const dialogRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLElement | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const status = ref<PromptCacheKeepaliveStatus | null>(null)
const draftEnabled = ref(false)
const draftTtlMinutes = ref(5)
const draftIdleTimeoutMinutes = ref(60)

const validTtl = computed(() => (
  Number.isInteger(draftTtlMinutes.value)
  && draftTtlMinutes.value >= 5
  && draftTtlMinutes.value <= 1440
))
const probeMinutes = computed(() => Math.max(1, Math.round(draftTtlMinutes.value * 0.8)))
const validIdleTimeout = computed(() => (
  Number.isInteger(draftIdleTimeoutMinutes.value)
  && draftIdleTimeoutMinutes.value >= 5
  && draftIdleTimeoutMinutes.value <= 1440
  && draftIdleTimeoutMinutes.value * 60 > draftTtlMinutes.value * 60 * 0.8
))
const validConfig = computed(() => !draftEnabled.value || (
  validTtl.value && validIdleTimeout.value
))
const estimatedProbeCount = computed(() => {
  if (!validTtl.value || !Number.isFinite(draftIdleTimeoutMinutes.value)) return 0
  const intervalSeconds = draftTtlMinutes.value * 60 * 0.8
  return Math.max(0, Math.floor((draftIdleTimeoutMinutes.value * 60 - 1) / intervalSeconds))
})
const statusText = computed(() => {
  if (!status.value) return ''
  const key = `chat.promptCacheKeepalive.states.${status.value.state}`
  const translated = t(key)
  return translated === key ? status.value.state : translated
})

function close() {
  if (!saving.value) emit('close')
}

function formatTime(value: number): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

async function load() {
  if (!props.open || !props.sessionKey) return
  loading.value = true
  error.value = ''
  try {
    const next = await rpc.call<PromptCacheKeepaliveStatus>(
      'sessions.promptCacheKeepalive.status',
      { key: props.sessionKey },
    )
    status.value = next
    draftEnabled.value = next.enabled
    draftTtlMinutes.value = Math.max(5, Math.round(next.ttlSeconds / 60))
    draftIdleTimeoutMinutes.value = Math.max(
      5,
      Math.round((next.idleTimeoutSeconds || 3_600) / 60),
    )
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    error.value = /session not found/i.test(detail)
      ? t('chat.promptCacheKeepalive.sessionUnavailable')
      : detail
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!validConfig.value) return
  saving.value = true
  error.value = ''
  const savedSessionKey = props.sessionKey
  try {
    const ttlSeconds = Number.isInteger(draftTtlMinutes.value)
      ? Math.round(draftTtlMinutes.value * 60)
      : (status.value?.ttlSeconds || 300)
    const idleTimeoutSeconds = Number.isInteger(draftIdleTimeoutMinutes.value)
      ? Math.round(draftIdleTimeoutMinutes.value * 60)
      : (status.value?.idleTimeoutSeconds || 3_600)
    const next = await rpc.call<PromptCacheKeepaliveStatus>(
      'sessions.promptCacheKeepalive.set',
      {
        key: savedSessionKey,
        enabled: draftEnabled.value,
        ttlSeconds,
        idleTimeoutSeconds,
      },
    )
    status.value = next
    emit('saved', { sessionKey: savedSessionKey, status: next })
    pushToast(t(next.enabled
      ? 'chat.promptCacheKeepalive.enabledToast'
      : 'chat.promptCacheKeepalive.disabledToast'), { tone: 'ok' })
    emit('close')
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause)
    error.value = /session not found/i.test(detail)
      ? t('chat.promptCacheKeepalive.sessionUnavailable')
      : detail
  } finally {
    saving.value = false
  }
}

watch(() => [props.open, props.sessionKey] as const, ([isOpen]) => {
  if (isOpen) void nextTick(load)
}, { immediate: true })

useDialogA11y(dialogRef, computed(() => props.open), close, {
  initialFocus: closeButtonRef,
})
</script>

<style scoped>
.keepalive-overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-4);
  position: fixed;
  z-index: 1100;
}

.keepalive-dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
  max-width: 520px;
  width: 100%;
}

.keepalive-dialog__header,
.keepalive-dialog__footer {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-4) var(--sp-5);
}

.keepalive-dialog__header { border-bottom: 1px solid var(--border); }
.keepalive-dialog__footer { border-top: 1px solid var(--border); justify-content: flex-end; }
.keepalive-dialog__header h2 { font-size: var(--fs-lg); margin: 0; }
.keepalive-dialog__close { background: none; border: 0; color: var(--text-muted); cursor: pointer; padding: var(--sp-1); }
.keepalive-dialog__body { display: grid; gap: var(--sp-3); padding: var(--sp-5); }
.keepalive-dialog__toggle {
  align-items: center;
  cursor: pointer;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}
.keepalive-dialog__toggle > span { min-width: 0; }
.keepalive-dialog__toggle span,
.keepalive-dialog__toggle small { display: block; }
.keepalive-dialog__toggle small,
.keepalive-dialog__muted { color: var(--text-muted); font-size: var(--fs-xs); }
.keepalive-dialog__timing { display: grid; gap: var(--sp-3); grid-template-columns: repeat(2, minmax(0, 1fr)); }
.keepalive-dialog__summary.is-disabled { opacity: var(--state-disabled-opacity); }
.keepalive-dialog__field { display: grid; gap: var(--sp-2); min-width: 0; }
.keepalive-dialog__field-label { align-items: center; display: flex; gap: var(--sp-1); }
.keepalive-dialog__timing.is-disabled .keepalive-dialog__field-label > label,
.keepalive-dialog__timing.is-disabled .keepalive-dialog__input-wrap {
  opacity: var(--state-disabled-opacity);
}
.keepalive-dialog__field strong { font-size: var(--fs-sm); font-weight: 600; }
.keepalive-dialog__field-help {
  align-items: center;
  background: none;
  border: 0;
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  padding: 0;
  position: relative;
}
.keepalive-dialog__field-help:hover { color: var(--text); }
.keepalive-dialog__field-tooltip {
  background: var(--text);
  border-radius: var(--radius-sm);
  bottom: calc(100% + 8px);
  box-shadow: var(--shadow-md);
  color: var(--bg-elevated);
  font-size: var(--fs-xs);
  font-weight: 400;
  left: 50%;
  line-height: 1.45;
  max-width: min(280px, 70vw);
  opacity: 0;
  padding: 7px 9px;
  pointer-events: none;
  position: absolute;
  text-align: left;
  transform: translateX(-50%);
  visibility: hidden;
  white-space: normal;
  width: max-content;
  z-index: 40;
}
.keepalive-dialog__field-tooltip::after {
  border: 5px solid transparent;
  border-top-color: var(--text);
  content: '';
  left: 50%;
  position: absolute;
  top: 100%;
  transform: translateX(-50%);
}
.keepalive-dialog__field-help:hover .keepalive-dialog__field-tooltip,
.keepalive-dialog__field-help:focus .keepalive-dialog__field-tooltip {
  opacity: 1;
  visibility: visible;
}
.keepalive-dialog__field-help:focus-visible {
  border-radius: var(--radius-full);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.keepalive-dialog__field:last-child .keepalive-dialog__field-tooltip {
  left: auto;
  right: -8px;
  transform: none;
}
.keepalive-dialog__field:last-child .keepalive-dialog__field-tooltip::after {
  left: auto;
  right: 9px;
  transform: none;
}
.keepalive-dialog__input-wrap { align-items: center; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); display: flex; overflow: hidden; }
.keepalive-dialog__input-wrap input { background: transparent; border: 0; border-radius: 0; color: var(--text); min-width: 0; padding: 8px 10px; width: 100%; }
.keepalive-dialog__input-wrap input:focus { box-shadow: none; }
.keepalive-dialog__input-wrap:focus-within { border-color: var(--accent); box-shadow: var(--focus-ring); }
.keepalive-dialog__input-wrap > span { color: var(--text-dim); flex: 0 0 auto; font-size: var(--fs-xs); padding-right: var(--sp-3); }
.keepalive-dialog__summary { color: var(--text-muted); font-size: var(--fs-xs); margin: 0; }
.keepalive-dialog__warning { align-items: flex-start; color: var(--warn); display: flex; font-size: var(--fs-xs); gap: var(--sp-2); }
.keepalive-dialog__validation { color: var(--danger); font-size: var(--fs-xs); margin: calc(var(--sp-2) * -1) 0 0; }
.keepalive-dialog__status { color: var(--text-muted); font-size: var(--fs-xs); margin: 0; }
.keepalive-dialog__error { color: var(--danger); font-size: var(--fs-sm); margin: 0; }
.modal-enter-active, .modal-leave-active { transition: opacity var(--dur-base); }
.modal-enter-from, .modal-leave-to { opacity: 0; }

@media (max-width: 560px) {
  .keepalive-dialog__timing { grid-template-columns: 1fr; }
}
</style>
