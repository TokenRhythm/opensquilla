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
      :data-toast-id="toast.id"
      :aria-busy="toast.busy || undefined"
      @pointerenter="pauseToast(toast.id, 'pointer')"
      @pointerleave="resumeToast(toast.id, 'pointer')"
      @focusin="pauseToast(toast.id, 'focus')"
      @focusout="onFocusOut($event, toast.id)"
    >
      <span class="toast__message">{{ toast.message }}</span>
      <button
        v-if="toast.action"
        type="button"
        class="toast__action"
        :aria-disabled="toast.busy || undefined"
        @click="runAction($event, toast)"
      >
        {{ toast.busy ? t('shared.toast.actionPending') : toast.action.label }}
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
import { nextTick, watchPostEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useToasts, type ToastItem } from '@/composables/useToasts'

const { t } = useI18n()
const { toasts, dismissToast, pauseToast, resumeToast, runToastAction } = useToasts()

function onFocusOut(event: FocusEvent, id: number) {
  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) resumeToast(id, 'focus')
}

async function runAction(event: Event, toast: ToastItem) {
  const row = (event.currentTarget as HTMLElement).closest('.toast')
  await runToastAction(toast.id, t('shared.toast.actionFailed'))
  await nextTick()
  if (!row?.contains(document.activeElement)) resumeToast(toast.id, 'focus')
}

// Removing a focused action during a keyed update does not reliably emit focusout.
watchPostEffect(() => {
  for (const toast of toasts.value) {
    void toast.action
    const row = document.querySelector(`[data-toast-id="${toast.id}"]`)
    if (!row?.contains(document.activeElement)) resumeToast(toast.id, 'focus')
  }
})
</script>

<style scoped>
.toast-host {
  /* One viewport-owned stack for every conversation. Keep the lower half
     available to composers and Stop, including a resized workbench pane. */
  position: fixed;
  top: calc(56px + env(safe-area-inset-top, 0px));
  inset-inline: var(--sp-4);
  width: min(392px, calc(100% - 2 * var(--sp-4)));
  max-height: max(56px, calc(50dvh - 56px - env(safe-area-inset-top, 0px)));
  margin-inline: auto;
  padding: var(--sp-2);
  box-sizing: border-box;
  overflow: auto;
  overscroll-behavior: contain;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--sp-2);
  pointer-events: none;
}

.toast {
  position: relative;
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: var(--sp-2);
  padding: 11px 12px 11px 14px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  background: var(--bg-elevated);
  color: var(--text);
  font-size: var(--fs-sm);
  box-shadow: var(--shadow-md);
  pointer-events: auto;
}

.toast::before {
  align-self: stretch;
  width: 3px;
  border-radius: var(--radius-pill);
  background: var(--text-dim);
  content: '';
  flex: 0 0 auto;
}

.toast--ok::before {
  background: var(--ok);
}

.toast--warn::before {
  background: var(--warn);
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
  font-weight: 600;
  min-height: 32px;
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
  min-width: 32px;
  min-height: 32px;
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
    transform var(--dur-base) var(--ease-out);
}

.toast-stack-leave-active {
  /* Retain its slot until the fade ends so the bounded stack does not clip
     the last departing notice as soon as it leaves the queue. */
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.toast-stack-move {
  transition: transform var(--dur-base) var(--ease-out);
}

.toast-stack-enter-from,
.toast-stack-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

@media (prefers-reduced-motion: reduce) {
  .toast-stack-enter-active,
  .toast-stack-leave-active,
  .toast-stack-move {
    transition: none;
  }
}

</style>
