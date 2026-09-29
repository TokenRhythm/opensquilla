<script setup lang="ts">
import { inject, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import TraceTimeline from '@/components/trace/TraceTimeline.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import type { TraceDetails, TraceProjection } from '@/types/traceView'

const { t } = useI18n()
const props = defineProps<{ initialTraceId?: string }>()
const injectedObservability = inject(OBSERVABILITY_KEY)
if (!injectedObservability) throw new Error('Observability was not provided')
const observability = injectedObservability
const gatewayAccess = inject(GATEWAY_ACCESS_KEY)

const open = ref(false)
const dialogRef = ref<HTMLElement | null>(null)
const closeRef = ref<HTMLButtonElement | null>(null)
const traceId = ref('')
const projection = ref<TraceProjection | null>(null)
const details = ref<TraceDetails | null>(null)
const loading = ref(false)
const error = ref('')
let request: AbortController | null = null

function close() {
  request?.abort()
  request = null
  loading.value = false
  open.value = false
  projection.value = null
  details.value = null
  error.value = ''
}

function show() {
  open.value = true
}

async function inspect() {
  const id = traceId.value.trim()
  if (!id) return
  request?.abort()
  const controller = new AbortController()
  request = controller
  projection.value = null
  details.value = null
  error.value = ''
  loading.value = true
  try {
    const result = await observability.traceProjection(id, { signal: controller.signal })
    if (request !== controller || !open.value) return
    if (!result || !result.spans.length) {
      error.value = t('usageLogs.logs.traceNotFound')
      return
    }
    projection.value = result
    try {
      details.value = await observability.traceDetails(id, { signal: controller.signal, limit: 1000 })
    } catch (cause) {
      if (request !== controller || !open.value) return
      const code = cause && typeof cause === 'object' && 'code' in cause ? cause.code : null
      details.value = code === 'UNAUTHORIZED' || code === 'FORBIDDEN' || code === 'PERMISSION_DENIED'
        ? { traceId: id, available: false, reason: 'access_denied', rows: [], count: 0, total: 0 }
        : null
    }
  } catch {
    if (request === controller && open.value && !controller.signal.aborted) {
      error.value = t('usageLogs.logs.traceLoadFailed')
    }
  } finally {
    if (request === controller) {
      request = null
      loading.value = false
    }
  }
}

useDialogA11y(dialogRef, open, close, { initialFocus: closeRef })
watch([() => gatewayAccess?.subscriptionEpoch, () => gatewayAccess?.availability], close)
watch(() => props.initialTraceId, value => {
  const id = value?.trim() || ''
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(id)) return
  traceId.value = id
  show()
  void inspect()
}, { immediate: true })
onBeforeUnmount(close)
</script>

<template>
  <button type="button" class="btn" @click="show">
    <Icon name="router" :size="16" />
    {{ t('usageLogs.logs.traceInspector') }}
  </button>
  <Teleport to="body">
    <div v-if="open" class="trace-inspector-dialog__overlay" @click.self="close">
      <section
        ref="dialogRef"
        class="trace-inspector-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('usageLogs.logs.traceInspector')"
      >
        <header class="trace-inspector-dialog__header">
          <h3>{{ t('usageLogs.logs.traceInspector') }}</h3>
          <button ref="closeRef" type="button" class="btn btn--icon btn--ghost" :aria-label="t('common.close')" @click="close">
            <Icon name="x" :size="16" />
          </button>
        </header>
        <form class="trace-inspector-dialog__form" @submit.prevent="inspect">
          <input v-model="traceId" type="text" class="control-input" :aria-label="t('usageLogs.logs.traceIdPlaceholder')" :placeholder="t('usageLogs.logs.traceIdPlaceholder')" autocomplete="off" spellcheck="false" />
          <button class="btn btn--primary" type="submit" :disabled="loading || !traceId.trim()">
            {{ loading ? t('usageLogs.logs.traceLoading') : t('usageLogs.logs.traceInspect') }}
          </button>
        </form>
        <div class="trace-inspector-dialog__body" :aria-busy="loading">
          <p v-if="error" role="alert" class="trace-inspector-dialog__message">{{ error }}</p>
          <TraceTimeline
            v-if="projection"
            :projection="projection"
            :details="details?.rows"
            :details-available="details?.available"
            :details-reason="details?.reason"
            :clock-origin="details?.clockOrigin"
          />
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.trace-inspector-dialog__overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-4);
  position: fixed;
  z-index: 1100;
}
.trace-inspector-dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  max-height: calc(100dvh - 2 * var(--sp-4));
  max-width: 1100px;
  min-width: 0;
  overflow: hidden;
  width: 100%;
}
.trace-inspector-dialog__header,
.trace-inspector-dialog__form {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
}
.trace-inspector-dialog__header {
  border-bottom: 1px solid var(--hairline);
  justify-content: space-between;
}
.trace-inspector-dialog__header h3 { font-size: var(--fs-md); margin: 0; }
.trace-inspector-dialog__form { flex-wrap: wrap; }
.trace-inspector-dialog__form input { flex: 1 1 220px; min-width: 0; }
.trace-inspector-dialog__body { overflow: auto; padding: 0 var(--sp-4) var(--sp-4); }
.trace-inspector-dialog__message { color: var(--danger); font-size: var(--fs-sm); }
</style>
