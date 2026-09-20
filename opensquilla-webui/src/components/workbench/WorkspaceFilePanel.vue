<template>
  <div class="workspace-file" data-testid="workspace-file-panel">
    <p v-if="loading" role="status">{{ t('workspaceReference.loading') }}</p>
    <p v-else-if="errorKey" role="alert">{{ t(errorKey) }}</p>
    <template v-else-if="snapshot">
      <div class="workspace-file__range" role="status">
        {{ t('workspaceReference.lines', { start: snapshot.startLine, end: snapshot.endLine, total: snapshot.totalLines }) }}
      </div>
      <button v-if="windowStart > 0" class="workspace-file__more" type="button" @click="windowStart = Math.max(0, windowStart - 200)">{{ t('workspaceReference.previous') }}</button>
      <pre ref="source" class="workspace-file__source" tabindex="0" :aria-label="snapshot.relativePath"><code><span
        v-for="line in visibleLines" :key="line.number" class="workspace-file__line"
        :class="{ 'is-selected': line.number >= snapshot.startLine && line.number <= snapshot.endLine }"
        :data-line="line.number" :aria-current="line.number === snapshot.startLine ? 'location' : undefined"
      ><span class="workspace-file__number" aria-hidden="true">{{ line.number }}</span><span>{{ line.text || ' ' }}</span></span></code></pre>
      <button v-if="windowEnd < lines.length" class="workspace-file__more" type="button" @click="windowStart = windowEnd">{{ t('workspaceReference.next') }}</button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'

const props = defineProps<{ snapshot?: WorkspaceSourceSnapshot | null; loading?: boolean; errorKey?: string }>()
const { t } = useI18n()
const source = ref<HTMLElement | null>(null)
const lines = computed(() => {
  // Match the source receipt's Python splitlines() boundaries exactly.
  const values = props.snapshot?.content.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/) ?? []
  if (values[values.length - 1] === '') values.pop()
  return values
})
const windowStart = ref(0)
const windowEnd = computed(() => Math.min(lines.value.length, windowStart.value + 200))
const visibleLines = computed(() => lines.value.slice(windowStart.value, windowEnd.value)
  .map((text, index) => ({ text, number: windowStart.value + index + 1 })))
watch(() => props.snapshot, async snapshot => {
  windowStart.value = Math.max(0, (snapshot?.startLine ?? 1) - 41)
  await nextTick()
  source.value?.querySelector('[aria-current="location"]')?.scrollIntoView?.({ block: 'center' })
}, { immediate: true })
</script>

<style scoped>
.workspace-file { height: 100%; min-width: 0; overflow: auto; background: var(--bg-surface); color: var(--text); }
.workspace-file > p { padding: var(--sp-4); }
.workspace-file__range { padding: var(--sp-2) var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); border-bottom: 1px solid var(--border); }
.workspace-file__source { margin: 0; padding: var(--sp-3) 0; overflow: auto; font-size: var(--fs-sm); tab-size: 4; }
.workspace-file__line { display: flex; min-width: max-content; padding-right: var(--sp-4); line-height: 1.65; }
.workspace-file__line.is-selected { background: var(--bg-hover); box-shadow: inset 3px 0 var(--border-focus); }
.workspace-file__number { display: inline-block; flex: 0 0 5ch; text-align: right; margin-right: var(--sp-4); color: var(--text-muted); user-select: none; }
.workspace-file__more { width: 100%; border: 0; padding: var(--sp-2); background: var(--bg-hover); color: var(--text); font: inherit; font-size: var(--fs-sm); cursor: pointer; }
.workspace-file__source:focus-visible, button:focus-visible { outline: 2px solid var(--border-focus); outline-offset: -2px; }
</style>
