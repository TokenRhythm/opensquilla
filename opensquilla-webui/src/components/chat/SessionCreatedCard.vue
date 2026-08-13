<template>
  <div
    class="session-created-card"
    data-testid="session-created-card"
    :data-session-key="sessionKey"
  >
    <span class="session-created-card__identity">
      <Icon name="chat" :size="17" />
      <span>{{ t('chat.sessionCreated.title') }}</span>
    </span>
    <button
      type="button"
      class="session-created-card__open"
      :aria-label="t('chat.sessionCreated.open')"
      @click="$emit('open', sessionKey)"
    >
      {{ t('chat.sessionCreated.open') }}
      <Icon name="chevronRight" :size="15" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

defineProps<{
  sessionKey: string
}>()

defineEmits<{
  open: [sessionKey: string]
}>()

const { t } = useI18n()
</script>

<style scoped>
.session-created-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  gap: var(--sp-3);
  margin: var(--sp-2) 0;
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
}

.session-created-card__identity,
.session-created-card__open {
  display: inline-flex;
  align-items: center;
}

.session-created-card__identity {
  min-width: 0;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.session-created-card__identity :deep(.icon) {
  color: var(--text-muted);
}

.session-created-card__open {
  flex: 0 0 auto;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
}

.session-created-card__open:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.session-created-card__open:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

@media (max-width: 540px) {
  .session-created-card {
    padding: var(--sp-3);
  }
}
</style>
