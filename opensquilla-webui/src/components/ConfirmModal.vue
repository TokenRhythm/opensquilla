<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="confirmState" class="modal-overlay" @click="onCancel">
        <div
          ref="modalRef"
          class="modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-modal-title"
          aria-describedby="confirm-modal-description"
          @click.stop
        >
          <h3 id="confirm-modal-title" class="modal__title">{{ confirmState.title }}</h3>
          <div class="modal__body">
            <p id="confirm-modal-description">{{ confirmState.body }}</p>
          </div>
          <div class="modal__footer">
            <button
              v-if="confirmState.showCancel !== false"
              ref="cancelBtn"
              type="button"
              class="btn btn--ghost"
              @click.stop="onCancel"
            >{{ t('common.cancel') }}</button>
            <button
              v-if="confirmState.secondaryLabel"
              type="button"
              :class="['btn', 'modal__secondary', confirmState.secondaryClass]"
              @click.stop="onSecondary"
            >
              {{ confirmState.secondaryLabel }}
            </button>
            <button
              ref="primaryBtn"
              type="button"
              :class="['btn', 'modal__primary', confirmState.primaryClass]"
              @click.stop="onConfirm"
            >
              {{ confirmState.primaryLabel }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useConfirm } from '@/composables/useConfirm'
import { useDialogA11y } from '@/composables/useDialogA11y'

const { t } = useI18n()
const { confirmState, resolveConfirm, resolveConfirmChoice } = useConfirm()

const modalRef = ref<HTMLElement | null>(null)
const cancelBtn = ref<HTMLElement | null>(null)
const primaryBtn = ref<HTMLElement | null>(null)
const isOpen = computed(() => confirmState.value !== null)
const initialFocus = computed(() => (
  confirmState.value?.showCancel === false ? primaryBtn.value : cancelBtn.value
))

function onConfirm() {
  resolveConfirm(true)
}

function onCancel() {
  resolveConfirm(false)
}

function onSecondary() {
  resolveConfirmChoice('secondary')
}

// Cancel sits first (leading edge) and is the initial focus target when shown.
// A two-action save/discard prompt instead focuses its non-destructive save
// action, never the destructive discard button. Escape and Tab-trapping come
// from the shared a11y helper.
useDialogA11y(modalRef, isOpen, onCancel, { initialFocus })
</script>

<style scoped>
.modal-overlay {
  align-items: center;
  background: var(--scrim);
  bottom: 0;
  display: flex;
  justify-content: center;
  left: 0;
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1100;
}

.modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  max-width: 420px;
  padding: var(--sp-5);
  width: 90%;
}

.modal__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0 0 var(--sp-3);
}

.modal__body {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin-bottom: var(--sp-4);
}

.modal__body p {
  margin: 0;
  white-space: pre-line;
}

.modal__footer {
  display: flex;
  gap: var(--sp-3);
  justify-content: flex-end;
}

.modal__primary {
  box-shadow: 0 1px 2px color-mix(in srgb, var(--scrim) 24%, transparent);
  min-width: 88px;
  opacity: 1;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity var(--dur-base);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
