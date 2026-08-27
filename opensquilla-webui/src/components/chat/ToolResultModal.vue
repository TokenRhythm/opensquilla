<template>
  <div v-if="open" class="tool-sheet-overlay" @click.self="$emit('close')">
    <aside ref="rootRef" class="tool-sheet" role="dialog" aria-modal="true" :aria-label="dialogLabel">
      <header class="tool-sheet__header">
        <div class="tool-sheet__titles">
          <p v-if="filePath" class="tool-sheet__operation">{{ title }}</p>
          <h3 class="tool-sheet__title">{{ displayTitle }}</h3>
          <div class="tool-sheet__meta">
            <span>{{ contentMeta }}</span>
            <span v-if="filePath" class="tool-sheet__path" :title="filePath">{{ filePath }}</span>
          </div>
        </div>
        <div class="tool-sheet__actions">
          <button
            v-if="treeHtml"
            type="button"
            class="btn btn--ghost tool-sheet__mode"
            @click="rawMode = !rawMode"
          >
            {{ rawMode ? t('chat.toolModal.tree') : t('chat.toolModal.raw') }}
          </button>
          <button
            v-if="!treeHtml || rawMode"
            type="button"
            class="btn btn--ghost tool-sheet__mode"
            :aria-pressed="wrapLines"
            @click="wrapLines = !wrapLines"
          >
            {{ wrapLines ? t('chat.toolModal.preserveLines') : t('chat.toolModal.wrapLines') }}
          </button>
          <button
            type="button"
            class="btn btn--icon btn--ghost tool-sheet__copy"
            :class="{ 'is-copied': copied }"
            :title="copyLabel"
            :aria-label="copyLabel"
            @click="copyContent"
          >
            <Icon :name="copied ? 'check' : 'copy'" :size="16" />
          </button>
          <button ref="closeBtn" type="button" class="btn btn--icon btn--ghost" :title="t('common.close')" :aria-label="t('common.close')" @click="$emit('close')">
            <Icon name="x" :size="16" />
          </button>
        </div>
      </header>

      <div class="tool-sheet__body">
        <div
          v-if="treeHtml && !rawMode"
          class="tool-sheet__tree"
          role="region"
          tabindex="0"
          :aria-label="viewerRegionLabel"
          v-html="treeHtml"
        />
        <div
          v-else
          class="tool-sheet__code"
          :class="{ 'tool-sheet__code--wrap': wrapLines }"
          role="region"
          tabindex="0"
          :aria-label="viewerRegionLabel"
        >
          <pre v-if="showLineNumbers" class="tool-sheet__line-numbers" aria-hidden="true">{{ lineNumberText }}</pre>
          <pre class="tool-sheet__pre"><!-- eslint-disable-next-line vue/no-v-html -- Highlight.js escapes source and DOMPurify allow-lists its generated spans. --><code v-if="highlightedContent" class="hljs" :class="`language-${viewerLanguage}`" v-html="highlightedContent" /><code v-else>{{ content }}</code></pre>
        </div>
      </div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import type { ToolResultContext } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'

const { t } = useI18n()
const { pushToast } = useToasts()

const props = defineProps<{
  open: boolean
  title: string
  content: string
  context?: ToolResultContext
}>()

const emit = defineEmits<{
  close: []
}>()

const HIGHLIGHT_MAX_CHARS = 30_000
const MAX_LINE_NUMBER_COUNT = 10_000
const AUTO_OPEN_DEPTH = 2
const MAX_TREE_SOURCE_CHARS = 300000
const MAX_LEAF_STRING_CHARS = 2000
const MAX_TREE_NODES = 4000

const EXTENSION_LANGUAGE: Record<string, string> = {
  bash: 'bash',
  css: 'css',
  htm: 'xml',
  html: 'xml',
  ini: 'ini',
  java: 'java',
  js: 'javascript',
  json: 'json',
  jsonc: 'json',
  jsx: 'javascript',
  md: 'markdown',
  markdown: 'markdown',
  py: 'python',
  sh: 'bash',
  toml: 'ini',
  ts: 'typescript',
  tsx: 'typescript',
  xml: 'xml',
  yaml: 'yaml',
  yml: 'yaml',
  zsh: 'bash',
}

const LANGUAGE_LABEL: Record<string, string> = {
  bash: 'Shell',
  css: 'CSS',
  diff: 'Diff',
  ini: 'INI',
  java: 'Java',
  javascript: 'JavaScript',
  json: 'JSON',
  markdown: 'Markdown',
  plaintext: 'Text',
  python: 'Python',
  typescript: 'TypeScript',
  xml: 'HTML',
  yaml: 'YAML',
}

const rawMode = ref(false)
const wrapLines = ref(false)
const copied = ref(false)
const closeBtn = ref<HTMLButtonElement | null>(null)
const rootRef = ref<HTMLElement | null>(null)
let copiedResetId: ReturnType<typeof setTimeout> | null = null

// Trap Tab focus inside the sheet, close on Escape, and restore focus to the
// invoker on close; initial focus lands on the close button.
useDialogA11y(rootRef, toRef(props, 'open'), () => emit('close'), { initialFocus: closeBtn })

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function filePathFromToolInput(inputRaw?: string): string {
  if (!inputRaw) return ''
  try {
    const record = asRecord(JSON.parse(inputRaw))
    return typeof record?.path === 'string' ? record.path : ''
  } catch {
    return ''
  }
}

function filenameFromPath(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || ''
}

function extensionFromPath(path: string): string {
  const match = filenameFromPath(path).match(/\.([A-Za-z0-9]+)$/)
  return match?.[1].toLowerCase() || ''
}

function inferredLanguage(text: string, path: string): string {
  const extension = extensionFromPath(path)
  if (extension && EXTENSION_LANGUAGE[extension]) return EXTENSION_LANGUAGE[extension]

  const trimmed = text.trim()
  if (!trimmed) return 'plaintext'
  if (/^[{[]/.test(trimmed)) {
    try {
      JSON.parse(trimmed)
      return 'json'
    } catch {
      // Fall through: a partial streamed JSON value should remain plain text.
    }
  }
  if (/^(---\s*$|[A-Za-z0-9_.-]+:\s)/m.test(trimmed)) return 'yaml'
  if (/^(#{1,6}\s|```|[-*+]\s)/m.test(trimmed)) return 'markdown'
  if (/^(#!|\$\s)/m.test(trimmed)) return 'bash'
  return 'plaintext'
}

const isReadFileSection = computed(() =>
  props.context?.toolName?.endsWith('read_file') && props.context?.section !== 'input',
)
const filePath = computed(() =>
  isReadFileSection.value ? filePathFromToolInput(props.context?.inputRaw) : '',
)
const fileName = computed(() => filenameFromPath(filePath.value))
const displayTitle = computed(() => fileName.value || props.title)
const dialogLabel = computed(() => fileName.value ? `${fileName.value} · ${props.title}` : props.title)
const contentLines = computed(() => props.content ? props.content.split(/\r\n|\r|\n/) : [])
const viewerLanguage = computed(() => {
  if (props.context?.format === 'diff') return 'diff'
  if (isReadFileSection.value && props.context?.section === 'error') return 'plaintext'
  const languagePath = props.context?.section === 'result' ? filePath.value : ''
  return inferredLanguage(props.content, languagePath)
})
const viewerLanguageLabel = computed(() => LANGUAGE_LABEL[viewerLanguage.value] || 'Text')
const contentMeta = computed(() => t('chat.toolModal.contentMeta', {
  type: viewerLanguageLabel.value,
  lines: contentLines.value.length.toLocaleString(),
  chars: props.content.length.toLocaleString(),
}))
const viewerRegionLabel = computed(() => `${displayTitle.value} · ${contentMeta.value}`)
const showLineNumbers = computed(() =>
  !wrapLines.value
    && contentLines.value.length > 0
    && contentLines.value.length <= MAX_LINE_NUMBER_COUNT,
)
const lineNumberText = computed(() =>
  Array.from({ length: contentLines.value.length }, (_, index) => String(index + 1)).join('\n'),
)
const copyLabel = computed(() => copied.value ? t('chat.copied') : t('chat.copy'))

const highlightedContent = computed(() => {
  const content = props.content
  const language = viewerLanguage.value
  if (!content || content.length > HIGHLIGHT_MAX_CHARS || language === 'plaintext' || !hljs.getLanguage(language)) return ''
  try {
    const highlighted = hljs.highlight(content, { language, ignoreIllegals: true }).value
    return DOMPurify.sanitize(highlighted, { ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'] })
  } catch {
    return ''
  }
})

async function copyContent() {
  try {
    await copyTextWithFallback(props.content)
    copied.value = true
    if (copiedResetId) clearTimeout(copiedResetId)
    copiedResetId = setTimeout(() => {
      copied.value = false
      copiedResetId = null
    }, 1600)
  } catch {
    copied.value = false
    pushToast(t('chat.toast.copyFailed'), { tone: 'danger' })
  }
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] as string
  ))
}

function leafHtml(value: unknown): string {
  if (typeof value === 'string') {
    const shown = value.length > MAX_LEAF_STRING_CHARS
      ? `${value.slice(0, MAX_LEAF_STRING_CHARS)}… (${value.length} chars)`
      : value
    return `<span class="jt-string">"${escapeHtml(shown)}"</span>`
  }
  if (value === null) return '<span class="jt-null">null</span>'
  return `<span class="jt-${typeof value}">${escapeHtml(String(value))}</span>`
}

function nodeHtml(value: unknown, key: string, depth: number, budget: { left: number }): string {
  if (budget.left-- <= 0) return ''
  const keyHtml = key === '' ? '' : `<span class="jt-key">${escapeHtml(key)}</span><span class="jt-colon">: </span>`
  if (value === null || typeof value !== 'object') {
    return `<div class="jt-row">${keyHtml}${leafHtml(value)}</div>`
  }
  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) {
    return `<div class="jt-row">${keyHtml}<span class="jt-badge">${Array.isArray(value) ? '[]' : '{}'}</span></div>`
  }
  const badge = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`
  const children = entries.map(([childKey, childValue]) => nodeHtml(childValue, childKey, depth + 1, budget)).join('')
  const truncated = budget.left <= 0 ? '<div class="jt-row jt-truncated">…</div>' : ''
  const openAttr = depth < AUTO_OPEN_DEPTH ? ' open' : ''
  return `<details class="jt-node"${openAttr}><summary class="jt-summary">${keyHtml}<span class="jt-badge">${badge}</span></summary><div class="jt-children">${children}${truncated}</div></details>`
}

// Pre-rendered, fully escaped fold tree; empty string means "not JSON".
const treeHtml = computed(() => {
  const text = (props.content || '').trim()
  if (!text || text.length > MAX_TREE_SOURCE_CHARS) return ''
  if (!/^[[{]/.test(text)) return ''
  try {
    const parsed: unknown = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object') return ''
    return nodeHtml(parsed, '', 0, { left: MAX_TREE_NODES })
  } catch {
    return ''
  }
})

// Each new viewing session starts in the compact tree/raw default, with
// original line widths restored. Copy feedback must not leak to another file.
watch(() => props.open, open => {
  if (!open) return
  rawMode.value = false
  wrapLines.value = false
  copied.value = false
  if (copiedResetId) {
    clearTimeout(copiedResetId)
    copiedResetId = null
  }
})

onBeforeUnmount(() => {
  if (copiedResetId) clearTimeout(copiedResetId)
})
</script>

<style scoped>
.tool-sheet-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: var(--scrim);
  display: flex;
  justify-content: flex-end;
}

.tool-sheet {
  background: var(--bg-surface);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  height: 100%;
  width: min(780px, 76vw);
  animation: toolSheetIn var(--dur-base) var(--ease-out);
}

@keyframes toolSheetIn {
  from { transform: translateX(24px); opacity: 0.4; }
  to { transform: translateX(0); opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .tool-sheet {
    animation: none;
  }
}

.tool-sheet__header {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
}

.tool-sheet__titles {
  flex: 1 1 12rem;
  min-width: 0;
}

.tool-sheet__operation {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 600;
  margin: 0 0 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-sheet__title {
  color: var(--text);
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-sheet__meta {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: 2px var(--sp-2);
  margin-top: 2px;
  min-width: 0;
}

.tool-sheet__path {
  color: var(--text-dim);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-sheet__actions {
  align-items: center;
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
}

.tool-sheet__mode {
  font-size: var(--fs-xs);
}

.tool-sheet__copy.is-copied {
  color: var(--ok);
}

.tool-sheet__body {
  background: var(--bg);
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  padding: var(--sp-3);
}

.tool-sheet__code,
.tool-sheet__tree {
  background: var(--bg-surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  flex: 1;
  min-height: 0;
}

.tool-sheet__code {
  display: flex;
  overflow: auto;
}

.tool-sheet__line-numbers {
  background: var(--bg-elevated);
  border-right: 1px solid var(--hairline);
  color: var(--text-dim);
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
  margin: 0;
  min-width: 3.5ch;
  padding: var(--sp-3) var(--sp-2);
  position: sticky;
  left: 0;
  text-align: right;
  user-select: none;
  z-index: 1;
}

.tool-sheet__pre {
  color: var(--text);
  flex: 1;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
  margin: 0;
  min-width: 0;
  padding: var(--sp-3);
  white-space: pre;
}

.tool-sheet__pre code {
  color: inherit;
  font: inherit;
}

.tool-sheet__code:not(.tool-sheet__code--wrap) .tool-sheet__pre {
  min-width: max-content;
}

.tool-sheet__code--wrap .tool-sheet__pre {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.tool-sheet__tree {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
  overflow: auto;
  padding: var(--sp-3);
}

.tool-sheet__tree :deep(.jt-node) {
  margin: 0;
}

.tool-sheet__tree :deep(.jt-summary) {
  color: var(--text-muted);
  cursor: pointer;
  list-style-position: outside;
}

.tool-sheet__tree :deep(.jt-summary:hover) {
  color: var(--text);
}

.tool-sheet__tree :deep(.jt-children) {
  border-left: 1px solid var(--hairline);
  margin-left: var(--sp-4);
  padding-left: var(--sp-2);
}

.tool-sheet__tree :deep(.jt-key),
.tool-sheet__code :deep(.hljs-attr),
.tool-sheet__code :deep(.hljs-attribute),
.tool-sheet__code :deep(.hljs-variable),
.tool-sheet__code :deep(.hljs-template-variable),
.tool-sheet__code :deep(.hljs-type),
.tool-sheet__code :deep(.hljs-property) {
  color: var(--syntax-attr);
}

.tool-sheet__tree :deep(.jt-string),
.tool-sheet__code :deep(.hljs-string),
.tool-sheet__code :deep(.hljs-regexp),
.tool-sheet__code :deep(.hljs-addition) {
  color: var(--syntax-string);
}

.tool-sheet__tree :deep(.jt-number),
.tool-sheet__tree :deep(.jt-boolean),
.tool-sheet__code :deep(.hljs-number),
.tool-sheet__code :deep(.hljs-literal),
.tool-sheet__code :deep(.hljs-symbol),
.tool-sheet__code :deep(.hljs-bullet) {
  color: var(--syntax-literal);
}

.tool-sheet__tree :deep(.jt-null),
.tool-sheet__tree :deep(.jt-badge),
.tool-sheet__tree :deep(.jt-truncated),
.tool-sheet__code :deep(.hljs-comment),
.tool-sheet__code :deep(.hljs-quote),
.tool-sheet__code :deep(.hljs-meta),
.tool-sheet__code :deep(.hljs-doctag) {
  color: var(--syntax-comment);
}

.tool-sheet__code :deep(.hljs-keyword),
.tool-sheet__code :deep(.hljs-selector-tag),
.tool-sheet__code :deep(.hljs-built_in),
.tool-sheet__code :deep(.hljs-template-tag) {
  color: var(--syntax-keyword);
}

.tool-sheet__code :deep(.hljs-title),
.tool-sheet__code :deep(.hljs-section),
.tool-sheet__code :deep(.hljs-name) {
  color: var(--syntax-title);
}

@media (max-width: 700px) {
  .tool-sheet {
    width: 100%;
  }

  .tool-sheet__header {
    align-items: flex-start;
  }

  .tool-sheet__body {
    padding: var(--sp-2);
  }
}
</style>
