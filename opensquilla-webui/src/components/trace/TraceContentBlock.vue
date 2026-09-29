<template>
  <article class="trace-content-block" :data-block-id="block.id" :data-format="block.format || 'value'">
    <header class="trace-content-block__header">
      <button type="button" class="trace-content-block__heading" :aria-expanded="expanded" @click="expanded = !expanded">
        <span aria-hidden="true">{{ expanded ? '⌄' : '›' }}</span><strong>{{ label }}</strong>
        <small>{{ contentMeta }}</small>
      </button>
      <button type="button" class="trace-content-block__copy" :aria-label="`${t(`${ns}.copy`)} · ${label}`" @click="copy">{{ t(`${ns}.${copyState}`) }}</button>
    </header>
    <div v-if="expanded" class="trace-content-block__body">
      <div v-if="block.children" class="trace-content-block__children">
        <TraceContentBlock v-for="child in block.children" :key="child.id" :block="child" />
      </div>
      <div v-else-if="block.format === 'diff'" class="trace-content-block__diff">
        <section class="trace-content-block__before"><span>{{ t(`${ns}.before`) }}</span><pre><code>{{ serialized(block.value) }}</code></pre></section>
        <section class="trace-content-block__after"><span>{{ t(`${ns}.after`) }}</span><pre><code>{{ serialized(block.secondaryValue) }}</code></pre></section>
      </div>
      <!-- Markdown and code previews use the same sanitizers as existing local viewers. -->
      <div v-else-if="block.format === 'markdown' && typeof block.value === 'string'" class="trace-content-block__markdown" v-html="markdown" />
      <div v-else-if="block.format === 'code'" class="trace-content-block__code">
        <span v-if="block.language" class="trace-content-block__language">{{ block.language }}</span>
        <pre><code v-if="highlighted" class="hljs" v-html="highlighted" /><code v-else>{{ text }}</code></pre>
      </div>
      <TraceValue v-else :value="block.value" />
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import { renderArtifactMarkdown } from '@/utils/workbench/artifactPreview'
import { copyTextWithFallback } from '@/utils/browser'
import type { InspectorBlock } from '@/utils/traceInspector'
import TraceValue from './TraceValue.vue'

const props = defineProps<{ block: InspectorBlock }>()
const { t, te } = useI18n()
const ns = 'usageLogs.logs.traceInspectorView'
const expanded = ref(!props.block.collapsed)
const copyState = ref<'copy' | 'copied' | 'copyFailed'>('copy')
let copyTimer: ReturnType<typeof setTimeout> | undefined
const label = computed(() => props.block.labelKey && te(`${ns}.${props.block.labelKey}`) ? t(`${ns}.${props.block.labelKey}`) : props.block.label)
const text = computed(() => serialized(props.block.value))
const contentMeta = computed(() => {
  const value = props.block.value
  if (value != null && typeof value === 'object') return t(`${ns}.items`, { count: Object.keys(value).length })
  if (typeof value === 'string' && value.includes('\n')) return t(`${ns}.lines`, { count: value.split('\n').length })
  return t(`${ns}.characters`, { count: text.value.length })
})
const markdown = computed(() => props.block.format === 'markdown' ? renderArtifactMarkdown(text.value) : '')
const highlighted = computed(() => {
  const language = props.block.language
  if (props.block.format !== 'code' || !language || text.value.length > 30_000 || !hljs.getLanguage(language)) return ''
  return DOMPurify.sanitize(hljs.highlight(text.value, { language, ignoreIllegals: true }).value, { ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'] })
})
function serialized(value: unknown): string { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '' }
async function copy() {
  try {
    const content = props.block.format === 'diff' ? JSON.stringify({ before: props.block.value, after: props.block.secondaryValue }, null, 2) : text.value
    await copyTextWithFallback(content)
    copyState.value = 'copied'
  } catch { copyState.value = 'copyFailed' }
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => { copyState.value = 'copy' }, 1800)
}
onBeforeUnmount(() => { if (copyTimer) clearTimeout(copyTimer) })
</script>

<style scoped>
.trace-content-block { border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); min-width: 0; overflow: hidden; }
.trace-content-block__header { display: flex; align-items: center; gap: 8px; min-height: 37px; padding: 0 10px; background: color-mix(in srgb, var(--bg-elevated) 55%, var(--bg-surface)); }
.trace-content-block__heading { display: flex; align-items: center; flex: 1; min-width: 0; gap: 8px; padding: 9px 0; border: 0; background: transparent; color: var(--text); cursor: pointer; text-align: left; }
.trace-content-block__heading strong { font-size: 12px; overflow-wrap: anywhere; }.trace-content-block__heading > span { color: var(--text-muted); }
.trace-content-block__heading small { color: var(--text-dim); font-size: 10px; margin-left: auto; white-space: nowrap; }
.trace-content-block__copy { border: 0; background: transparent; cursor: pointer; color: var(--text-muted); font-size: 10px; padding: 4px; }
.trace-content-block__copy:hover { color: var(--accent); }
.trace-content-block__body { padding: 12px; max-height: 440px; overflow: auto; border-top: 1px solid var(--border); }
.trace-content-block__children { display: grid; gap: 10px; }
.trace-content-block__code { position: relative; }.trace-content-block__language { display: block; color: var(--text-dim); font-size: 10px; margin-bottom: 8px; }
.trace-content-block pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.65 var(--font-mono, monospace); color: var(--text); }
.trace-content-block pre code { background: transparent; padding: 0; font: inherit; }
.trace-content-block__markdown { color: var(--text); font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }
.trace-content-block__markdown :deep(p) { margin: 0 0 10px; }.trace-content-block__markdown :deep(p:last-child) { margin-bottom: 0; }
.trace-content-block__markdown :deep(h1), .trace-content-block__markdown :deep(h2), .trace-content-block__markdown :deep(h3) { font-size: 14px; margin: 14px 0 6px; }
.trace-content-block__markdown :deep(pre) { background: var(--bg-elevated); padding: 10px; border-radius: var(--radius-sm); white-space: pre-wrap; }
.trace-content-block__markdown :deep(table) { border-collapse: collapse; max-width: 100%; display: block; overflow: auto; }
.trace-content-block__markdown :deep(td), .trace-content-block__markdown :deep(th) { border: 1px solid var(--border); padding: 6px 8px; }
.trace-content-block__markdown :deep(a) { color: var(--accent); }.trace-content-block__markdown :deep(blockquote) { border-left: 2px solid var(--border); margin: 8px 0; padding-left: 12px; color: var(--text-muted); }
.trace-content-block__diff { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.trace-content-block__diff section { min-width: 0; border-radius: var(--radius-sm); padding: 10px; }
.trace-content-block__diff section > span { display: block; font-size: 10px; margin-bottom: 8px; }
.trace-content-block__before { background: color-mix(in srgb, var(--danger) 6%, var(--bg-surface)); border-left: 2px solid var(--danger); }
.trace-content-block__after { background: color-mix(in srgb, var(--accent-secondary) 6%, var(--bg-surface)); border-left: 2px solid var(--accent-secondary); }
@media (max-width: 620px) { .trace-content-block__diff { grid-template-columns: minmax(0, 1fr); } }
</style>
