<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { downloadBlob } from '@/utils/browser'

const { t } = useI18n()
const { pushToast } = useToasts()
const injectedGatewayAccess = inject(GATEWAY_ACCESS_KEY)
if (!injectedGatewayAccess) throw new Error('GatewayAccess was not provided')
const gatewayAccess = injectedGatewayAccess
const injectedObservability = inject(OBSERVABILITY_KEY)
if (!injectedObservability) throw new Error('Observability was not provided')
const observability = injectedObservability

const entryRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const dialogRef = ref<HTMLElement | null>(null)
const closeRef = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const loading = ref(false)
const logText = ref('')
const loadedEpoch = ref<number | null>(null)
const failed = ref(false)
let request: AbortController | null = null
let disposed = false

// Log reads use the connected RPC socket, including authenticated remote
// Gateways. They do not inherit the support ZIP's same-origin HTTP restriction.
const connected = computed(() => gatewayAccess.isAvailable
  && gatewayAccess.connectionHealth === 'healthy'
  && (!gatewayAccess.connectionPhase || gatewayAccess.connectionPhase === 'healthy')
  && !gatewayAccess.isResuming)
const canRead = computed(() => connected.value
  && (gatewayAccess.isAuthenticated || gatewayAccess.isLocalOwner))
const canSave = computed(() => open.value && canRead.value && !loading.value && !failed.value
  && logText.value.length > 0 && loadedEpoch.value === gatewayAccess.subscriptionEpoch)
const unavailableReason = computed(() => !connected.value
  ? t('gatewayLogs.connectionRequired')
  : !canRead.value ? t('gatewayLogs.authenticationRequired') : '')

function clearRequest() {
  request?.abort()
  request = null
  loading.value = false
}

function close() {
  open.value = false
  clearRequest()
  logText.value = ''
  loadedEpoch.value = null
  failed.value = false
  void nextTick(() => {
    if (disposed) return
    if (triggerRef.value && !triggerRef.value.disabled) triggerRef.value.focus()
    else entryRef.value?.focus()
  })
}

async function readLogs() {
  if (!open.value || !canRead.value || loading.value) return
  const controller = new AbortController()
  const expectedEpoch = gatewayAccess.subscriptionEpoch
  request = controller
  loading.value = true
  failed.value = false
  logText.value = ''
  loadedEpoch.value = null
  const isCurrent = () => !disposed && open.value && canRead.value
    && request === controller && !controller.signal.aborted
    && gatewayAccess.subscriptionEpoch === expectedEpoch
  try {
    const result = await observability.tailLogs({ signal: controller.signal })
    if (!isCurrent()) return
    logText.value = result.entries.map(entry => typeof entry === 'string'
      ? entry : JSON.stringify(entry)).join('\n')
    loadedEpoch.value = expectedEpoch
  } catch {
    if (isCurrent()) failed.value = true
  } finally {
    if (request === controller) {
      request = null
      loading.value = false
    }
  }
}

function showLogs() {
  if (!canRead.value || open.value) return
  open.value = true
  void readLogs()
}

function saveCurrentLogs() {
  if (disposed || !canSave.value) return
  try {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
    downloadBlob(new Blob([logText.value], { type: 'text/plain;charset=utf-8' }), `gateway-logs-${timestamp}.txt`)
  } catch {
    pushToast(t('gatewayLogs.saveFailed'), { tone: 'danger' })
  }
}

useDialogA11y(dialogRef, open, close, { initialFocus: closeRef })

watch([canRead, () => gatewayAccess.subscriptionEpoch], () => {
  if (open.value) close()
})

onBeforeUnmount(() => {
  disposed = true
  clearRequest()
})
</script>

<template>
  <div id="settings-gateway-logs" ref="entryRef" class="gateway-logs" tabindex="-1">
    <button
      ref="triggerRef"
      type="button"
      class="btn"
      data-testid="support-view-logs"
      :disabled="!canRead"
      :aria-describedby="unavailableReason ? 'gateway-logs-unavailable' : undefined"
      @click="showLogs"
    >
      <Icon name="logs" :size="16" />
      {{ t('gatewayLogs.viewLogs') }}
    </button>
    <p v-if="unavailableReason" id="gateway-logs-unavailable" class="gateway-logs__hint">{{ unavailableReason }}</p>
    <Teleport to="body">
      <div v-if="open" class="gateway-logs__overlay" @click.self="close">
        <section
          ref="dialogRef"
          class="gateway-logs__dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="gateway-logs-title"
          aria-describedby="gateway-logs-description"
        >
          <header class="gateway-logs__header">
            <div class="gateway-logs__heading">
              <h3 id="gateway-logs-title">{{ t('gatewayLogs.title') }}</h3>
              <p id="gateway-logs-description">{{ t('gatewayLogs.description') }}</p>
            </div>
            <button
              ref="closeRef"
              type="button"
              class="btn btn--icon btn--ghost"
              :aria-label="t('common.close')"
              @click="close"
            ><Icon name="x" :size="16" /></button>
          </header>
          <div class="gateway-logs__body" :aria-busy="loading">
            <p v-if="loading" class="gateway-logs__message" role="status">{{ t('gatewayLogs.loading') }}</p>
            <p v-else-if="failed" class="gateway-logs__message gateway-logs__error" role="alert">{{ t('gatewayLogs.failed') }}</p>
            <pre v-else-if="logText" class="gateway-logs__text" tabindex="0" :aria-label="t('gatewayLogs.title')">{{ logText }}</pre>
            <p v-else class="gateway-logs__message" role="status">{{ t('gatewayLogs.empty') }}</p>
          </div>
          <footer class="gateway-logs__footer">
            <button type="button" class="btn" :disabled="!canSave" data-testid="gateway-logs-save" @click="saveCurrentLogs">
              <Icon name="download" :size="16" />
              {{ t('gatewayLogs.saveCurrentLogs') }}
            </button>
            <button type="button" class="btn" :disabled="loading" data-testid="gateway-logs-refresh" @click="readLogs">
              <Icon name="refresh" :size="16" />
              {{ t('gatewayLogs.refresh') }}
            </button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.gateway-logs { align-items: flex-start; display: inline-flex; flex-direction: column; min-width: 0; }
.gateway-logs:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
.gateway-logs > .btn { white-space: nowrap; }
.gateway-logs__hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: var(--sp-2) 0 0;
  max-width: 30ch;
}

.gateway-logs__overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-4);
  position: fixed;
  z-index: 1100;
}

.gateway-logs__dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  max-height: calc(100dvh - (2 * var(--sp-4)));
  max-width: 880px;
  overflow: hidden;
  width: 100%;
}

.gateway-logs__header {
  align-items: flex-start;
  border-bottom: 1px solid var(--hairline);
  display: flex;
  flex-shrink: 0;
  gap: var(--sp-3);
  padding: var(--sp-4);
}

.gateway-logs__heading { flex: 1; min-width: 0; }
.gateway-logs__heading h3 { font-size: var(--fs-md); margin: 0; }
.gateway-logs__heading p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: var(--sp-1) 0 0;
}

.gateway-logs__body { flex: 1 1 auto; max-height: 60dvh; min-height: 0; min-width: 0; overflow: auto; }
.gateway-logs__message { color: var(--text-muted); font-size: var(--fs-sm); margin: var(--sp-4); }
.gateway-logs__error { color: var(--danger); }
.gateway-logs__text {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.6;
  margin: 0;
  padding: var(--sp-4);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.gateway-logs__footer {
  border-top: 1px solid var(--hairline);
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
  padding: var(--sp-3) var(--sp-4);
}
</style>
