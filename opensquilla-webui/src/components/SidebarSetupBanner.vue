<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from './Icon.vue'
import { useRpcCall } from '@/composables/useRpc'
import {
  onReadinessInvalidated,
  useReadinessSummary,
  type ReadinessStatus,
} from '@/composables/setup/useReadinessSummary'

const { t } = useI18n()
const router = useRouter()
const { data: status, execute } = useRpcCall<ReadinessStatus>('onboarding.status')
const { needsAction, actionCount } = useReadinessSummary(status)

// This banner outlives the Settings dialog (it is mounted once in App.vue), so
// its status snapshot goes stale the moment a save hot-applies config. Re-fetch
// whenever a save signals, otherwise "Setup needed" survives a completed setup
// until the next full page reload.
const stopReadinessSync = onReadinessInvalidated(() => {
  execute().catch(() => { /* error already captured in the rpc-call state */ })
})
onUnmounted(stopReadinessSync)

// Per-session dismissal that re-arms when the readiness signal changes.
const dismissedSignature = ref<string | null>(null)
const signature = computed(() => JSON.stringify({
  n: status.value?.needsOnboarding,
  s: status.value?.llmSource,
  d: status.value?.sectionDetails,
}))
const visible = computed(() => needsAction.value && dismissedSignature.value !== signature.value)

function dismiss() { dismissedSignature.value = signature.value }
// Lands on the first not-ready section of the Settings dialog.
function openSetup() { router.push('/settings/auto') }
// Inline readiness report.
function openReadiness() { router.push('/overview') }
</script>

<template>
  <section
    v-if="visible"
    class="sidebar-setup-banner"
    role="status"
    aria-live="polite"
    :aria-label="t('shared.setupBanner.ariaLabel')"
  >
    <div class="sidebar-setup-banner__row">
      <Icon class="sidebar-setup-banner__icon" name="info" :size="15" aria-hidden="true" />
      <span class="sidebar-setup-banner__text">
        {{ t('shared.setupBanner.title') }}<span v-if="actionCount > 1"> ({{ actionCount }})</span>
      </span>
      <button
        type="button"
        class="sidebar-setup-banner__dismiss"
        :title="t('shared.setupBanner.dismiss')"
        :aria-label="t('shared.setupBanner.dismissNotice')"
        @click="dismiss"
      >
        <Icon name="x" :size="13" aria-hidden="true" />
      </button>
    </div>
    <p class="sidebar-setup-banner__hint">{{ t('shared.setupBanner.hint') }}</p>
    <div class="sidebar-setup-banner__actions">
      <button type="button" class="sidebar-setup-banner__cta" @click="openSetup">
        {{ t('shared.setupBanner.finishSetup') }}
      </button>
      <button type="button" class="sidebar-setup-banner__link" @click="openReadiness">
        {{ t('shared.setupBanner.viewReadiness') }}
      </button>
    </div>
  </section>

</template>
