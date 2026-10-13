<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { DeliverySnapshot } from '@/modules/delivery'

defineProps<{
  entries: readonly DeliverySnapshot[]
  checkingIds: readonly string[]
}>()
const emit = defineEmits<{ check: [requestId: string] }>()
const { t } = useI18n()
</script>

<template>
  <ul v-if="entries.length" class="chat-delivery-recovery" :aria-label="t('deliveryRecovery.sessionTitle')" data-testid="chat-delivery-recovery">
    <li v-for="(entry, index) in entries" :key="entry.id" class="chat-delivery-recovery__item">
      <div class="chat-delivery-recovery__copy" role="status" aria-live="polite">
        <strong>{{ t('deliveryRecovery.sessionTitle') }}</strong>
        <span class="chat-delivery-recovery__preview">{{ entry.preview || t('deliveryRecovery.messageNumber', { number: index + 1 }) }}</span>
        <span>{{ t(`deliveryRecovery.reason.${entry.waitReason || (entry.stopPending ? 'stop' : 'pending')}`) }}</span>
      </div>
      <button
        type="button"
        class="btn btn--ghost chat-delivery-recovery__check"
        data-testid="chat-delivery-recovery-check"
        :disabled="checkingIds.includes(entry.id)"
        :aria-busy="checkingIds.includes(entry.id) || undefined"
        @click="emit('check', entry.id)"
      >
        {{ t(checkingIds.includes(entry.id) ? 'deliveryRecovery.checking' : 'deliveryRecovery.checkStatus') }}
      </button>
    </li>
  </ul>
</template>

<style scoped>
.chat-delivery-recovery {
  align-self: center;
  box-sizing: border-box;
  flex: 0 0 auto;
  list-style: none;
  margin: var(--sp-2) auto;
  max-width: min(calc(100% - 32px), 680px);
  padding: 0;
  width: 100%;
}

.chat-delivery-recovery__item {
  align-items: center;
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--border));
  border-radius: var(--radius-lg);
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
}

.chat-delivery-recovery__item + .chat-delivery-recovery__item {
  margin-top: var(--sp-2);
}

.chat-delivery-recovery__copy {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.chat-delivery-recovery__copy strong { color: var(--text); }
.chat-delivery-recovery__preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-delivery-recovery__check { flex: 0 0 auto; min-height: 32px; }

@media (max-width: 600px) {
  .chat-delivery-recovery__item { align-items: flex-start; flex-wrap: wrap; }
}
</style>
