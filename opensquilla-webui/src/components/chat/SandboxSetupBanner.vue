<template>
  <aside v-if="status" class="sandbox-setup" :class="`sandbox-setup--${status.state}`" role="status">
    <Icon name="info" :size="16" aria-hidden="true" />
    <div class="sandbox-setup__body">
      <strong class="sandbox-setup__title">{{ title }}</strong>
      <p class="sandbox-setup__message">{{ message }}</p>
      <div
        v-if="status.state === 'setting_up'"
        class="sandbox-setup__progress"
        role="progressbar"
        :aria-label="t('chat.sandboxSetup.settingUp')"
      ><span /></div>
      <p v-if="error" class="sandbox-setup__error" role="alert">{{ error }}</p>
    </div>
    <div class="sandbox-setup__actions">
      <button
        v-if="canSetup"
        type="button"
        class="btn btn--primary"
        :disabled="ensuring"
        @click="emit('setup')"
      >{{ ensuring ? t('chat.sandboxSetup.settingUp') : status.state === 'failed' ? t('chat.sandboxSetup.retry') : t('chat.sandboxSetup.setup') }}</button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('common.dismiss')"
        :title="t('common.dismiss')"
        @click="emit('dismiss')"
      ><Icon name="x" :size="14" /></button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { SandboxSetupStatusPayload } from '@/types/sandbox'

const props = defineProps<{
  status: SandboxSetupStatusPayload | null
  canSetup: boolean
  ensuring: boolean
  error?: string
}>()

const emit = defineEmits<{ setup: []; dismiss: [] }>()
const { t } = useI18n()

const title = computed(() => {
  if (props.status?.state === 'setting_up') return t('chat.sandboxSetup.settingUpTitle')
  if (props.status?.state === 'failed') return t('chat.sandboxSetup.failedTitle')
  if (props.status?.state === 'unavailable') return t('chat.sandboxSetup.unavailableTitle')
  return t('chat.sandboxSetup.requiredTitle')
})

const message = computed(() => {
  if (props.status?.state === 'setting_up') return t('chat.sandboxSetup.settingUpBody')
  if (props.status?.state === 'failed') return t('chat.sandboxSetup.failedBody')
  if (props.status?.state === 'unavailable') return t('chat.sandboxSetup.unavailableBody')
  return t('chat.sandboxSetup.requiredBody')
})
</script>

<style scoped>
.sandbox-setup {
  align-items: flex-start;
  background: var(--bg-surface);
  border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--border));
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  gap: var(--sp-3);
  margin: var(--sp-2) auto;
  padding: var(--sp-3);
  width: var(--chat-col, min(calc(100% - 48px), 980px));
}

.sandbox-setup__body { flex: 1; min-width: 0; }
.sandbox-setup__title { color: var(--text); font-size: var(--fs-sm); }
.sandbox-setup__message,
.sandbox-setup__error { font-size: var(--fs-xs); margin: var(--sp-1) 0 0; }
.sandbox-setup__error { color: var(--danger); }
.sandbox-setup__actions { align-items: center; display: flex; gap: var(--sp-2); }
.sandbox-setup__progress { background: var(--bg-elevated); height: 3px; margin-top: var(--sp-2); overflow: hidden; }
.sandbox-setup__progress span { animation: sandbox-progress 1.2s var(--ease-out) infinite; background: var(--accent); display: block; height: 100%; width: 40%; }

@keyframes sandbox-progress {
  from { transform: translateX(-100%); }
  to { transform: translateX(350%); }
}

@media (prefers-reduced-motion: reduce) {
  .sandbox-setup__progress span { animation: none; width: 100%; }
}
</style>
