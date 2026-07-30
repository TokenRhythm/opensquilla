<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-overlay" @click="!busy && emit('close')">
        <section
          ref="modalRef"
          class="modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="diagnostics-bundle-title"
          aria-describedby="diagnostics-bundle-description"
          @click.stop
        >
          <header class="modal__header">
            <span class="modal__title-icon" aria-hidden="true">
              <Icon name="download" :size="19" />
            </span>
            <div class="modal__title-copy">
              <h3 id="diagnostics-bundle-title" class="modal__title">
                {{ t('monitorSupport.bundleTitle') }}
              </h3>
              <p id="diagnostics-bundle-description" class="modal__subtitle">
                {{ t('monitorSupport.bundleSubtitle') }}
              </p>
            </div>
            <button
              type="button"
              class="btn btn--icon btn--ghost modal__close"
              :disabled="busy"
              :aria-label="t('common.close')"
              :title="t('common.close')"
              @click="emit('close')"
            >
              <Icon name="x" :size="16" />
            </button>
          </header>

          <div class="modal__body">
            <section class="bundle-dialog__contents" :aria-label="t('monitorSupport.bundleDefaultIncludes')">
              <h4>{{ t('monitorSupport.bundleDefaultIncludes') }}</h4>
              <div class="bundle-dialog__contents-grid">
                <span><Icon name="check" :size="14" />{{ t('monitorSupport.bundleReadiness') }}</span>
                <span><Icon name="check" :size="14" />{{ t('monitorSupport.bundleConfig') }}</span>
                <span><Icon name="check" :size="14" />{{ t('monitorSupport.bundleLogs') }}</span>
                <span><Icon name="check" :size="14" />{{ t('monitorSupport.bundlePlatform') }}</span>
              </div>
            </section>

            <div class="bundle-dialog__scope" role="note" :aria-label="t('monitorSupport.bundleScopeTitle')">
              <span class="bundle-dialog__scope-icon" aria-hidden="true">
                <Icon name="clock" :size="16" />
              </span>
              <span>
                <strong>{{ t('monitorSupport.bundleScopeTitle') }}</strong>
                <small>{{ t('monitorSupport.bundleScopeBody') }}</small>
              </span>
            </div>

            <label class="bundle-dialog__option">
              <input v-model="includeContent" type="checkbox" :disabled="busy" />
              <span>
                <strong>{{ t('monitorSupport.bundleIncludeContentTitle') }}</strong>
                <small>{{ t('monitorSupport.bundleIncludeContentBody') }}</small>
              </span>
            </label>

            <div class="bundle-dialog__privacy" role="note">
              <Icon name="shield" :size="17" aria-hidden="true" />
              <p>
                <strong>{{ t('monitorSupport.bundleCredentialsTitle') }}</strong>
                <span>{{ t('monitorSupport.bundleCredentialsBody') }}</span>
              </p>
            </div>
          </div>

          <footer class="modal__footer">
            <button ref="cancelBtn" type="button" class="btn btn--ghost" :disabled="busy" @click="emit('close')">
              {{ t('monitorSupport.bundleCancel') }}
            </button>
            <button type="button" class="btn btn--primary" :disabled="busy" @click="emit('confirm', { includeContent })">
              <Icon name="download" :size="16" />
              {{ t('monitorSupport.bundleConfirm') }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'

const props = withDefaults(defineProps<{ open: boolean; busy?: boolean }>(), {
  busy: false,
})
const emit = defineEmits<{
  (event: 'close'): void
  (event: 'confirm', payload: { includeContent: boolean }): void
}>()

const { t } = useI18n()
const includeContent = ref(false)
const modalRef = ref<HTMLElement | null>(null)
const cancelBtn = ref<HTMLButtonElement | null>(null)
const isOpen = computed(() => props.open)

// Privacy-sensitive opt-in: re-arm to unchecked every time the dialog opens so
// an earlier opt-in never silently carries over to the next download.
watch(isOpen, (open) => {
  if (open) includeContent.value = false
})

// Cancel is the initial focus target so the primary action is never
// auto-focused; Escape and Tab-trapping come from the shared a11y helper.
useDialogA11y(modalRef, isOpen, () => emit('close'), { initialFocus: cancelBtn })
</script>

<style scoped>
.modal-overlay {
  align-items: center;
  background: var(--scrim);
  bottom: 0;
  display: flex;
  justify-content: center;
  left: 0;
  padding: var(--sp-4);
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1100;
}

.modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - (2 * var(--sp-4)));
  max-width: 610px;
  overflow: hidden;
  width: 100%;
}

.modal__header {
  align-items: center;
  border-bottom: 1px solid var(--hairline);
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5);
}

.modal__title-icon {
  align-items: center;
  background: var(--bg-surface-2);
  border-radius: var(--radius-md);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 38px;
  height: 38px;
  justify-content: center;
}

.modal__title-copy {
  flex: 1;
  min-width: 0;
}

.modal__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0;
}

.modal__subtitle {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 3px 0 0;
}

.modal__close {
  flex: 0 0 auto;
}

.modal__body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  overflow-y: auto;
  padding: var(--sp-4) var(--sp-5);
}

.bundle-dialog__contents {
  background: var(--bg-surface-2);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  padding: var(--sp-3);
}

.bundle-dialog__contents h4 {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 600;
  margin: 0 0 var(--sp-2);
}

.bundle-dialog__contents-grid {
  display: grid;
  gap: var(--sp-2) var(--sp-4);
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.bundle-dialog__contents-grid span {
  align-items: center;
  color: var(--text);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
}

.bundle-dialog__contents-grid :deep(.icon) {
  color: var(--ok);
}

.bundle-dialog__scope {
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: 30px 1fr;
  padding: var(--sp-3);
}

.bundle-dialog__scope-icon {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: var(--radius-sm);
  color: var(--accent);
  display: inline-flex;
  height: 30px;
  justify-content: center;
  width: 30px;
}

.bundle-dialog__scope strong,
.bundle-dialog__scope small {
  display: block;
}

.bundle-dialog__scope strong {
  font-size: var(--fs-sm);
  line-height: 1.35;
}

.bundle-dialog__scope small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin-top: 3px;
}

.bundle-dialog__option {
  align-items: flex-start;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  display: grid;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  grid-template-columns: auto 1fr;
  padding: var(--sp-3);
}

.bundle-dialog__option input {
  margin-top: 2px;
}

.bundle-dialog__option strong,
.bundle-dialog__option small {
  display: block;
}

.bundle-dialog__option strong {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.bundle-dialog__option small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin-top: 2px;
}

.bundle-dialog__privacy {
  align-items: flex-start;
  background: color-mix(in srgb, var(--ok) 10%, var(--bg-surface));
  border-radius: var(--radius-md);
  color: var(--ok);
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-3);
}

.bundle-dialog__privacy p {
  margin: 0;
}

.bundle-dialog__privacy strong,
.bundle-dialog__privacy span {
  display: block;
}

.bundle-dialog__privacy strong {
  font-size: var(--fs-xs);
  font-weight: 600;
}

.bundle-dialog__privacy span {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin-top: 2px;
}

.modal__footer {
  align-items: center;
  background: var(--bg-surface-2);
  border-top: 1px solid var(--hairline);
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
  padding: var(--sp-3) var(--sp-5);
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity var(--dur-base);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

@media (max-width: 560px) {
  .modal-overlay {
    padding: var(--sp-3);
  }

  .modal__header,
  .modal__body,
  .modal__footer {
    padding-left: var(--sp-3);
    padding-right: var(--sp-3);
  }

  .bundle-dialog__contents-grid {
    grid-template-columns: 1fr;
  }

  .modal__footer {
    align-items: stretch;
    flex-direction: column;
  }

  .modal__footer .btn {
    justify-content: center;
    width: 100%;
  }
}
</style>
