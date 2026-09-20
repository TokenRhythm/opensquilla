<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useImageClipboard, type ClipboardImageSource, isSvgClipboardCandidate } from '@/composables/useImageClipboard'

const props = withDefaults(defineProps<{
  source: ClipboardImageSource
  sessionKey?: string
  labelled?: boolean
  sourceMenu?: boolean
}>(), { sessionKey: '', labelled: false, sourceMenu: true })
const { t } = useI18n()
const { busy, copy } = useImageClipboard({
  source: () => props.source,
  sessionKey: () => props.sessionKey,
})
const descriptor = computed(() => props.source.kind === 'artifact' ? props.source.artifact : props.source.attachment)
const svg = computed(() => isSvgClipboardCandidate(descriptor.value))
const menuOpen = ref(false)
const menu = ref<HTMLElement | null>(null)
const moreButton = ref<HTMLButtonElement | null>(null)
const position = ref({ left: 0, top: 0 })
const isTopmost = useDialogLayer(menuOpen)
const feedback = ref('')
const result = ref<'idle' | 'ok' | 'error'>('idle')
let feedbackTimer: ReturnType<typeof setTimeout> | undefined
let generation = 0

function closeMenu(restoreFocus = true) {
  menuOpen.value = false
  if (restoreFocus) moreButton.value?.focus({ preventScroll: true })
}

async function openMenu() {
  if (menuOpen.value) { closeMenu(); return }
  const rect = moreButton.value?.getBoundingClientRect()
  position.value = { left: rect?.left ?? 8, top: rect?.bottom ?? 8 }
  menuOpen.value = true
  await nextTick()
  const bounds = menu.value?.getBoundingClientRect()
  position.value = {
    left: Math.max(8, Math.min(position.value.left, window.innerWidth - (bounds?.width ?? 220) - 8)),
    top: Math.max(8, Math.min(position.value.top, window.innerHeight - (bounds?.height ?? 48) - 8)),
  }
  menu.value?.querySelector<HTMLButtonElement>('button')?.focus()
}

function menuKeydown(event: KeyboardEvent) {
  if (!isTopmost.value) return
  if (event.key === 'Escape' || event.key === 'Tab') {
    event.preventDefault()
    event.stopPropagation()
    closeMenu()
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    menu.value?.querySelector<HTMLButtonElement>('button')?.focus()
  }
}

async function perform(mode: 'image' | 'svg-source') {
  if (busy.value) return
  if (menuOpen.value) closeMenu()
  const attempt = ++generation
  feedback.value = ''
  result.value = 'idle'
  // Calling copy before awaiting preserves the click's clipboard activation.
  const ok = await copy(mode)
  if (attempt !== generation) return
  result.value = ok ? 'ok' : 'error'
  feedback.value = t(ok
    ? mode === 'image' ? 'imageClipboard.copiedImage' : 'imageClipboard.copiedSource'
    : 'imageClipboard.failed')
  clearTimeout(feedbackTimer)
  feedbackTimer = setTimeout(() => { feedback.value = ''; result.value = 'idle' }, 1200)
}

function reset() {
  generation++
  clearTimeout(feedbackTimer)
  feedback.value = ''
  result.value = 'idle'
  closeMenu(false)
}
watch([descriptor, () => props.sessionKey], reset)
onBeforeUnmount(reset)
</script>

<template>
  <span class="image-copy-actions" :data-state="busy ? 'busy' : result">
    <button type="button" class="btn btn--ghost image-copy-actions__copy"
      :class="{ 'btn--icon': !labelled }" data-testid="copy-image"
      :aria-label="t('imageClipboard.copyImage')" :title="t('imageClipboard.imageDescription')"
      :aria-busy="busy" :disabled="busy" @click.stop="perform('image')">
      <span v-if="busy" class="spinner" aria-hidden="true" />
      <Icon v-else name="copy" :size="16" />
      <span v-if="labelled">{{ t('imageClipboard.copyImage') }}</span>
    </button>
    <button v-if="svg && sourceMenu" ref="moreButton" type="button" class="btn btn--icon btn--ghost"
      data-testid="image-copy-more" :aria-label="t('resourceActions.more')" :title="t('resourceActions.more')"
      aria-haspopup="menu" :aria-expanded="menuOpen" :disabled="busy"
      @click.stop="openMenu" @keydown.down.prevent="openMenu">
      <Icon name="moreHorizontal" :size="16" />
    </button>
    <span class="sr-only" role="status" aria-live="polite">{{ busy ? t('imageClipboard.copying') : feedback }}</span>
    <Teleport to="body">
      <div v-if="menuOpen" class="image-copy-menu-backdrop" @pointerdown.self="closeMenu()">
        <div ref="menu" class="image-copy-menu" role="menu" :aria-label="t('resourceActions.more')"
          :style="{ left: `${position.left}px`, top: `${position.top}px` }" @keydown="menuKeydown">
          <button type="button" role="menuitem" data-testid="copy-svg-source" @click.stop="perform('svg-source')">
            {{ t('imageClipboard.copySource') }}
          </button>
        </div>
      </div>
    </Teleport>
  </span>
</template>

<style scoped>
.image-copy-actions { display: inline-flex; align-items: center; gap: var(--sp-1); flex-shrink: 0; }
.image-copy-actions__copy { flex-shrink: 0; }
.image-copy-menu-backdrop { position: fixed; inset: 0; z-index: 10000; }
.image-copy-menu { position: fixed; width: max-content; min-width: 180px; padding: var(--sp-1); max-width: calc(100vw - 16px); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
.image-copy-menu button { border: 0; border-radius: var(--radius-sm); padding: var(--sp-2) var(--sp-3); background: transparent; color: inherit; font: inherit; font-size: var(--fs-sm); cursor: pointer; text-align: start; }
.image-copy-menu button:hover { background: var(--bg-hover); }
.image-copy-menu button:focus-visible { outline: 1px solid var(--accent); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
</style>
