<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { WORKSPACE_FILES_KEY, type WorkspaceFile } from '@/modules/workspaceFiles'
import { usePlatform } from '@/platform'
import { useDialogA11y } from '@/composables/useDialogA11y'
import Icon from '@/components/Icon.vue'
import WorkspaceFileActionsMenu, { type WorkspaceFileAction } from './WorkspaceFileActionsMenu.vue'

const props = withDefaults(defineProps<{
  file: WorkspaceFile | null
  sessionKey: string
  scope: string
  workbenchAvailable?: boolean
  nativeActionsAvailable?: boolean
  nativeRevealLabel?: string
}>(), { workbenchAvailable: false, nativeActionsAvailable: false, nativeRevealLabel: '' })
const emit = defineEmits<{ close: []; action: [action: WorkspaceFileAction, file: WorkspaceFile] }>()
const { t } = useI18n()
const platform = usePlatform()
const access = inject(WORKSPACE_FILES_KEY, null)
const panel = ref<HTMLElement | null>(null)
const loading = ref(false)
const failed = ref(false)
const unsupported = ref(false)
const content = ref('')
const imageUrl = ref('')
const blob = shallowRef<Blob | null>(null)
const open = computed(() => !!props.file)
const actionsMenu = ref<InstanceType<typeof WorkspaceFileActionsMenu> | null>(null)
useDialogA11y(panel, open, () => emit('close'))
function releaseImage() {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''
}
watch([() => props.file, () => props.sessionKey, () => props.scope], async ([file, sessionKey], _old, onCleanup) => {
  actionsMenu.value?.close()
  const request = new AbortController()
  onCleanup(() => { request.abort(); releaseImage() })
  releaseImage()
  blob.value = null
  content.value = ''
  failed.value = false
  unsupported.value = false
  loading.value = !!file
  if (!file || !sessionKey || !access) { loading.value = false; return }
  try {
    const result = await access.read(sessionKey, file, request.signal)
    if (request.signal.aborted) return
    blob.value = result
    if (file.kind === 'image' && /^image\/(png|jpeg|gif|webp)$/.test(result.type)) {
      imageUrl.value = URL.createObjectURL(result)
    } else if (file.kind === 'text' && result.size <= 2 * 1024 * 1024) {
      const text = new TextDecoder('utf-8', { fatal: true }).decode(await result.arrayBuffer())
      if (request.signal.aborted) return
      if (text.includes('\0')) unsupported.value = true
      else content.value = text
    } else {
      // Unsupported or changed content remains downloadable, never executable.
      unsupported.value = true
    }
  } catch {
    if (!request.signal.aborted) failed.value = true
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(releaseImage)
function download() {
  if (blob.value && props.file) emit('action', 'download', props.file)
}
function showActions(event: MouseEvent | KeyboardEvent) {
  if (props.file) void actionsMenu.value?.show(event, props.file)
}
function handleAction(action: WorkspaceFileAction, file: WorkspaceFile) {
  emit('action', action, file)
}
function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}
function formatType(file: WorkspaceFile): string {
  const mime = file.mime.toLowerCase().split(';', 1)[0]
  const labels: Record<string, string> = {
    'text/x-python': 'Python', 'application/javascript': 'JavaScript', 'text/javascript': 'JavaScript',
    'text/markdown': 'Markdown', 'text/html': 'HTML', 'application/json': 'JSON',
  }
  return labels[mime] || (mime.split('/').pop() || file.kind).toUpperCase()
}
</script>

<template>
  <Teleport to="body">
    <div v-if="file" class="workspace-file-preview" @click.self="emit('close')">
      <section ref="panel" class="workspace-file-preview__panel" role="dialog" aria-modal="true" :aria-label="file.name" @contextmenu="showActions" @keydown="showActions">
        <header>
          <div class="workspace-file-preview__heading">
            <strong>{{ file.name }}</strong>
            <small :title="file.path">{{ file.path }} · {{ formatType(file) }} · {{ formatSize(file.size) }} · {{ t('workspaceReference.readonly') }}</small>
          </div>
          <div class="workspace-file-preview__actions">
            <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('workspaceReference.actions')" aria-haspopup="menu" @click="showActions"><Icon name="moreHorizontal" :size="16" /></button>
            <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('chat.closePreview')" @click="emit('close')"><Icon name="x" :size="16" /></button>
          </div>
        </header>
        <p v-if="loading" role="status">{{ t('chat.loadingPreview') }}</p>
        <p v-else-if="failed" role="alert">{{ t('workspaceReference.unavailable') }}</p>
        <p v-else-if="unsupported" role="status">{{ t('workspaceReference.unsupported') }} · {{ file.mime }} · {{ formatSize(file.size) }}</p>
        <img v-else-if="imageUrl" :src="imageUrl" :alt="file.name" />
        <pre v-else tabindex="0"><code>{{ content }}</code></pre>
        <footer v-if="blob">
          <button type="button" class="btn btn--primary" @click="download"><Icon name="download" :size="14" />{{ t(platform.files.saveArtifact ? 'resourceActions.saveAs' : 'chat.download') }}</button>
        </footer>
      </section>
      <WorkspaceFileActionsMenu
        ref="actionsMenu"
        :session-key="sessionKey"
        :workbench-available="workbenchAvailable"
        :native-open-available="nativeActionsAvailable"
        :native-reveal-available="nativeActionsAvailable"
        :native-reveal-label="nativeRevealLabel"
        :copy-contents-available="true"
        @action="handleAction"
      />
    </div>
  </Teleport>
</template>

<style scoped>
.workspace-file-preview { position: fixed; inset: 0; z-index: 310; display: flex; align-items: center; justify-content: center; padding: var(--sp-4); background: var(--scrim); }
.workspace-file-preview__panel { display: flex; flex-direction: column; width: min(64rem, 100%); max-height: 90vh; min-height: 8rem; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
header, footer { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-3); }
header { justify-content: space-between; border-bottom: 1px solid var(--border); }
.workspace-file-preview__heading { min-width: 0; display: flex; flex-direction: column; gap: 0.2rem; }
.workspace-file-preview__heading strong, .workspace-file-preview__heading small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-file-preview__heading small { color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-file-preview__actions { display: flex; flex: 0 0 auto; align-items: center; gap: var(--sp-1); }
footer { justify-content: flex-end; border-top: 1px solid var(--border); }
pre { margin: 0; padding: var(--sp-4); overflow: auto; font-size: var(--fs-sm); }
img { display: block; min-height: 0; max-width: 100%; object-fit: contain; }
p { padding: var(--sp-4); }
</style>
