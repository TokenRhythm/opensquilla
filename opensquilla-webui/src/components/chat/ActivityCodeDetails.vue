<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import Icon from '@/components/Icon.vue'
import { boundedTextPreviews } from '@/utils/chat/toolContentPreview'
import {
  hasActivityToolCodeContent,
  isOmittedActivityPayload,
  type ActivityToolCodeView,
} from '@/utils/chat/activityToolDetails'

const props = withDefaults(defineProps<{
  view: ActivityToolCodeView
  copyState?: 'idle' | 'copied' | 'error'
  canOpenCodeView?: boolean
  truncated?: boolean
  downloadError?: boolean
  full?: boolean
}>(), { copyState: 'idle', canOpenCodeView: false, full: false })
const emit = defineEmits<{ copy: []; download: []; openCodeView: []; reachEnd: [] }>()
const viewport = ref<HTMLElement | null>(null)
function checkScrollEnd() {
  const element = viewport.value
  if (!props.full && !isTruncated.value && element && element.clientHeight > 0
    && element.scrollTop + element.clientHeight >= element.scrollHeight - 96) emit('reachEnd')
}
function onWheel(event: WheelEvent) { if (event.deltaY > 0) checkScrollEnd() }
function onPageKey(event: KeyboardEvent) {
  if (['ArrowDown', 'PageDown', 'End'].includes(event.key) || (event.key === ' ' && !event.shiftKey)) checkScrollEnd()
}
const { t } = useI18n()
const title = computed(() => props.view.kind === 'command'
  ? 'Shell' : props.view.path?.replace(/\\/g, '/').split('/').pop() || t('shared.runTrace.sectionResult'))
const hasContent = computed(() => hasActivityToolCodeContent(props.view))
const boundedParts = computed(() => {
  const value = props.view.kind === 'command' || props.view.error ? props.view.output : props.view.content
  const command = props.view.kind === 'command' && !isOmittedActivityPayload(props.view.command) ? props.view.command || '' : ''
  return boundedTextPreviews([command, isOmittedActivityPayload(value) ? '' : value || ''])
})
const source = computed(() => boundedParts.value.previews[1]?.content || '')
const command = computed(() => boundedParts.value.previews[0]?.content || '')
const isTruncated = computed(() => props.truncated || props.view.truncated || boundedParts.value.truncated)
const actionLabel = computed(() => isTruncated.value
  ? t(props.downloadError ? 'shared.runTrace.downloadContentFailed' : props.view.partial ? 'chat.download' : 'shared.runTrace.downloadFullContent') : copyLabel.value)
const exitCode = computed(() => props.view.exitCode !== undefined && !/^\s*exit[_ ]code\s*[:=]/im.test(source.value)
  ? `exit_code=${props.view.exitCode}` : '')
const copyLabel = computed(() => props.copyState === 'copied' ? t('chat.copied')
  : props.copyState === 'error' ? t('chat.toast.copyFailed')
    : t(props.view.partial ? 'shared.runTrace.copyPreview' : 'chat.copy'))
const openLabel = computed(() => t(props.view.kind === 'edit'
  ? 'shared.runTrace.codeViewDiff' : 'shared.runTrace.codeViewSource'))
const highlightedContent = computed(() => {
  const text = source.value
  const language = props.view.language
  const escaped = () => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // Bound the source before highlighting or splitting it into DOM nodes.
  if (isTruncated.value || text.length > 100_000 || !language || !hljs.getLanguage(language)) return escaped()
  try {
    return DOMPurify.sanitize(hljs.highlight(text, { language, ignoreIllegals: true }).value, {
      ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'],
    })
  } catch { return escaped() }
})
// The retained diff is the authority. No fabricated line numbers or hunks.
const diffLines = computed(() => source.value.split('\n').map(text => ({
  text,
  kind: /^(?:--- |\+\+\+ |diff |index |\\ No newline|@@)/.test(text) ? 'meta'
    : text.startsWith('+') ? 'added' : text.startsWith('-') ? 'removed' : 'context',
})))
</script>

<template>
  <section v-if="hasContent" class="activity-code-details" :class="{ 'activity-code-details--full': full }"
    :data-kind="view.kind" data-testid="activity-code-details">
    <header v-if="!full" class="activity-code-details__header">
      <span class="activity-code-details__title" :title="view.path">{{ title }}</span>
      <div class="activity-code-details__actions">
        <button v-if="canOpenCodeView && view.kind !== 'command' && !view.error" type="button"
          class="activity-code-details__action activity-code-details__open" data-share-control data-testid="activity-code-open"
          :aria-label="openLabel" :title="openLabel" @click.stop="emit('openCodeView')">
          {{ openLabel }}
        </button>
        <button type="button" class="activity-code-details__action" data-share-control
          :data-testid="isTruncated ? 'activity-tool-detail-download' : 'activity-tool-detail-copy'"
          :aria-label="actionLabel" :title="actionLabel" @click.stop="isTruncated ? emit('download') : emit('copy')">
          <Icon :name="isTruncated ? (downloadError ? 'x' : 'download') : copyState === 'copied' ? 'check' : copyState === 'error' ? 'x' : 'copy'" :size="14" aria-hidden="true" />
        </button>
      </div>
      <span class="activity-code-details__sr" aria-live="polite" aria-atomic="true">{{ isTruncated ? (downloadError ? actionLabel : '') : copyState === 'idle' ? '' : copyLabel }}</span>
    </header>
    <div ref="viewport" class="activity-code-details__viewport" role="region" tabindex="0" :aria-label="title"
      @scroll.passive="checkScrollEnd" @wheel.passive="onWheel" @keydown="onPageKey">
      <template v-if="view.kind === 'command'">
        <pre v-if="command" class="activity-code-details__command"><code><span class="activity-code-details__prompt" aria-hidden="true">$ </span>{{ command }}</code></pre>
        <pre v-if="source" class="activity-code-details__output"><code>{{ source }}</code></pre>
        <pre v-if="exitCode" class="activity-code-details__exit"><code>{{ exitCode }}</code></pre>
      </template>
      <pre v-else-if="view.error && source" class="activity-code-details__output"><code>{{ source }}</code></pre>
      <pre v-else-if="view.kind === 'edit' && source" class="activity-code-details__diff"><code><span
        v-for="(line, index) in diffLines" :key="index"
        class="activity-code-details__diff-line" :class="`activity-code-details__diff-line--${line.kind}`"
      >{{ line.text }}{{ index < diffLines.length - 1 ? '\n' : '' }}</span></code></pre>
      <pre v-else-if="source" class="activity-code-details__source"><code v-html="highlightedContent"></code></pre>
      <p v-else-if="!view.error && (view.kind === 'read' || view.kind === 'write')" class="activity-code-details__note">{{ t('shared.runTrace.codeEmptyFile') }}</p>
    </div>
    <p v-if="isTruncated || (view.partial && source)" class="activity-code-details__note activity-code-details__preview-note"
      :data-testid="isTruncated ? 'tool-content-truncated' : undefined">{{ t(view.partial ? 'shared.runTrace.codePartial' : 'shared.runTrace.previewTruncated') }}</p>
  </section>
</template>

<style scoped>
.activity-code-details {
  min-width: 0;
  margin: 0.375rem 0 0.5rem;
  padding: 0.875rem;
  overflow: hidden;
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-control);
  background: var(--code-block-bg);
  color: var(--text-muted);
  font-size: 0.8125rem;
  font-weight: 400;
  line-height: 1.375rem;
  box-shadow: none;
}
.activity-code-details__header { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; min-height: 1.5rem; margin-bottom: 0.625rem; }
.activity-code-details__title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); }
.activity-code-details__actions { display: flex; flex: 0 0 auto; gap: 0.25rem; }
.activity-code-details__action { display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; padding: 0; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.activity-code-details__action:hover { color: var(--text); background: var(--bg-hover); }
.activity-code-details__open { width: auto; padding: 0 0.375rem; font: inherit; opacity: 0; }
.activity-code-details:hover .activity-code-details__open,
.activity-code-details:focus-within .activity-code-details__open { opacity: 1; }
.activity-code-details__viewport { min-width: 0; width: 100%; max-height: min(20rem, 45vh); overflow: auto; overscroll-behavior: contain; scrollbar-width: thin; scrollbar-color: var(--border-strong) transparent; color: var(--text); }
.activity-code-details pre,
.activity-code-details code { border: 0; border-radius: 0; background: transparent; box-shadow: none; font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 400; line-height: 1.375rem; text-align: left; tab-size: 2; }
.activity-code-details pre { width: max-content; min-width: 100%; box-sizing: border-box; margin: 0; padding: 0; white-space: pre; }
.activity-code-details code { padding: 0; color: inherit; }
.activity-code-details pre.activity-code-details__command { width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }
.activity-code-details__command + .activity-code-details__output,
.activity-code-details pre + .activity-code-details__exit { margin-top: 0.625rem; }
.activity-code-details__prompt { color: var(--text-dim); user-select: none; }
.activity-code-details__diff-line--added { background: color-mix(in srgb, var(--ok) 8%, transparent); color: var(--text); }
.activity-code-details__diff-line--removed { background: color-mix(in srgb, var(--danger) 8%, transparent); color: var(--text); }
.activity-code-details__diff-line--meta { color: var(--text-muted); }
.activity-code-details__note { margin: 0; color: var(--text-muted); }
.activity-code-details__preview-note { margin-top: 0.625rem; font-size: 0.75rem; }
pre + .activity-code-details__note { margin-top: 0.625rem; }
.activity-code-details__action:focus-visible,
.activity-code-details__viewport:focus-visible { outline: none; box-shadow: var(--focus-ring-inset); }
.activity-code-details--full { margin: 0; padding: 0; overflow: visible; border: 0; border-radius: 0; background: transparent; }
.activity-code-details--full .activity-code-details__viewport { max-height: none; overflow: visible; }
.activity-code-details__sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.activity-code-details :deep(.hljs-comment), .activity-code-details :deep(.hljs-quote) { color: var(--syntax-comment); }
.activity-code-details :deep(.hljs-keyword), .activity-code-details :deep(.hljs-selector-tag) { color: var(--syntax-keyword); }
.activity-code-details :deep(.hljs-string), .activity-code-details :deep(.hljs-regexp) { color: var(--syntax-string); }
.activity-code-details :deep(.hljs-number), .activity-code-details :deep(.hljs-literal) { color: var(--syntax-literal); }
.activity-code-details :deep(.hljs-title), .activity-code-details :deep(.hljs-name) { color: var(--syntax-title); }
.activity-code-details :deep(.hljs-attr), .activity-code-details :deep(.hljs-attribute) { color: var(--syntax-attr); }
@media (pointer: coarse) { .activity-code-details__action { width: 2.75rem; height: 2.75rem; } }
@media (hover: none), (pointer: coarse) { .activity-code-details__open { width: auto; opacity: 1; } }
</style>
