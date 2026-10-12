<template>
  <div
    class="activity-tool-details"
    :class="{ 'activity-tool-details--bounded': isBoundedDetail }"
    :aria-busy="fileSnapshot.loading.value || undefined"
  >
    <div
      v-if="executionIo.kind !== 'unknown'"
      class="activity-tool-details__execution-io"
      :class="`activity-tool-details__execution-io--${executionIo.kind}`"
      role="status"
    >
      {{ executionIoLabel }}
      <span v-if="executionIo.kind === 'fallback' && executionIo.fallbackReason" class="activity-tool-details__execution-io-reason">
        {{ executionIo.fallbackReason }}
      </span>
    </div>
    <p v-if="inlineLog.error.value" class="activity-tool-details__log-status" role="status">
      {{ t(inlineLog.error.value === 'not-ready' ? 'chat.toolModal.logPending' : 'chat.toolModal.logReadFailed') }}
      <button type="button" data-share-control @click.stop="inlineLog.retry()">{{ t('chat.toolModal.retry') }}</button>
    </p>
    <p v-if="fileSnapshot.error.value" class="activity-tool-details__log-status" role="status">
      {{ t('workspaceReference.unavailable') }}
      <button type="button" data-share-control @click.stop="fileSnapshot.retry()">{{ t('chat.toolModal.retry') }}</button>
    </p>
    <p v-if="downloadError" class="activity-tool-details__log-status" role="status">{{ t('shared.runTrace.downloadContentFailed') }}</p>
    <template v-if="projection.codeView">
      <ActivityCodeDetails
        :view="projection.codeView"
        :copy-state="copyState"
        :download-error="downloadError"
        :can-open-code-view="canOpenCodeView"
        @copy="copyDetails"
        @download="downloadDetails"
        @open-code-view="openCodeView"
        @reach-end="inlineLog.loadMore()"
      />
    </template>
    <template v-else-if="isBoundedDetail">
      <button
        type="button"
        class="activity-tool-details__copy"
        :class="{
          'is-copied': copyState === 'copied',
          'is-error': copyState === 'error',
        }"
        data-share-control
        :data-testid="detailTruncated ? 'activity-tool-detail-download' : 'activity-tool-detail-copy'"
        :aria-label="detailTruncated ? detailDownloadLabel : copyLabel"
        :title="detailTruncated ? detailDownloadLabel : copyLabel"
        @click.stop="detailTruncated ? downloadDetails() : copyDetails()"
      >
        <Icon :name="detailTruncated ? 'download' : copyIcon" :size="13" aria-hidden="true" />
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
        @scroll.passive="checkLogScrollEnd"
        @wheel.passive="onLogWheel"
        @keydown="onLogPageKey"
      >
        <section
          v-for="section in visibleSections"
          :key="section.kind"
          class="activity-tool-details__section"
        >
          <div class="activity-tool-details__section-label">{{ section.label }}</div>
          <pre class="activity-tool-details__preview">{{ section.content }}</pre>
        </section>
      </div>
      <p v-if="detailTruncated || detailPartial" class="activity-tool-details__truncated"
        :data-testid="detailTruncated ? 'tool-content-truncated' : undefined">{{ detailPartial ? detailPartialLabel : t('shared.runTrace.previewTruncated') }}</p>
    </template>
    <div
      v-else-if="projection.lines.length"
      class="activity-tool-details__summary"
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { HISTORY_DETAILS_READY } from '@/modules/historyDetails'
import { useI18n } from 'vue-i18n'
import { getActivePinia } from 'pinia'
import { useInlineExecutionLog } from '@/composables/chat/useInlineExecutionLog'
import { useInlineFileSnapshot } from '@/composables/chat/useInlineFileSnapshot'
import { normalizeToolFileSnapshot } from '@/utils/chat/toolFileSnapshot'
import { boundedTextPreviews } from '@/utils/chat/toolContentPreview'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import Icon from '@/components/Icon.vue'
import ActivityCodeDetails from './ActivityCodeDetails.vue'
import { useAppStore } from '@/stores/app'
import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'
import { projectExecutionIoForCall } from '@/utils/chat/executionIo'
import {
  projectActivityToolDetail,
  activityToolCodeContent,
  canReadActivityExecutionLog,
  canReadActivityFileSnapshot,
  redactActivityDetail,
  type ActivityToolDetailLine,
} from '@/utils/chat/activityToolDetails'
import { copyTextWithFallback, downloadText } from '@/utils/browser'
import { canOpenToolHistoryFile, downloadToolHistoryContent, openToolHistoryFile } from '@/workbench/toolHistoryFileItems'

type ActivityDetailSection = {
  kind: 'input' | 'result' | 'error' | 'changes'
  label: string
  content: string
}

const props = defineProps<{
  call: ChatToolCallRenderItem
  label: string
  operationKey: string
  sessionKey?: string
}>()

defineEmits<{
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

const { locale, t } = useI18n()
const historyDetailsReady = inject(HISTORY_DETAILS_READY, undefined)
const pinia = getActivePinia()
const appStore = pinia ? useAppStore(pinia) : undefined
const contentAccess = inject(ARTIFACT_WORKBENCH_KEY, null)?.content
const baseProjection = computed(() =>
  projectActivityToolDetail(props.call, props.operationKey),
)
const inlineLog = useInlineExecutionLog({
  sessionKey: () => props.sessionKey,
  handle: () => props.call.executionLogHandle,
  toolId: () => props.call.renderKey || props.call.toolId,
  enabled: () => canReadActivityExecutionLog(props.call, props.operationKey),
})
const fileSnapshot = useInlineFileSnapshot({
  sessionKey: () => props.sessionKey,
  toolId: () => props.call.renderKey || props.call.toolId,
  snapshot: () => normalizeToolFileSnapshot(props.call.fileSnapshot),
  enabled: () => canReadActivityFileSnapshot(props.call, props.operationKey),
})
const projection = computed(() => {
  const detail = baseProjection.value
  if (detail.codeView && fileSnapshot.required.value && fileSnapshot.content.value !== null) {
    const content = redactActivityDetail(fileSnapshot.content.value)
    return { ...detail, rawContent: content, codeView: { ...detail.codeView, content, unavailable: false, truncated: fileSnapshot.truncated.value } }
  }
  if (detail.codeView?.kind !== 'command' || inlineLog.content.value === null) return detail
  return { ...detail, codeView: { ...detail.codeView,
    output: redactActivityDetail(inlineLog.content.value), unavailable: false, partial: inlineLog.partial.value,
    truncated: inlineLog.truncated.value,
  } }
})
const canOpenCodeView = computed(() => Boolean(
  appStore?.features.artifactWorkbench && props.sessionKey?.trim() && props.call.toolId?.trim()
  && !props.call.isRunning && props.call.status === 'success'
  && canOpenToolHistoryFile(projection.value.codeView),
))
async function openCodeView() {
  const ready = ensureHistoryDetails()
  if (!(typeof ready === 'boolean' ? ready : await ready)) return
  const view = projection.value.codeView
  if (!canOpenCodeView.value || !view) return
  openToolHistoryFile(view, { sessionKey: props.sessionKey!, toolId: props.call.toolId,
    recordKey: props.call.renderKey, title: props.label,
    ...(normalizeToolFileSnapshot(props.call.fileSnapshot) ? { fileSnapshot: normalizeToolFileSnapshot(props.call.fileSnapshot) } : {}) })
}
const executionIo = computed(() => projectExecutionIoForCall(props.call))
const executionIoLabel = computed(() => {
  if (executionIo.value.kind === 'pty') return t('shared.runTrace.executionIoPty')
  if (executionIo.value.kind === 'fallback') return t('shared.runTrace.executionIoFallback')
  if (executionIo.value.kind === 'pipe') return t('shared.runTrace.executionIoPipe')
  if (executionIo.value.kind === 'mixed') return t('shared.runTrace.executionIoMixed')
  return t('shared.runTrace.executionIoUnknown')
})
const copyState = ref<'idle' | 'copied' | 'error'>('idle')
const downloadError = ref(false)
let downloadPending = false
let copyResetId: number | null = null
let copyGeneration = 0
let disposed = false

function ensureHistoryDetails(): boolean | Promise<boolean> {
  const toolId = props.call.toolId
  const generation = copyGeneration
  const ready = historyDetailsReady?.()
  if (ready === false) return false
  if (ready && typeof ready !== 'boolean') {
    return ready.then(async resolved => {
      if (!resolved) return false
      await nextTick()
      return !disposed && generation === copyGeneration && toolId === props.call.toolId
    })
  }
  return !disposed && generation === copyGeneration && toolId === props.call.toolId
}

function detailInputText(): string {
  const raw = String(props.call.inputRaw || props.call.inputPreview || '').trim()
  return raw ? redactActivityDetail(raw) : ''
}

function detailResultText(): string {
  return redactActivityDetail(
    String(props.call.result || props.call.resultPreview || '').trim(),
  )
}

const baseDetailSections = computed<ActivityDetailSection[]>(() => {
  if (projection.value.detailMode === 'changes') {
    const content = projection.value.rawContent
    return content
      ? [{
          kind: 'changes',
          label: t('shared.runTrace.sectionChanges'),
          content,
        }]
      : []
  }
  if (projection.value.detailMode === 'result') {
    const content = projection.value.rawContent
    const kind = props.call.isError || props.call.status === 'error'
      ? 'error'
      : 'result'
    return content
      ? [{
          kind,
          label: t(
            kind === 'error'
              ? 'shared.runTrace.sectionError'
              : 'shared.runTrace.sectionResult',
          ),
          content,
        }]
      : []
  }
  const sections: ActivityDetailSection[] = []
  const input = detailInputText()
  const result = detailResultText()
  if (input) {
    sections.push({
      kind: 'input',
      label: t('shared.runTrace.sectionInput'),
      content: input,
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
    })
  }
  return sections
})

const genericLog = computed(() => !projection.value.codeView && inlineLog.content.value !== null
  ? redactActivityDetail(inlineLog.content.value) : '')
const detailSections = computed<ActivityDetailSection[]>(() => {
  const sections = baseDetailSections.value
  const log = genericLog.value
  if (!log || sections.some(section => section.content === log)) return sections
  const resultIndex = sections.findIndex(section => section.kind === 'result' || section.kind === 'error')
  if (resultIndex < 0) return [...sections, { kind: 'result', label: t('shared.runTrace.sectionResult'), content: log }]
  return sections.map((section, index) => index === resultIndex
    ? { ...section, content: `${section.content}\n\n${log}` } : section)
})
const sectionPreviews = computed(() => boundedTextPreviews(detailSections.value.map(section => section.content)))
const visibleSections = computed(() => detailSections.value.map((section, index) => ({
  ...section, content: sectionPreviews.value.previews[index]?.content || '',
})).filter(section => section.content))
const detailTruncated = computed(() => inlineLog.truncated.value || sectionPreviews.value.truncated)
const detailPartial = computed(() => inlineLog.content.value !== null ? inlineLog.partial.value
  : props.call.resultTruncated === true || (typeof props.call.resultOriginalChars === 'number'
    && props.call.resultOriginalChars > String(props.call.result || props.call.resultPreview || '').length))
const detailDownloadLabel = computed(() => t(downloadError.value ? 'shared.runTrace.downloadContentFailed'
  : detailPartial.value ? 'chat.download' : 'shared.runTrace.downloadFullContent'))
const detailPartialLabel = computed(() => t(inlineLog.content.value !== null && inlineLog.partial.value
  ? 'chat.toolModal.logIncomplete' : 'shared.runTrace.codePartial'))
const isBoundedDetail = computed(() => !projection.value.codeView
  && Boolean(projection.value.rawContent || genericLog.value))

const copyLabel = computed(() => (
  copyState.value === 'copied'
    ? t('chat.copied')
    : copyState.value === 'error'
      ? t('chat.toast.copyFailed')
      : t(detailPartial.value ? 'shared.runTrace.copyPreview' : 'chat.copy')
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
  const generation = ++copyGeneration
  try {
    const ready = ensureHistoryDetails()
    if (!(typeof ready === 'boolean' ? ready : await ready)) {
      if (!disposed && generation === copyGeneration) { copyState.value = 'error'; scheduleCopyReset() }
      return
    }
    if (inlineLog.available.value && !await inlineLog.ensureComplete()) {
      if (!disposed && generation === copyGeneration) {
        copyState.value = 'error'
        scheduleCopyReset()
      }
      return
    }
    if (fileSnapshot.required.value && !await fileSnapshot.ensureComplete()) {
      if (!disposed && generation === copyGeneration) { copyState.value = 'error'; scheduleCopyReset() }
      return
    }
    if (disposed || generation !== copyGeneration) return
    const detail = projection.value
    const log = genericLog.value
    const raw = detail.rawContent
    const copyContent = detail.codeView ? activityToolCodeContent(detail.codeView)
      : log && !baseDetailSections.value.some(section => section.content === log)
        ? [raw, log].filter(Boolean).join('\n\n') : raw
    await copyTextWithFallback(copyContent)
    if (disposed || generation !== copyGeneration) return
    copyState.value = 'copied'
  } catch {
    if (disposed || generation !== copyGeneration) return
    copyState.value = 'error'
  }
  scheduleCopyReset()
}

async function downloadDetails() {
  if (downloadPending) return
  const generation = copyGeneration
  downloadPending = true
  downloadError.value = false
  try {
    const ready = ensureHistoryDetails()
    if (!(typeof ready === 'boolean' ? ready : await ready)) throw new Error('History details unavailable')
    const view = projection.value.codeView
    if (view && ['write', 'edit', 'read'].includes(view.kind) && !view.error) {
      await downloadToolHistoryContent(view, { sessionKey: props.sessionKey || '',
        fileSnapshot: normalizeToolFileSnapshot(props.call.fileSnapshot) }, contentAccess)
      return
    }
    if (inlineLog.available.value) { await inlineLog.download(); return }
    // Already-received raw records can be saved without constructing another DOM view.
    const detail = projection.value
    const content = detail.codeView ? activityToolCodeContent(detail.codeView)
      : detailSections.value.map(section => `${section.label}\n${section.content}`).join('\n\n')
    downloadText('tool-output.txt', 'text/plain;charset=utf-8', content)
  } catch {
    if (!disposed && generation === copyGeneration) downloadError.value = true
  } finally {
    if (!disposed && generation === copyGeneration) downloadPending = false
  }
}

function checkLogScrollEnd(event: Event) {
  const element = event.currentTarget as HTMLElement | null
  if (!detailTruncated.value && element && element.clientHeight > 0 && element.scrollTop + element.clientHeight >= element.scrollHeight - 96) {
    void inlineLog.loadMore()
  }
}
function onLogWheel(event: WheelEvent) { if (event.deltaY > 0) checkLogScrollEnd(event) }
function onLogPageKey(event: KeyboardEvent) {
  if (['ArrowDown', 'PageDown', 'End'].includes(event.key) || (event.key === ' ' && !event.shiftKey)) checkLogScrollEnd(event)
}

watch([() => props.call.renderKey, () => props.call.toolId, () => props.sessionKey, () => props.operationKey], () => {
  copyGeneration += 1
  clearCopyReset()
  copyState.value = 'idle'
  downloadError.value = false
  downloadPending = false
}, { flush: 'sync' })
onBeforeUnmount(() => {
  disposed = true
  copyGeneration += 1
  clearCopyReset()
})

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

</script>

<style scoped>
.activity-tool-details { min-width: 0; padding: 0.0625rem 0 0.25rem; font-size: 0.75rem; line-height: 1.45; }
.activity-tool-details:empty { padding: 0; }
.activity-tool-details__execution-io { margin: 0 0 0.375rem; color: var(--text-muted); font-size: 0.6875rem; }
.activity-tool-details__execution-io--fallback { color: var(--warn); }
.activity-tool-details__execution-io-reason { display: block; color: var(--text-dim); font-family: var(--font-mono); overflow-wrap: anywhere; }
.activity-tool-details__log-status { margin: 0 0 0.375rem; color: var(--text-muted); font-size: 0.75rem; }
.activity-tool-details__log-status button { margin-inline-start: 0.5rem; padding: 0; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text); font: inherit; text-decoration: underline; cursor: pointer; }
.activity-tool-details__log-status button:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.activity-tool-details--bounded { position: relative; display: flex; flex-direction: column; max-width: 100%; max-height: min(20rem, 45vh); padding: 0; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg); overflow: hidden; }
.activity-tool-details__window { min-height: 0; padding: 0.875rem 2.75rem 0.875rem 0.875rem; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.activity-tool-details__truncated { flex: 0 0 auto; margin: 0; padding: 0.375rem 0.875rem; color: var(--text-muted); font-size: 0.75rem; }
.activity-tool-details__window:focus-visible { outline: none; box-shadow: var(--focus-ring-inset); }
.activity-tool-details__section { margin: 0; padding: 0; border: 0; background: transparent; }
.activity-tool-details__section + .activity-tool-details__section { margin-top: 0.75rem; }
.activity-tool-details__section-label { margin-bottom: 0.25rem; color: var(--text-dim); font-size: 0.6875rem; font-weight: 600; }
.activity-tool-details__preview { margin: 0; padding: 0; border: 0; background: transparent; color: var(--text-muted); font: inherit; font-family: var(--font-mono); font-variant-ligatures: none; line-height: 1.55; overflow-wrap: anywhere; white-space: pre-wrap; }
.activity-tool-details__copy { position: absolute; top: 0.25rem; right: 0.375rem; z-index: 1; display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; padding: 0; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.activity-tool-details__copy:hover { background: var(--bg-hover); color: var(--text); }
.activity-tool-details__copy:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.activity-tool-details__copy.is-copied { color: var(--ok); }
.activity-tool-details__copy.is-error { color: var(--danger); }
.activity-tool-details__copy-status { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.activity-tool-details__summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 0.35rem; width: fit-content; max-width: 100%; min-width: 0; color: var(--text-muted); }
.activity-tool-details__line { flex: 0 0 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.activity-tool-details__line--target,
.activity-tool-details__line--code { flex: 1 1 auto; color: var(--text-muted); }
.activity-tool-details__line--code { font-family: var(--font-mono); font-variant-ligatures: none; }
.activity-tool-details__line--error { flex: 1 1 auto; overflow: visible; overflow-wrap: anywhere; white-space: normal; color: var(--text-muted); }
.activity-tool-details__separator { flex: 0 0 auto; color: var(--text-dim); }
@media (pointer: coarse) { .activity-tool-details__copy { width: 2.75rem; height: 2.75rem; } }
</style>
