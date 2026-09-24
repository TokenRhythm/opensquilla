<template>
  <Teleport to="body">
    <div v-if="open" class="browser-window-overlay" @click.self="emit('close')">
      <section
        ref="dialogRef"
        class="browser-window-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('chat.composer.browserUse')"
      >
        <BrowserStartPanel ready :can-reopen="false" external @open="openWindow" />
        <button
          type="button"
          class="btn btn--icon btn--ghost browser-window-dialog__close"
          :aria-label="t('common.close')"
          @click="emit('close')"
        >
          <Icon name="x" :size="16" />
        </button>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import BrowserStartPanel from '@/components/workbench/BrowserStartPanel.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { normalizeBrowserUrl } from '@/workbench/browserItems'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const { t } = useI18n()
const dialogRef = ref<HTMLElement | null>(null)
useDialogA11y(dialogRef, computed(() => props.open), () => emit('close'))

function openWindow(value: string) {
  const url = normalizeBrowserUrl(value)
  if (!url) return
  window.open(url, '_blank', 'noopener,noreferrer')
  emit('close')
}
</script>

<style scoped>
.browser-window-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: grid;
  place-items: center;
  background: var(--scrim);
}

.browser-window-dialog {
  position: relative;
  width: min(94vw, 520px);
  padding-top: var(--sp-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
  box-shadow: var(--shadow-lg);
}

.browser-window-dialog__close {
  position: absolute;
  top: var(--sp-2);
  right: var(--sp-2);
}
</style>
