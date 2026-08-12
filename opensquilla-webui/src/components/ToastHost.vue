<template>
  <TransitionGroup
    name="toast-stack"
    tag="div"
    class="toast-host"
    aria-live="polite"
    data-testid="toast-host"
  >
    <div
      v-for="toast in toasts"
      :key="toast.id"
      class="toast"
      :class="`toast--${toast.tone}`"
      data-testid="toast"
    >
      <span class="toast__message">{{ toast.message }}</span>
      <button
        v-if="toast.action"
        type="button"
        class="toast__action"
        @click="runAction(toast)"
      >
        {{ toast.action.label }}
      </button>
      <button
        type="button"
        class="toast__dismiss"
        :aria-label="t('shared.toast.dismiss')"
        @click="dismissToast(toast.id)"
      >
        <Icon name="x" :size="14" />
      </button>
    </div>
  </TransitionGroup>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useToasts, type ToastItem } from '@/composables/useToasts'

const { t } = useI18n()
const { toasts, dismissToast } = useToasts()

function runAction(toast: ToastItem) {
  try {
    toast.action?.onClick()
  } finally {
    dismissToast(toast.id)
  }
}
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
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: 360px;
  padding: 11px 12px 11px 14px;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--border-strong) 72%, transparent);
  border-radius: var(--radius-card);
  background: color-mix(in srgb, var(--bg-elevated) 92%, transparent);
  color: var(--text);
  font-size: var(--fs-sm);
  box-shadow: var(--elev-3);
  backdrop-filter: blur(18px) saturate(1.1);
  -webkit-backdrop-filter: blur(18px) saturate(1.1);
  pointer-events: auto;
}

.toast::before {
  align-self: stretch;
  width: 3px;
  border-radius: var(--radius-pill);
  background: var(--accent);
  content: '';
  flex: 0 0 auto;
}

.toast--ok {
  border-color: color-mix(in srgb, var(--ok) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--ok) 10%, var(--bg-elevated));
}

.toast--ok::before {
  background: var(--ok);
}

.toast--warn {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-elevated));
}

.toast--warn::before {
  background: var(--warn);
}

.toast--danger {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-elevated));
}

.toast--danger::before {
  background: var(--danger);
}

.toast__message {
  flex: 1 1 auto;
  min-width: 0;
  overflow-wrap: anywhere;
}

.toast__action {
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: inherit;
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  font-weight: 650;
  padding: var(--sp-1) var(--sp-2);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.toast__action:hover {
  background: var(--bg-hover);
}

.toast__action:focus-visible {
  box-shadow: var(--focus-ring);
  outline: none;
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

.toast-stack-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-spring);
}

.toast-stack-leave-active {
  position: absolute;
  right: 0;
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.toast-stack-move {
  transition: transform var(--dur-base) var(--ease-out);
}

.toast-stack-enter-from {
  opacity: 0;
  transform: translateY(8px) scale(0.97);
}

.toast-stack-leave-to {
  opacity: 0;
  transform: translateX(12px) scale(0.98);
}

@media (prefers-reduced-motion: reduce) {
  .toast-stack-enter-active,
  .toast-stack-leave-active,
  .toast-stack-move {
    transition: none;
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
