<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ActivityCodeDetails from '@/components/chat/ActivityCodeDetails.vue'
import type { ToolHistoryFileView } from '@/workbench/toolHistoryFileItems'

const props = defineProps<{
  view: ToolHistoryFileView
  wrapLines?: boolean
  copyState?: 'idle' | 'copied' | 'error'
  downloadError?: boolean
}>()
const { t } = useI18n()
const copyFeedback = computed(() => props.copyState === 'error' ? t('workspaceReference.copyFailed') : '')
</script>

<template>
  <section class="historical-tool-file" :class="{ 'historical-tool-file--wrap': wrapLines }"
    :aria-label="view.path" data-testid="historical-tool-file-panel">
    <ActivityCodeDetails :view="view" full />
    <p v-if="downloadError" class="historical-tool-file__download-error" role="status">{{ t('shared.runTrace.downloadContentFailed') }}</p>
    <span class="historical-tool-file__feedback" role="status">{{ copyFeedback }}</span>
  </section>
</template>

<style scoped>
.historical-tool-file { min-width: 0; min-height: 100%; padding: var(--sp-4) var(--sp-5) var(--sp-6); }
.historical-tool-file__download-error { color: var(--text-muted); font-size: 0.75rem; }
.historical-tool-file.historical-tool-file--wrap :deep(pre),
.historical-tool-file.historical-tool-file--wrap :deep(code) { width: 100%; min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.historical-tool-file__feedback { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
@media (max-width: 650px) { .historical-tool-file { padding: var(--sp-4); } }
</style>
