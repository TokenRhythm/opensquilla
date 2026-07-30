<template>
  <div v-if="open" class="share-modal-overlay" @click.self="emit('close')">
    <section
      ref="panelRef"
      class="share-modal"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="titleId"
    >
      <header class="share-modal__header">
        <h3 :id="titleId" class="share-modal__title">{{ t('chat.sharePreview') }}</h3>
        <button
          ref="closeBtn"
          type="button"
          class="btn btn--icon btn--ghost"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          @click="emit('close')"
        >
          <Icon name="x" :size="16" />
        </button>
      </header>

      <div class="share-modal__body">
        <div class="share-modal__stage" :aria-busy="busy ? 'true' : 'false'">
          <img
            v-if="imageUrl"
            class="share-modal__image"
            :src="imageUrl"
            :alt="t('chat.sharePreview')"
            decoding="async"
          />
          <p v-else class="share-modal__empty" role="status">{{ t('chat.nothingToPreview') }}</p>
          <div
            v-if="busy"
            class="share-modal__loading"
            role="status"
            :aria-label="t('chat.renderingPreview')"
          >
            <span class="share-modal__spinner" aria-hidden="true" />
          </div>
        </div>
      </div>

      <footer class="share-modal__footer">
        <div
          class="share-modal__seg"
          role="group"
          :aria-label="t('chat.exportTheme')"
        >
          <button
            type="button"
            class="share-modal__seg-btn"
            :class="{ 'is-active': theme === 'light' }"
            :aria-pressed="theme === 'light' ? 'true' : 'false'"
            :disabled="busy"
            @click="emit('setTheme', 'light')"
          >
            <Icon name="sun" :size="14" />
            <span>{{ t('chat.themeLight') }}</span>
          </button>
          <button
            type="button"
            class="share-modal__seg-btn"
            :class="{ 'is-active': theme === 'dark' }"
            :aria-pressed="theme === 'dark' ? 'true' : 'false'"
            :disabled="busy"
            @click="emit('setTheme', 'dark')"
          >
            <Icon name="moon" :size="14" />
            <span>{{ t('chat.themeDark') }}</span>
          </button>
        </div>

        <div class="share-modal__actions">
          <button
            v-if="copySupported"
            type="button"
            class="btn btn--ghost"
            :disabled="busy || !imageUrl"
            @click="emit('copy')"
          >
            <Icon name="copy" :size="14" />
            <span>{{ t('chat.copyImage') }}</span>
          </button>
          <button
            type="button"
            class="btn btn--primary"
            :disabled="busy || !imageUrl"
            @click="emit('download')"
          >
            <Icon name="download" :size="14" />
            <span>{{ t('chat.downloadImage') }}</span>
          </button>
        </div>
      </footer>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogLayer } from '@/composables/useDialogA11y'

const { t } = useI18n()

// The parent owns the blob and its object URL lifecycle: it creates `imageUrl`
// (a `blob:` URL), swaps it on a theme change, and revokes it on close. This
// modal only renders what it is handed and never calls createObjectURL/revoke.
const props = defineProps<{
  open: boolean
  imageUrl: string
  filename: string
  theme: 'light' | 'dark'
  copySupported: boolean
  busy?: boolean
}>()

// Focus-return contract: the PARENT restores focus to the Share invoker when
// the modal closes. This component stores the invoker only to keep the trap
// honest; on close it emits `close` and lets the parent move focus.
const emit = defineEmits<{
  close: []
  download: []
  copy: []
  setTheme: ['light' | 'dark']
}>()

const panelRef = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)
const titleId = `share-preview-title-${Math.random().toString(36).slice(2, 9)}`
const isTopmost = useDialogLayer(computed(() => props.open))

let invokerEl: HTMLElement | null = null

/* ── Dialog a11y: focus trap, Escape, focus return ─────────────────────── */

function trapFocus(event: KeyboardEvent, rootEl: HTMLElement | null) {
  if (event.key !== 'Tab' || !rootEl) return
  const focusables = Array.from(rootEl.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const activeEl = document.activeElement as HTMLElement | null
  const inside = !!activeEl && rootEl.contains(activeEl)
  if (event.shiftKey && (!inside || activeEl === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || activeEl === last)) {
    event.preventDefault()
    first.focus()
  }
}

function onDocumentKeydown(event: KeyboardEvent) {
  if (!props.open) return
  if (!isTopmost.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  trapFocus(event, panelRef.value)
}

watch(
  () => props.open,
  (open, wasOpen) => {
    if (open && !wasOpen) {
      invokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
      document.addEventListener('keydown', onDocumentKeydown)
      nextTick(() => closeBtn.value?.focus())
    } else if (!open && wasOpen) {
      document.removeEventListener('keydown', onDocumentKeydown)
      // Defensive only — the parent owns focus return per the close contract.
      if (invokerEl && document.contains(invokerEl)) invokerEl.focus()
      invokerEl = null
    }
  },
)

onUnmounted(() => {
  document.removeEventListener('keydown', onDocumentKeydown)
})
</script>

<style scoped>
.share-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: var(--scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
}

.share-modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  width: min(640px, 100%);
  max-height: calc(100dvh - var(--sp-8));
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  animation: shareModalIn var(--dur-base) var(--ease-out);
}

@keyframes shareModalIn {
  from { transform: scale(0.98); opacity: 0.4; }
  to { transform: scale(1); opacity: 1; }
}

.share-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.share-modal__title {
  font-family: var(--font-display);
  font-size: var(--fs-lg);
  font-weight: 600;
  line-height: 1.2;
  margin: 0;
  color: var(--text);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.share-modal__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--bg);
  padding: var(--sp-4);
}

.share-modal__stage {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 160px;
}

.share-modal__image {
  display: block;
  max-width: 100%;
  height: auto;
  border-radius: var(--radius-md);
  border: 1px solid var(--hairline);
  box-shadow: var(--shadow-sm);
  transition: opacity var(--transition);
}

.share-modal__stage[aria-busy='true'] .share-modal__image {
  opacity: 0.45;
}

.share-modal__empty {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.share-modal__loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.share-modal__spinner {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-full);
  border: 2px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-top-color: var(--accent);
  animation: shareModalSpin 0.7s linear infinite;
}

@keyframes shareModalSpin {
  to { transform: rotate(360deg); }
}

.share-modal__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}

/* Segmented Light/Dark toggle — accent marks the active export theme. */
.share-modal__seg {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.share-modal__seg-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-3);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
}

.share-modal__seg-btn:hover:not(.is-active):not(:disabled) {
  color: var(--text);
  background: var(--bg-hover);
}

.share-modal__seg-btn.is-active {
  background: var(--accent);
  color: var(--accent-foreground);
}

.share-modal__seg-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.share-modal__seg-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.share-modal__actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

@media (max-width: 768px) {
  .share-modal-overlay {
    padding: 0;
  }

  .share-modal {
    width: 100%;
    max-height: 100dvh;
    height: 100%;
    border: 0;
    border-radius: 0;
  }

  .share-modal__footer {
    gap: var(--sp-2);
  }

  /* 44px minimum tap targets on touch viewports. */
  .share-modal__seg-btn {
    min-height: 44px;
  }

  .share-modal__actions .btn {
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .share-modal {
    animation: none;
  }

  .share-modal__image {
    transition: none;
  }

  .share-modal__spinner {
    animation: none;
  }
}
</style>
