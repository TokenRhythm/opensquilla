<template>
  <div class="support-bundle">
    <button
      ref="triggerRef"
      type="button"
      class="btn btn--primary support-bundle__download"
      :title="t('monitorSupport.downloadBundleDescription')"
      :disabled="!canDownload || bundleInFlight"
      :aria-describedby="unavailableReason ? 'support-bundle-unavailable' : undefined"
      data-testid="support-download-bundle"
      @click="openBundleDialog"
    >
      <Icon name="download" :size="16" />
      <span>{{ t('monitorSupport.downloadBundle') }}</span>
    </button>
    <p v-if="unavailableReason" id="support-bundle-unavailable" class="support-bundle__hint">
      {{ unavailableReason }}
    </p>

    <DiagnosticsBundleDialog
      :open="bundleDialogOpen"
      :busy="bundleInFlight"
      @close="closeBundleDialog"
      @confirm="downloadBundle"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import DiagnosticsBundleDialog from '@/components/DiagnosticsBundleDialog.vue'
import Icon from '@/components/Icon.vue'
import { useToasts } from '@/composables/useToasts'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { downloadBlob } from '@/utils/browser'

const { t } = useI18n()
const injectedObservability = inject(OBSERVABILITY_KEY)
if (!injectedObservability) throw new Error('Observability was not provided')
const observability = injectedObservability
const injectedGatewayAccess = inject(GATEWAY_ACCESS_KEY)
if (!injectedGatewayAccess) throw new Error('GatewayAccess was not provided')
const gatewayAccess = injectedGatewayAccess
const { pushToast } = useToasts()
const triggerRef = ref<HTMLButtonElement | null>(null)
const bundleDialogOpen = ref(false)
const bundleInFlight = ref(false)
let downloadController: AbortController | null = null
let disposed = false

const canDownload = computed(() => gatewayAccess.supportBundleUnavailableReason === null)
const unavailableReason = computed(() => {
  if (gatewayAccess.supportBundleUnavailableReason === 'disconnected') {
    return t('monitorSupport.bundleConnectionRequired')
  }
  if (gatewayAccess.supportBundleUnavailableReason === 'permission') {
    return t('monitorSupport.bundleOwnerRequired')
  }
  if (gatewayAccess.supportBundleUnavailableReason === 'differentGateway') {
    return t('monitorSupport.bundleDifferentGateway')
  }
  return ''
})

function restoreFocus() {
  void nextTick(() => {
    if (!disposed) triggerRef.value?.focus()
  })
}

function openBundleDialog() {
  if (!canDownload.value || bundleInFlight.value) return
  bundleDialogOpen.value = true
}

function closeBundleDialog() {
  bundleDialogOpen.value = false
  restoreFocus()
}

async function downloadBundle(options: { includeContent: boolean }) {
  if (!canDownload.value || bundleInFlight.value) return
  bundleDialogOpen.value = false
  bundleInFlight.value = true
  const expectedEpoch = gatewayAccess.subscriptionEpoch
  const controller = new AbortController()
  downloadController = controller
  try {
    const bundle = await observability.downloadSupportBundle({
      includeContent: options.includeContent,
      days: 1,
      signal: controller.signal,
    })
    if (controller.signal.aborted || disposed || !canDownload.value
      || gatewayAccess.subscriptionEpoch !== expectedEpoch) return
    downloadBlob(bundle.blob, bundle.filename)
    pushToast(t('monitorSupport.bundleReady'), { tone: 'ok' })
  } catch {
    if (!controller.signal.aborted && !disposed) {
      pushToast(t('monitorSupport.bundleFailed'), { tone: 'danger' })
    }
  } finally {
    if (downloadController === controller) downloadController = null
    bundleInFlight.value = false
    restoreFocus()
  }
}

// A confirmation belongs to the connection on which it was opened. Do not
// download a stale result after reconnecting, changing authority, or closing Settings.
watch([canDownload, () => gatewayAccess.subscriptionEpoch], () => {
  bundleDialogOpen.value = false
  downloadController?.abort()
})

onBeforeUnmount(() => {
  disposed = true
  downloadController?.abort()
})
</script>

<style scoped>
.support-bundle__download {
  min-height: 36px;
  white-space: normal;
}

.support-bundle__hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: var(--sp-2) 0 0;
}

</style>
