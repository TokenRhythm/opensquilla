<template>
  <div
    class="activity-tool-details"
    :class="{ 'activity-tool-details--bounded': isBoundedDetail }"
  >
    <template v-if="isBoundedDetail">
      <button
        type="button"
        class="activity-tool-details__copy"
        :class="{
          'is-copied': copyState === 'copied',
          'is-error': copyState === 'error',
        }"
        data-share-control
        data-testid="activity-tool-detail-copy"
        :aria-label="copyLabel"
        :title="copyLabel"
        @click.stop="copyDetails"
      >
        <Icon :name="copyIcon" :size="13" aria-hidden="true" />
      </button>
      <span
        class="activity-tool-details__copy-status"
        aria-live="polite"
        aria-atomic="true"
      >{{ copyState === 'idle' ? '' : copyLabel }}</span>
      <div
        class="activity-tool-details__window"
        role="region"
        tabindex="0"
        :aria-label="detailActionLabel"
      >
        <section
          v-for="section in detailSections"
          :key="section.kind"
          class="activity-tool-details__section"
        >
          <div class="activity-tool-details__section-label">{{ section.label }}</div>
          <pre class="activity-tool-details__preview">{{ section.preview }}</pre>
        </section>
        <button
          type="button"
          class="activity-tool-details__view"
          data-share-control
          @click.stop="showRawDetails"
        >
          {{ t('shared.runTrace.viewFull') }}
        </button>
      </div>
      <div class="activity-tool-details__fade" aria-hidden="true"></div>
    </template>
    <div
      v-else-if="projection.lines.length"
      class="activity-tool-details__summary"
      :class="{ 'activity-tool-details__summary--interactive': projection.rawContent }"
    >
      <template v-for="(line, index) in projection.lines" :key="`${line.kind}:${index}`">
        <span
          v-if="index"
          class="activity-tool-details__separator"
          aria-hidden="true"
        >·</span>
        <span
          class="activity-tool-details__line"
          :class="`activity-tool-details__line--${line.kind}`"
        >
          {{ formatLine(line) }}
        </span>
      </template>
      <button
        v-if="projection.rawContent"
        type="button"
        class="activity-tool-details__hit-target"
        data-share-control
        :aria-label="detailActionLabel"
        :title="t('shared.runTrace.activityViewDetails')"
        @click.stop="showRawDetails"
      ></button>
    </div>
    <button
      v-else-if="projection.rawContent"
      type="button"
      class="activity-tool-details__fallback"
      data-share-control
      @click.stop="showRawDetails"
    >
      {{ t('shared.runTrace.activityViewDetails') }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'
import {
  projectActivityToolDetail,
  redactActivityDetail,
  type ActivityToolDetailLine,
} from '@/utils/chat/activityToolDetails'
import { copyTextWithFallback } from '@/utils/browser'

const DETAIL_WINDOW_CHAR_LIMIT = 360
const DETAIL_WINDOW_LINE_LIMIT = 6
const DETAIL_PREVIEW_CHAR_LIMIT = 6_000
const DETAIL_PREVIEW_LINE_LIMIT = 80

type ActivityDetailSection = {
  kind: 'input' | 'result' | 'error'
  label: string
  content: string
  preview: string
}

const props = defineProps<{
  call: ChatToolCallRenderItem
  label: string
  operationKey: string
}>()

const emit = defineEmits<{
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

const { locale, t } = useI18n()
const projection = computed(() =>
  projectActivityToolDetail(props.call, props.operationKey),
)
const copyState = ref<'idle' | 'copied' | 'error'>('idle')
let copyResetId: number | null = null

function asRecord(value: string): Record<string, unknown> | null {
  const source = String(value || '').trim()
  if (!source.startsWith('{')) return null
  try {
    const parsed = JSON.parse(source)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function recordString(
  record: Record<string, unknown> | null,
  keys: string[],
): string {
  if (!record) return ''
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function detailInputText(): string {
  const raw = String(props.call.inputRaw || props.call.inputPreview || '').trim()
  if (!raw) return ''
  if (props.operationKey === 'command.run' || props.operationKey === 'code.python') {
    const executable = recordString(asRecord(raw), [
      'command',
      'cmd',
      'code',
      'script',
    ])
    if (executable) return redactActivityDetail(executable)
  }
  return redactActivityDetail(raw)
}

function detailResultText(): string {
  return redactActivityDetail(
    String(props.call.result || props.call.resultPreview || '').trim(),
  )
}

function detailLineCount(value: string): number {
  return value ? value.split(/\r\n|\r|\n/).length : 0
}

function boundedPreview(value: string): string {
  const lines = value.split(/\r\n|\r|\n/)
  let preview = lines.slice(0, DETAIL_PREVIEW_LINE_LIMIT).join('\n')
  let truncated = lines.length > DETAIL_PREVIEW_LINE_LIMIT
  if (preview.length > DETAIL_PREVIEW_CHAR_LIMIT) {
    preview = preview.slice(0, DETAIL_PREVIEW_CHAR_LIMIT).trimEnd()
    truncated = true
  }
  return truncated ? `${preview}\n…` : preview
}

const detailSections = computed<ActivityDetailSection[]>(() => {
  const sections: ActivityDetailSection[] = []
  const input = detailInputText()
  const result = detailResultText()
  if (input) {
    sections.push({
      kind: 'input',
      label: t('shared.runTrace.sectionInput'),
      content: input,
      preview: boundedPreview(input),
    })
  }
  if (result) {
    const kind = props.call.isError || props.call.status === 'error'
      ? 'error'
      : 'result'
    sections.push({
      kind,
      label: t(
        kind === 'error'
          ? 'shared.runTrace.sectionError'
          : 'shared.runTrace.sectionResult',
      ),
      content: result,
      preview: boundedPreview(result),
    })
  }
  return sections
})

const isBoundedDetail = computed(() => {
  if (!projection.value.rawContent) return false
  const sections = detailSections.value
  const characters = sections.reduce(
    (total, section) => total + section.content.length,
    0,
  )
  const lines = sections.reduce(
    (total, section) => total + detailLineCount(section.content),
    0,
  )
  return characters > DETAIL_WINDOW_CHAR_LIMIT || lines > DETAIL_WINDOW_LINE_LIMIT
})

const copyLabel = computed(() => (
  copyState.value === 'copied'
    ? t('chat.copied')
    : copyState.value === 'error'
      ? t('chat.toast.copyFailed')
      : t('chat.copy')
))

const copyIcon = computed(() => (
  copyState.value === 'copied'
    ? 'check'
    : copyState.value === 'error'
      ? 'x'
      : 'copy'
))

function clearCopyReset() {
  if (copyResetId === null) return
  window.clearTimeout(copyResetId)
  copyResetId = null
}

function scheduleCopyReset() {
  clearCopyReset()
  copyResetId = window.setTimeout(() => {
    copyState.value = 'idle'
    copyResetId = null
  }, 1600)
}

async function copyDetails() {
  try {
    await copyTextWithFallback(redactActivityDetail(projection.value.rawContent))
    copyState.value = 'copied'
  } catch {
    copyState.value = 'error'
  }
  scheduleCopyReset()
}

onBeforeUnmount(clearCopyReset)

function formatNumber(value: number): string {
  return new Intl.NumberFormat(locale.value).format(value)
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '0 B'
  if (value < 1024) return `${formatNumber(value)} B`
  const units = ['KiB', 'MiB', 'GiB']
  let amount = value / 1024
  let unitIndex = 0
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024
    unitIndex += 1
  }
  const digits = amount >= 10 ? 0 : 1
  return `${new Intl.NumberFormat(locale.value, {
    maximumFractionDigits: digits,
  }).format(amount)} ${units[unitIndex]}`
}

function formatLine(line: ActivityToolDetailLine): string {
  if (line.kind === 'document-category') {
    const category = {
      DOCUMENT_PREVIEW_UNAVAILABLE: t('shared.runTrace.documentPreviewUnavailableCategory'),
      DOCUMENT_ACTION_RESULT_UNKNOWN: t('shared.runTrace.documentActionUnknownCategory'),
      DOCUMENT_EDIT_FAILED: t('shared.runTrace.documentEditFailedCategory'),
    }[line.category]
    return t('shared.runTrace.documentErrorCategory', {
      category,
      code: line.category,
    })
  }
  if (line.kind === 'document-message') {
    return t({
      'document.previewUnavailable': 'shared.runTrace.documentPreviewUnavailableMessage',
      'document.actionResultUnknown': 'shared.runTrace.documentActionUnknownMessage',
      'document.editFailed': 'shared.runTrace.documentEditFailedMessage',
    }[line.messageKey])
  }
  if (line.kind === 'document-retry') {
    return t({
      same_turn: 'shared.runTrace.documentRetrySameTurn',
      new_turn: 'shared.runTrace.documentRetryNewTurn',
      never: 'shared.runTrace.documentRetryNever',
    }[line.policy])
  }
  if (line.kind === 'document-next-action') {
    return t({
      retry: 'shared.runTrace.documentNextRetry',
      reinspect: 'shared.runTrace.documentNextReinspect',
      finalize_without_tools: 'shared.runTrace.documentNextFinalize',
      start_new_turn: 'shared.runTrace.documentNextNewTurn',
      stop: 'shared.runTrace.documentNextStop',
    }[line.action])
  }
  if (line.kind === 'bytes') {
    return t('shared.runTrace.activityBytesWritten', { size: formatBytes(line.bytes) })
  }
  if (line.kind === 'content-size') {
    return t('shared.runTrace.activityContentSize', {
      lines: formatNumber(line.lines),
      characters: formatNumber(line.characters),
    })
  }
  if (line.kind === 'exit-code') {
    return t('shared.runTrace.activityExitCode', { code: formatNumber(line.code) })
  }
  if (line.kind === 'published') {
    return t('shared.runTrace.activityPublished')
  }
  return line.text
}

const detailActionLabel = computed(() => {
  const action = t('shared.runTrace.activityViewDetails')
  const summary = projection.value.lines.map(formatLine).join(' · ')
  return summary ? `${action}: ${summary}` : action
})

function showRawDetails() {
  const detail = projection.value
  emit(
    'showResult',
    detail.rawContent,
    `${props.label} · ${t('shared.runTrace.activityDetailsTitle')}`,
    {
      toolName: props.call.name,
      inputRaw: props.call.inputRaw || props.call.inputPreview,
      section: detail.rawSection,
    },
  )
}
</script>

<style scoped>
.activity-tool-details {
  min-width: 0;
  padding: 0.0625rem 0 0.25rem;
  font-size: 0.75rem;
  line-height: 1.45;
}

.activity-tool-details--bounded {
  position: relative;
  display: flex;
  flex-direction: column;
  max-width: min(100%, 48rem);
  max-height: min(20rem, 52vh);
  padding: 0;
  border: 0;
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-md);
  overflow: hidden;
}

.activity-tool-details__window {
  min-height: 0;
  padding: 0.625rem 2.75rem 1.5rem 0.75rem;
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.activity-tool-details__window:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
}

.activity-tool-details__section {
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
}

.activity-tool-details__section + .activity-tool-details__section {
  margin-top: 0.75rem;
}

.activity-tool-details__section-label {
  margin-bottom: 0.25rem;
  color: var(--text-dim);
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.activity-tool-details__preview {
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-family: var(--font-mono);
  font-variant-ligatures: none;
  line-height: 1.55;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.activity-tool-details__copy {
  position: absolute;
  top: 0.25rem;
  right: 0.375rem;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2rem;
  height: 2rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.activity-tool-details__copy:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.activity-tool-details__copy:focus-visible,
.activity-tool-details__view:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.activity-tool-details__copy.is-copied {
  color: var(--ok);
}

.activity-tool-details__copy.is-error {
  color: var(--danger);
}

.activity-tool-details__copy-status {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.activity-tool-details__view {
  margin: 0.625rem 0 0;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.71875rem;
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, currentColor 40%, transparent);
  text-underline-offset: 0.15em;
}

.activity-tool-details__view:hover,
.activity-tool-details__view:focus-visible {
  color: var(--text);
  text-decoration-color: currentColor;
}

.activity-tool-details__fade {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  height: 1.5rem;
  background: linear-gradient(to bottom, transparent, var(--bg-elevated));
  pointer-events: none;
}

.activity-tool-details__summary {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 0.35rem;
  width: fit-content;
  max-width: 100%;
  min-width: 0;
  color: var(--text-muted);
}

.activity-tool-details__summary--interactive {
  cursor: pointer;
}

.activity-tool-details__line {
  flex: 0 0 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-tool-details__line--target {
  flex: 1 1 auto;
  color: var(--text-muted);
}

.activity-tool-details__line--code {
  flex: 1 1 auto;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-variant-ligatures: none;
}

.activity-tool-details__line--error {
  flex: 1 1 auto;
  overflow: visible;
  overflow-wrap: anywhere;
  white-space: normal;
  color: var(--text-muted);
}

.activity-tool-details__summary--interactive .activity-tool-details__line--target,
.activity-tool-details__summary--interactive .activity-tool-details__line--code {
  text-decoration: underline;
  text-decoration-style: dotted;
  text-decoration-thickness: 1px;
  text-decoration-color: color-mix(in srgb, var(--text) 40%, transparent);
  text-underline-offset: 0.15em;
}

.activity-tool-details__separator {
  flex: 0 0 auto;
  color: color-mix(in srgb, var(--text) 34%, transparent);
}

.activity-tool-details__hit-target {
  position: absolute;
  inset: -0.0625rem -0.125rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  cursor: pointer;
}

.activity-tool-details__summary--interactive:hover .activity-tool-details__line,
.activity-tool-details__summary--interactive:focus-within .activity-tool-details__line {
  color: var(--text-muted);
}

.activity-tool-details__summary--interactive:hover .activity-tool-details__line--target,
.activity-tool-details__summary--interactive:hover .activity-tool-details__line--code,
.activity-tool-details__summary--interactive:focus-within .activity-tool-details__line--target,
.activity-tool-details__summary--interactive:focus-within .activity-tool-details__line--code {
  color: var(--text);
  text-decoration-color: currentColor;
}

.activity-tool-details__summary--interactive:hover .activity-tool-details__line--error,
.activity-tool-details__summary--interactive:focus-within .activity-tool-details__line--error {
  color: var(--text);
}

.activity-tool-details__hit-target:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.activity-tool-details__fallback {
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.71875rem;
  line-height: 1.45;
  /* This button is the only path to the raw viewer for tools that expose no
     safe inline detail, and it sits where the detail text would be. Without a
     resting underline it renders as muted body copy and reads as the row's
     content rather than the control that opens it — hover-only affordance also
     leaves touch users with nothing. */
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, currentColor 40%, transparent);
  text-underline-offset: 0.15em;
  transition:
    color var(--dur-fast) var(--ease-standard),
    text-decoration-color var(--dur-fast) var(--ease-standard);
}

.activity-tool-details__fallback:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.activity-tool-details__fallback:hover,
.activity-tool-details__fallback:focus-visible {
  color: var(--text);
  text-decoration-color: currentColor;
}

@media (prefers-reduced-motion: reduce) {
  .activity-tool-details__fallback {
    transition: none;
  }
}

@media (pointer: coarse) {
  .activity-tool-details__copy {
    width: 2.75rem;
    height: 2.75rem;
  }
}
</style>
