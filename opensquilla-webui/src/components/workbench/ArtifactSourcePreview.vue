<template>
  <div class="artifact-source-preview">
    <p v-if="loading" role="status">{{ t('common.loading') }}</p>
    <p v-else-if="error" role="alert">{{ error }}</p>
    <pre v-else tabindex="0"><code>{{ content }}</code></pre>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import type { ArtifactDocument } from '@/types/artifactDocuments'
import { classifyArtifactProductError } from '@/utils/artifactProductErrors'

const props = withDefaults(defineProps<{ document: ArtifactDocument; sessionKey?: string }>(), {
  sessionKey: '',
})
const { t } = useI18n()
const documents = useArtifactDocumentsStore()
const content = ref('')
const loading = ref(false)
const error = ref('')
let request: AbortController | null = null

async function reload(): Promise<void> {
  request?.abort()
  const current = new AbortController()
  request = current
  content.value = ''
  error.value = ''
  loading.value = true
  try {
    const source = await documents.provider?.readSource({
      sessionKey: props.sessionKey,
      documentId: props.document.documentId,
      revisionId: props.document.headRevisionId,
    }, current.signal)
    if (current.signal.aborted) return
    if (!source) throw new Error(String(t('workbench.artifactDocument.sourceUnavailable')))
    content.value = source.content
  } catch (cause) {
    if (!current.signal.aborted) error.value = classifyArtifactProductError(cause).fallbackMessage
  } finally {
    if (request === current) loading.value = false
  }
}

watch(() => [props.sessionKey, props.document.documentId, props.document.headRevisionId], reload, {
  immediate: true,
})
onBeforeUnmount(() => request?.abort())
defineExpose({ reload })
</script>

<style scoped>
.artifact-source-preview { flex: 1; min-width: 0; overflow: auto; padding: 16px; }
pre { margin: 0; font-family: var(--font-mono, monospace); font-size: 12px; line-height: 1.6; }
</style>
