<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { WORKSPACE_FILES_KEY, type WorkspaceFile } from '@/modules/workspaceFiles'
import { downloadBlob } from '@/utils/browser'
import { useDialogA11y } from '@/composables/useDialogA11y'
import Icon from '@/components/Icon.vue'

const props = defineProps<{ file: WorkspaceFile | null; sessionKey: string; scope: string }>()
const emit = defineEmits<{ close: [] }>()
const { t } = useI18n()
const access = inject(WORKSPACE_FILES_KEY, null)
const panel = ref<HTMLElement | null>(null)
const loading = ref(false)
const failed = ref(false)
const content = ref('')
const imageUrl = ref('')
const blob = shallowRef<Blob | null>(null)
const open = computed(() => !!props.file)
useDialogA11y(panel, open, () => emit('close'))
function releaseImage() {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''
}
watch([() => props.file, () => props.sessionKey, () => props.scope], async ([file, sessionKey], _old, onCleanup) => {
  const request = new AbortController()
  onCleanup(() => { request.abort(); releaseImage() })
  releaseImage()
  blob.value = null
  content.value = ''
  failed.value = false
  loading.value = !!file
  if (!file || !sessionKey || !access) { loading.value = false; return }
  try {
    const result = await access.read(sessionKey, file, request.signal)
    if (request.signal.aborted) return
    blob.value = result
    if (file.kind === 'download') {
      downloadBlob(result, file.name)
      emit('close')
    } else if (file.kind === 'image' && /^image\/(png|jpeg|gif|webp)$/.test(result.type)) {
      imageUrl.value = URL.createObjectURL(result)
    } else if (file.kind === 'text' && result.size <= 2 * 1024 * 1024) {
      const text = await result.text()
      if (!request.signal.aborted) content.value = text
    } else {
      // Unsupported or changed content is downloadable, never executable preview HTML.
      downloadBlob(result, file.name)
      emit('close')
    }
  } catch {
    if (!request.signal.aborted) failed.value = true
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(releaseImage)
function download() {
  if (blob.value && props.file) downloadBlob(blob.value, props.file.name)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="file" class="workspace-file-preview" @click.self="emit('close')">
      <section ref="panel" class="workspace-file-preview__panel" role="dialog" aria-modal="true" :aria-label="file.name">
        <header>
          <span>{{ file.name }}</span>
          <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('chat.closePreview')" @click="emit('close')"><Icon name="x" :size="16" /></button>
        </header>
        <p v-if="loading" role="status">{{ t('chat.loadingPreview') }}</p>
        <p v-else-if="failed" role="alert">{{ t('workspaceReference.unavailable') }}</p>
        <img v-else-if="imageUrl" :src="imageUrl" :alt="file.name" />
        <pre v-else tabindex="0"><code>{{ content }}</code></pre>
        <footer v-if="blob">
          <button type="button" class="btn btn--primary" @click="download"><Icon name="download" :size="14" />{{ t('chat.download') }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.workspace-file-preview { position: fixed; inset: 0; z-index: 310; display: flex; align-items: center; justify-content: center; padding: var(--sp-4); background: var(--scrim); }
.workspace-file-preview__panel { display: flex; flex-direction: column; width: min(64rem, 100%); max-height: 90vh; min-height: 8rem; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
header, footer { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-3); }
header { justify-content: space-between; border-bottom: 1px solid var(--border); }
header span { overflow-wrap: anywhere; }
footer { justify-content: flex-end; border-top: 1px solid var(--border); }
pre { margin: 0; padding: var(--sp-4); overflow: auto; font-size: var(--fs-sm); }
img { display: block; min-height: 0; max-width: 100%; object-fit: contain; }
p { padding: var(--sp-4); }
</style>
