<script setup lang="ts">
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import { optionalSessionRpcAllowed } from '@/composables/chat/sessionBootstrapAdmission'
import { useSetupStatus } from '@/composables/setup/useSetupStatus'
import { SETUP_WORKFLOW_KEY } from '@/modules/setupWorkflow'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'

const { t } = useI18n()
const router = useRouter()
const setupWorkflow = inject(SETUP_WORKFLOW_KEY)
if (!setupWorkflow) throw new Error('SetupWorkflow was not provided')
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const statusReadAllowed = computed(() => optionalSessionRpcAllowed.value && gateway?.isAvailable !== false)

// This is the Gateway's local configuration state, not a connection test.
// Unknown/loading/error states must not turn into a new startup or send gate.
const { data: status } = useSetupStatus<{ llmConfigured?: boolean }>(setupWorkflow, {
  allowed: statusReadAllowed,
})

function openSettings() {
  // The existing settings overlay retains the live composer and attachments.
  void router.push('/settings/provider').catch(() => {})
}
</script>

<template>
  <div
    v-if="status?.llmConfigured === false"
    class="chat-model-setup-notice"
    role="status"
    aria-live="polite"
  >
    <Icon name="info" :size="16" aria-hidden="true" />
    <span class="chat-model-setup-notice__copy">{{ t('chat.modelSetup.notice') }}</span>
    <button type="button" class="btn btn--ghost" @click="openSettings">
      {{ t('chat.modelSetup.configure') }}
    </button>
  </div>
</template>

<style scoped>
.chat-model-setup-notice {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
  width: min(calc(100% - 48px), var(--composer-col, 820px));
  margin: 0 auto var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.chat-model-setup-notice__copy { flex: 1; min-width: 0; }
.chat-model-setup-notice .btn { flex-shrink: 0; }
</style>
