<template>
  <div class="toast-host" aria-live="polite" data-testid="toast-host">
    <div
      v-for="toast in toasts"
      :key="toast.id"
      class="toast"
      :class="`toast--${toast.tone}`"
      data-testid="toast"
    >
      <span class="toast__message">{{ toast.message }}</span>
      <button
        type="button"
        class="toast__dismiss"
        :aria-label="t('shared.toast.dismiss')"
        @click="dismissToast(toast.id)"
      >
        <Icon name="x" :size="14" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useToasts } from '@/composables/useToasts'

const { t } = useI18n()
const { toasts, dismissToast } = useToasts()
</script>

<style scoped>
.toast-host {
  position: fixed;
  right: var(--sp-4);
  bottom: var(--sp-4);
  z-index: 1000;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: 360px;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text);
  font-size: var(--fs-sm);
  box-shadow: var(--shadow-md);
  pointer-events: auto;
  animation: toast-in var(--dur-fast) var(--ease-out);
}

.toast--ok {
  border-color: color-mix(in srgb, var(--ok) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--ok) 10%, var(--bg-elevated));
}

.toast--warn {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-elevated));
}

.toast--danger {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-elevated));
}

.toast__message {
  min-width: 0;
  overflow-wrap: anywhere;
}

.toast__dismiss {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: var(--sp-1);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
}

.toast__dismiss:hover {
  color: var(--text);
  background: var(--bg-hover);
}

.toast__dismiss:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .toast {
    animation: none;
  }
}

@media (max-width: 768px) {
  .toast-host {
    left: var(--sp-4);
    align-items: stretch;
  }

  .toast {
    max-width: none;
  }
}
</style>
