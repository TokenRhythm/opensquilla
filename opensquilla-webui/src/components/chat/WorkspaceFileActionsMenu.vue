<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { WorkspaceFile } from '@/modules/workspaceFiles'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { usePlatform } from '@/platform'

export type WorkspaceFileAction = 'open' | 'native-open' | 'reveal' | 'copy-path' | 'copy-contents' | 'download'

const props = withDefaults(defineProps<{
  sessionKey: string
  workbenchAvailable?: boolean
  nativeOpenAvailable?: boolean
  nativeRevealAvailable?: boolean
  nativeRevealLabel?: string
  copyContentsAvailable?: boolean
}>(), {
  workbenchAvailable: false,
  nativeOpenAvailable: false,
  nativeRevealAvailable: false,
  nativeRevealLabel: '',
  copyContentsAvailable: false,
})

const emit = defineEmits<{ action: [action: WorkspaceFileAction, file: WorkspaceFile] }>()
const { t } = useI18n()
const platform = usePlatform()
const visible = ref(false)
const topmost = useDialogLayer(visible)
const menu = ref<HTMLElement | null>(null)
const selected = shallowRef<WorkspaceFile | null>(null)
const point = ref({ left: 0, top: 0 })
const nativeReady = ref(false)
let showId = 0
let invoker: HTMLElement | null = null

const title = computed(() => selected.value?.path || selected.value?.name || '')
const canOpenInWorkbench = computed(() => props.workbenchAvailable && selected.value?.kind === 'text')

function close() {
  showId++
  const wasVisible = visible.value
  visible.value = false
  if (wasVisible && invoker?.isConnected) invoker.focus({ preventScroll: true })
  selected.value = null
}

function cancel(event?: Event) {
  event?.preventDefault()
  close()
}

function constrainToViewport() {
  const bounds = menu.value?.getBoundingClientRect()
  point.value = {
    left: Math.max(8, Math.min(point.value.left, window.innerWidth - (bounds?.width || 260) - 8)),
    top: Math.max(8, Math.min(point.value.top, window.innerHeight - (bounds?.height || 320) - 8)),
  }
}

async function show(event: MouseEvent | KeyboardEvent, file: WorkspaceFile) {
  if (event instanceof KeyboardEvent && event.key !== 'ContextMenu'
    && !(event.shiftKey && event.key === 'F10')) return
  event.preventDefault()
  event.stopPropagation()
  const requestId = ++showId
  nativeReady.value = false
  selected.value = file
  invoker = (event.target instanceof Element
    ? event.target.closest<HTMLElement>('button, a, [tabindex]') : null)
    || (document.activeElement instanceof HTMLElement ? document.activeElement : null)
  point.value = {
    left: event instanceof MouseEvent && event.type === 'contextmenu' ? event.clientX : invoker?.getBoundingClientRect().left ?? 8,
    top: event instanceof MouseEvent && event.type === 'contextmenu' ? event.clientY : invoker?.getBoundingClientRect().bottom ?? 8,
  }
  visible.value = true
  await nextTick()
  if (!visible.value || selected.value !== file) return
  constrainToViewport()
  menu.value?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
  if (file.nativeActions && platform.id === 'desktop'
    && (props.nativeOpenAvailable || props.nativeRevealAvailable)) {
    try {
      const [status, connection] = await Promise.all([
        platform.gateway.getStatus?.(), platform.gateway.getConnection?.(),
      ])
      if (requestId !== showId || !visible.value) return
      nativeReady.value = status?.owned === true && status.status === 'ready'
        && connection?.status === 'ready' && !!connection.instanceId
    } catch { /* Unavailable native hosts retain the portable file actions. */ }
    await nextTick()
  }
  if (requestId !== showId || !visible.value) return
  constrainToViewport()
}

function perform(action: WorkspaceFileAction) {
  const file = selected.value
  if (!file) return
  close()
  emit('action', action, file)
}

function onKeydown(event: KeyboardEvent) {
  if (!topmost.value) return
  if (event.key === 'Escape' || event.key === 'Tab') { event.stopPropagation(); cancel(event); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const buttons = [...menu.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []]
  if (!buttons.length) return
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
  buttons[next]?.focus()
}

onBeforeUnmount(close)
watch(() => props.sessionKey, close, { flush: 'sync' })
defineExpose({ show, close })
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="workspace-file-actions-backdrop" @pointerdown.self="cancel">
      <div
        ref="menu"
        class="workspace-file-actions-menu"
        role="menu"
        tabindex="-1"
        :aria-label="t('workspaceReference.actions')"
        :style="{ left: `${point.left}px`, top: `${point.top}px` }"
        @keydown="onKeydown"
        @contextmenu.prevent
      >
        <button v-if="selected" role="menuitem" type="button" @click="perform('open')">
          {{ canOpenInWorkbench ? t('workspaceReference.open', { path: title }) : t('resourceActions.preview') }}
        </button>
        <button v-if="nativeOpenAvailable && nativeReady && selected?.nativeActions" role="menuitem" type="button" @click="perform('native-open')">
          {{ t('resourceActions.openSource') }}
        </button>
        <button v-if="nativeRevealAvailable && nativeReady && selected?.nativeActions" role="menuitem" type="button" @click="perform('reveal')">
          {{ nativeRevealLabel || t('resourceActions.reveal') }}
        </button>
        <button role="menuitem" type="button" @click="perform('copy-path')">
          {{ t('workspaceReference.copyPath') }}
        </button>
        <button v-if="copyContentsAvailable && selected?.kind === 'text'" role="menuitem" type="button" @click="perform('copy-contents')">
          {{ t('resourceActions.copyContents') }}
        </button>
        <button role="menuitem" type="button" @click="perform('download')">
          {{ t(platform.files.saveArtifact ? 'resourceActions.saveAs' : 'chat.download') }}
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.workspace-file-actions-backdrop { position: fixed; inset: 0; z-index: 10000; }
.workspace-file-actions-menu { position: fixed; display: flex; flex-direction: column; min-width: 240px; max-width: calc(100vw - 16px); max-height: calc(100vh - 16px); overflow-y: auto; padding: 5px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
.workspace-file-actions-menu button { border: 0; border-radius: var(--radius-sm); padding: 8px 12px; background: transparent; color: inherit; text-align: start; font: inherit; font-size: 0.8125rem; cursor: pointer; overflow-wrap: anywhere; }
.workspace-file-actions-menu button:hover, .workspace-file-actions-menu button:focus-visible { background: var(--bg-hover); outline: 1px solid var(--accent); }
</style>
