<template>
  <div
    ref="traceRoot"
    class="tool-timeline"
    :class="{
      'tool-timeline--checklist': variant === 'checklist',
      'tool-timeline--activity': presentation === 'activity',
    }"
  >
  <section
    v-if="summary"
    class="run-trace__summary control-stat-grid control-stat-grid--fixed"
    style="--control-stat-columns: 5"
  >
    <div class="control-stat control-stat--static">
      <div class="control-stat__label">{{ t('shared.runTrace.status') }}</div>
      <div class="control-stat__value">
        <span v-if="summary.loading" class="run-trace__skeleton" aria-hidden="true" />
        <template v-else-if="summary.status">
          <span
            class="run-trace__dot"
            :class="`run-trace__dot--${statusTone}`"
            :aria-label="statusLabel"
          />
          {{ statusLabel }}
        </template>
        <template v-else>—</template>
      </div>
    </div>
    <div class="control-stat control-stat--static">
      <div class="control-stat__label">{{ t('shared.runTrace.executor') }}</div>
      <div class="control-stat__value run-trace__summary-text">
        <span v-if="summary.loading" class="run-trace__skeleton" aria-hidden="true" />
        <template v-else>{{ summary.executor || '—' }}</template>
      </div>
    </div>
    <div class="control-stat control-stat--static">
      <div class="control-stat__label">{{ t('shared.runTrace.time') }}</div>
      <div class="control-stat__value control-stat__value--mono">
        <span v-if="summary.loading" class="run-trace__skeleton" aria-hidden="true" />
        <template v-else>{{ fmtMs(summary.elapsedMs) }}</template>
      </div>
    </div>
    <div class="control-stat control-stat--static">
      <div class="control-stat__label">{{ t('shared.runTrace.tokens') }}</div>
      <div class="control-stat__value control-stat__value--mono">
        <span v-if="summary.loading" class="run-trace__skeleton" aria-hidden="true" />
        <template v-else>{{ fmtTok(summary.tokens) }}</template>
      </div>
    </div>
    <div class="control-stat control-stat--static">
      <div class="control-stat__label">{{ t('shared.runTrace.steps') }}</div>
      <div class="control-stat__value control-stat__value--mono">
        <span v-if="summary.loading" class="run-trace__skeleton" aria-hidden="true" />
        <template v-else>{{ summary.steps != null ? summary.steps : '—' }}</template>
      </div>
    </div>
  </section>
  <TransitionGroup name="tool-row" tag="div" class="tool-row-group">
  <template v-for="item in displayItems" :key="item.key">
    <div
      v-if="item.type === 'bulk-control'"
      class="tool-timeline__toolbar"
      data-testid="run-trace-bulk-toolbar"
      data-share-control
    >
      <span class="tool-timeline__summary">
        <Icon name="listChecks" :size="13" aria-hidden="true" />
        <span>{{ t('shared.runTrace.callsCount', { count: item.callCount }) }}</span>
      </span>
      <button
        type="button"
        class="tool-timeline__bulk-action"
        data-testid="run-trace-bulk-toggle"
        :title="bulkToggleLabel"
        @click="toggleAllTools"
      >
        <span>{{ bulkToggleLabel }}</span>
        <Icon
          v-if="presentation !== 'activity'"
          name="chevronDown"
          :size="13"
          class="tool-timeline__bulk-icon"
          :class="{ 'is-collapse': anyBulkTargetOpen }"
          aria-hidden="true"
        />
      </button>
    </div>
    <div
      v-else-if="item.type === 'text'"
      class="msg-ai-text"
      :data-activity-entry="presentation === 'activity' ? 'true' : undefined"
      :data-activity-type="presentation === 'activity' ? 'segment' : undefined"
      :data-activity-id="presentation === 'activity' ? item.key : undefined"
      :data-activity-order="presentation === 'activity' ? item.activityOrder : undefined"
      v-html="item.html"
    />
    <div
      v-else-if="item.type === 'interrupt'"
      class="run-trace__interrupt"
      :data-activity-entry="presentation === 'activity' ? 'true' : undefined"
      :data-activity-type="presentation === 'activity' ? 'interrupt' : undefined"
      :data-activity-id="presentation === 'activity' ? item.key : undefined"
      :data-activity-order="presentation === 'activity' ? item.activityOrder : undefined"
    >
      <slot name="interrupt" :part="item.part" />
    </div>
    <button
      v-else-if="item.type === 'overflow'"
      type="button"
      class="tool-overflow-note"
      :title="t('shared.runTrace.showAllCalls')"
      @click="showAllRows = true"
    >
      {{ t('shared.runTrace.earlierCalls', { count: item.hiddenCount }) }}
    </button>
    <div v-else class="step-card">
      <div
        class="step-group"
        :class="{
          'step-group--running': item.group.isRunning,
          'step-group--error': item.group.isError,
          'is-open': groupOpen(item.group),
        }"
      >
        <!-- Multi-call batches keep a group header; single calls render as one row. -->
        <template v-if="item.group.calls.length > 1">
          <button
            type="button"
            class="tool-row tool-row--group"
            :data-op="item.group.operationKey"
            :aria-expanded="groupOpen(item.group)"
            @click="toggleGroupDisclosure(item.group)"
          >
            <Icon
              v-if="presentation === 'activity'"
              class="tool-row__activity-icon"
              :class="activityGroupIconClass(item.group)"
              :name="item.group.iconName"
              :size="14"
              aria-hidden="true"
            />
            <span v-else class="tool-row__bullet" :class="groupBulletClass(item.group)" aria-hidden="true" />
            <span class="tool-row__label">{{ item.group.label }}</span>
            <!-- Activity groups already carry the call count in their footprint
                 secondary ("2 web actions"), so the raw call-count pill would
                 repeat it. -->
            <span v-if="presentation !== 'activity'" class="step-count">{{ t('shared.runTrace.callsCount', { count: item.group.calls.length }) }}</span>
            <span v-if="item.group.secondary && (!item.group.isRunning || groupOpen(item.group))" class="tool-row__arg">{{ item.group.secondary }}</span>
            <Icon
              v-if="presentation === 'activity'"
              class="step-chevron tool-row__activity-arrow"
              name="chevronRight"
              :size="13"
              data-share-control
              aria-hidden="true"
            />
            <span class="tool-row__trailing">
              <span v-if="showGroupStatus(item.group)" class="tool-row__status">{{ resolvedGroupStatusText(item.group) }}</span>
              <Icon v-if="presentation !== 'activity'" class="step-chevron" name="chevronRight" :size="14" />
            </span>
          </button>
          <TransitionGroup
            v-if="groupOpen(item.group)"
            name="tool-member"
            tag="div"
            class="step-group-members"
          >
            <div
              v-for="call in item.group.calls"
              :key="call.renderKey"
              class="tool-row-wrap"
              :data-activity-entry="presentation === 'activity' ? 'true' : undefined"
              :data-activity-type="presentation === 'activity' ? 'segment' : undefined"
              :data-activity-id="presentation === 'activity' ? `tool:${call.toolId}` : undefined"
              :data-activity-order="presentation === 'activity' ? call.activityOrder : undefined"
            >
              <button
                type="button"
                class="tool-row tool-row--member"
                :class="rowClass(call)"
                :data-op="operationKey(call)"
                :aria-expanded="callOpen(call)"
                @click="toggleItemDisclosure(call)"
              >
                <Icon
                  v-if="presentation === 'activity'"
                  class="tool-row__activity-icon"
                  :class="activityIconClass(call)"
                  :name="toolIconName(call.name)"
                  :size="14"
                  aria-hidden="true"
                />
                <span v-else class="tool-row__bullet" :class="bulletClass(call)" aria-hidden="true" />
                <span class="tool-row__label tool-row__label--member">{{ call.displayName }}</span>
                <span v-if="(!call.isRunning || callOpen(call)) && resolvedSecondaryText(call)" class="tool-row__arg">{{ resolvedSecondaryText(call) }}</span>
                <Icon
                  v-if="presentation === 'activity' && callHasDetails(call)"
                  class="step-chevron tool-row__activity-arrow"
                  name="chevronRight"
                  :size="13"
                  data-share-control
                  aria-hidden="true"
                />
                <span class="tool-row__trailing">
                  <!-- Failure text is plain row content on purpose: it joins the
                       button's accessible name, which screen readers announce when
                       the row is reached. A live region mounted already-populated
                       would never announce. -->
                  <span v-if="activityTerminalStatusText(call)" class="tool-row__status">{{ activityTerminalStatusText(call) }}</span>
                  <span v-if="resultCountText(call)" class="tool-row__status">{{ resultCountText(call) }}</span>
                  <span v-if="elapsedFor(call)" class="tool-row__elapsed">{{ elapsedFor(call) }}</span>
                  <Icon v-if="presentation !== 'activity' && iconFor(call).glyph === 'check'" class="tool-row__state-icon tool-row__state-icon--ok" name="check" :size="13" />
                  <Icon v-else-if="presentation !== 'activity' && iconFor(call).glyph === 'x'" class="tool-row__state-icon tool-row__state-icon--err" name="x" :size="13" />
                  <Icon v-if="presentation !== 'activity'" class="step-chevron" name="chevronRight" :size="14" />
                </span>
              </button>
              <Transition name="activity-tool-detail" :css="presentation === 'activity'">
                <div v-if="callOpen(call)" class="tool-row-body">
                  <ActivityToolDetails
                    v-if="presentation === 'activity'"
                    :call="call"
                    :label="call.displayName"
                    :operation-key="operationKey(call)"
                    @show-result="forwardShowResult"
                  />
                  <ToolRowSections
                    v-else
                    :call="call"
                    :label="call.displayName"
                    @show-result="forwardShowResult"
                  />
                </div>
              </Transition>
            </div>
          </TransitionGroup>
        </template>
        <template v-else>
          <div
            v-for="call in item.group.calls"
            :key="call.renderKey"
            class="tool-row-wrap"
            :data-activity-entry="presentation === 'activity' ? 'true' : undefined"
            :data-activity-type="presentation === 'activity' ? 'segment' : undefined"
            :data-activity-id="presentation === 'activity' ? `tool:${call.toolId}` : undefined"
            :data-activity-order="presentation === 'activity' ? call.activityOrder : undefined"
          >
            <button
              type="button"
              class="tool-row"
              :class="rowClass(call)"
              :data-op="operationKey(call)"
              :aria-expanded="callOpen(call)"
              @click="toggleItemDisclosure(call)"
            >
              <Icon
                v-if="presentation === 'activity'"
                class="tool-row__activity-icon"
                :class="activityIconClass(call)"
                :name="item.group.iconName"
                :size="14"
                aria-hidden="true"
              />
              <span v-else class="tool-row__bullet" :class="bulletClass(call)" aria-hidden="true" />
              <span class="tool-row__label">{{ item.group.label }}</span>
              <span v-if="(!call.isRunning || callOpen(call)) && singleCallSecondary(item.group, call)" class="tool-row__arg">
                {{ singleCallSecondary(item.group, call) }}
              </span>
              <Icon
                v-if="presentation === 'activity' && callHasDetails(call)"
                class="step-chevron tool-row__activity-arrow"
                name="chevronRight"
                :size="13"
                data-share-control
                aria-hidden="true"
              />
              <span class="tool-row__trailing">
                <span v-if="activityTerminalStatusText(call)" class="tool-row__status">{{ activityTerminalStatusText(call) }}</span>
                <span v-if="resultCountText(call)" class="tool-row__status">{{ resultCountText(call) }}</span>
                <span v-if="elapsedFor(call)" class="tool-row__elapsed">{{ elapsedFor(call) }}</span>
                <Icon v-if="presentation !== 'activity' && iconFor(call).glyph === 'check'" class="tool-row__state-icon tool-row__state-icon--ok" name="check" :size="13" />
                <Icon v-else-if="presentation !== 'activity' && iconFor(call).glyph === 'x'" class="tool-row__state-icon tool-row__state-icon--err" name="x" :size="13" />
                <Icon v-if="presentation !== 'activity'" class="step-chevron" name="chevronRight" :size="14" />
              </span>
            </button>
            <Transition name="activity-tool-detail" :css="presentation === 'activity'">
              <div v-if="callOpen(call)" class="tool-row-body">
                <ActivityToolDetails
                  v-if="presentation === 'activity'"
                  :call="call"
                  :label="item.group.label"
                  :operation-key="operationKey(call)"
                  @show-result="forwardShowResult"
                />
                <ToolRowSections
                  v-else
                  :call="call"
                  :label="item.group.label"
                  @show-result="forwardShowResult"
                />
              </div>
            </Transition>
          </div>
        </template>
      </div>
    </div>
  </template>
  </TransitionGroup>
  </div>
</template>

<script lang="ts">
import { defineComponent, h, type PropType } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'

const SECTION_PREVIEW_LIMIT = 200
const COMPACT_SECTION_CHAR_LIMIT = 360
const COMPACT_SECTION_LINE_LIMIT = 6
const COMPACT_SNIPPET_LIMIT = 150

function parseToolResultRecord(raw: string): Record<string, unknown> | null {
  const text = String(raw || '').trim()
  if (!text.startsWith('{')) return null
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function contentLineCount(value: string): number {
  if (!value) return 0
  return value.split(/\r\n|\r|\n/).length
}

function shouldCompactSection(full: string, preview: string): boolean {
  const text = full || preview || ''
  return text.length > COMPACT_SECTION_CHAR_LIMIT || contentLineCount(text) > COMPACT_SECTION_LINE_LIMIT
}

function truncateInline(value: string, limit = COMPACT_SNIPPET_LIMIT): string {
  const text = value.replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit).trimEnd()}...` : text
}

function firstNonEmptyLine(value: string): string {
  return value.split(/\r\n|\r|\n/).find(line => line.trim()) || ''
}

function compactJsonValue(value: unknown, limit = 44): string {
  if (typeof value === 'string') return truncateInline(value, limit)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null) return 'null'
  if (Array.isArray(value)) return `[${value.length} items]`
  const record = asRecord(value)
  if (record) {
    const entries = Object.entries(record)
      .slice(0, 2)
      .map(([key, entry]) => `${key}: ${compactJsonValue(entry, 32)}`)
    return entries.length ? `{ ${entries.join(', ')}${Object.keys(record).length > 2 ? ', ...' : ''} }` : '{}'
  }
  return truncateInline(String(value), limit)
}

function jsonObjectSnippet(record: Record<string, unknown>): string {
  const pairs = Object.entries(record)
    .slice(0, 4)
    .map(([key, entry]) => `${key}: ${compactJsonValue(entry)}`)
  return pairs.length ? truncateInline(pairs.join(', ')) : ''
}

function shellCommandFromRecord(record: Record<string, unknown> | null): string {
  return typeof record?.command === 'string' ? record.command : ''
}

function shellResultBlock(lines: string[], key: string): string {
  const start = lines.findIndex(line => line.trimStart().startsWith(key))
  if (start < 0) return ''

  const block = [lines[start].trimStart().slice(key.length)]
  for (const line of lines.slice(start + 1)) {
    if (/^(exit_code|stdout|stderr|timed_out|duration|stdout_truncated|stderr_truncated)=/.test(line.trimStart())) {
      break
    }
    block.push(line)
  }
  return block.join('\n').trim()
}

function shellResultSnippet(value: string): string {
  const lines = value.split(/\r\n|\r|\n/)
  const exitLineIndex = lines.findIndex(line => line.trim())
  const exitLine = exitLineIndex >= 0 ? lines[exitLineIndex].trim() : ''
  if (!/^exit_code=/.test(exitLine)) return ''

  let outputLabel = 'stdout'
  let output = shellResultBlock(lines, 'stdout=')
  if (!output && exitLineIndex >= 0) {
    outputLabel = 'output'
    output = lines.slice(exitLineIndex + 1).join('\n').trim()
  }

  const parts = [exitLine]
  if (output) {
    const outputRecord = parseToolResultRecord(output)
    const outputPreview = outputRecord
      ? jsonObjectSnippet(outputRecord)
      : truncateInline(firstNonEmptyLine(output) || output, 90)
    if (outputPreview) parts.push(`${outputLabel}: ${outputPreview}`)
  }
  return truncateInline(parts.join(', '))
}

function compactKind(value: string): string {
  const text = value.trim()
  if (!text) return 'text'
  const record = parseToolResultRecord(text)
  if (shellCommandFromRecord(record)) return 'shell command'
  if (shellResultSnippet(text)) return 'shell result'
  if (text.startsWith('{') || text.startsWith('[')) return 'JSON'
  return 'text'
}

function compactSnippet(value: string): string {
  const text = value.trim()
  if (!text) return ''
  const record = parseToolResultRecord(text)
  const command = shellCommandFromRecord(record)
  if (command) return truncateInline(`command: ${command}`)
  if (record) return jsonObjectSnippet(record)

  const shellSnippet = shellResultSnippet(text)
  if (shellSnippet) return shellSnippet

  return truncateInline(firstNonEmptyLine(text) || text)
}

function compactMeta(value: string): string {
  const command = shellCommandFromRecord(parseToolResultRecord(value.trim()))
  const measured = command || value
  const chars = measured.length
  const lines = contentLineCount(measured)
  const kind = compactKind(value)
  const parts = [kind]
  if (lines > 1) parts.push(`${lines} lines`)
  parts.push(`${chars.toLocaleString()} chars`)
  return parts.join(' | ')
}

function toolResultContext(
  call: ChatToolCallRenderItem,
  section: NonNullable<ToolResultContext['section']>,
): ToolResultContext {
  return {
    toolName: call.name,
    inputRaw: call.inputRaw || call.inputPreview,
    section,
  }
}

function webDiagnosticsSummary(raw: string): string {
  const payload = parseToolResultRecord(raw)
  if (!payload) return ''
  const diagnostics = asRecord(payload.diagnostics)
  if (!diagnostics) return ''

  const attempts = Array.isArray(payload.provider_attempts)
    ? payload.provider_attempts
    : Array.isArray(diagnostics.provider_attempts)
      ? diagnostics.provider_attempts
      : []
  const successfulAttempt = attempts
    .map(item => asRecord(item))
    .find(item => item?.status === 'success' && item?.provider)
  const selected = String(
    diagnostics.selected_provider ||
    payload.provider ||
    successfulAttempt?.provider ||
    '',
  )
  const fallbackFrom = String(diagnostics.fallback_from || '')
  const fetchedCount = asNumber(diagnostics.fetched_count)
  const fetchFailedCount = asNumber(diagnostics.fetch_failed_count)
  const returnedChars = asNumber(diagnostics.returned_chars)
  const truncated = diagnostics.budget_clamped === true

  const parts: string[] = []
  if (selected) parts.push(`provider ${selected}`)
  if (attempts.length) parts.push(`${attempts.length} attempt${attempts.length === 1 ? '' : 's'}`)
  if (fallbackFrom) parts.push(`fallback from ${fallbackFrom}`)
  if (fetchedCount !== null) parts.push(`${fetchedCount} fetched`)
  if (fetchFailedCount) parts.push(`${fetchFailedCount} fetch failed`)
  if (returnedChars !== null) parts.push(`${returnedChars} chars`)
  if (truncated) parts.push('truncated')
  return parts.join(' · ')
}

// Labeled input / result / error sections shown in an expanded row body.
const ToolRowSections = defineComponent({
  name: 'ToolRowSections',
  props: {
    call: { type: Object as PropType<ChatToolCallRenderItem>, required: true },
    label: { type: String, required: true },
  },
  emits: ['showResult'],
  setup(props, { emit }) {
    const { t } = useI18n()
    return () => {
      const call = props.call
      const sections = []
      if (call.inputPreview) {
        const fullInput = call.inputRaw || ''
        const inputContent = fullInput || call.inputPreview
        const compact = shouldCompactSection(inputContent, call.inputPreview)
        sections.push(h('section', { class: 'tool-row-section' }, [
          h('div', { class: 'tool-row-section__label' }, t('shared.runTrace.sectionInput')),
          compact
            ? h('div', { class: 'tool-row-section__compact' }, [
                h('span', { class: 'tool-row-section__compact-meta' }, compactMeta(inputContent)),
                h('span', { class: 'tool-row-section__compact-snippet' }, compactSnippet(inputContent)),
              ])
            : h('pre', { class: 'tool-row-section__pre' }, call.inputPreview),
          fullInput.length > SECTION_PREVIEW_LIMIT || compact
            ? h('button', {
                type: 'button',
                class: 'step-view-btn',
                onClick: (event: Event) => {
                  event.stopPropagation()
                  emit(
                    'showResult',
                    fullInput,
                    `${props.label} · ${t('shared.runTrace.sectionInput')}`,
                    toolResultContext(call, 'input'),
                  )
                },
              }, t('shared.runTrace.viewFull'))
            : null,
        ]))
      }
      const diagnostics = webDiagnosticsSummary(call.result)
      if (diagnostics) {
        sections.push(h('section', { class: 'tool-row-section' }, [
          h('div', { class: 'tool-row-section__label' }, t('shared.runTrace.sectionDiagnostics')),
          h('pre', { class: 'tool-row-section__pre' }, diagnostics),
        ]))
      }
      if (call.result) {
        const kindLabel = call.isError
          ? t('shared.runTrace.sectionError')
          : t('shared.runTrace.sectionResult')
        const resultContent = call.result || call.resultPreview
        const compact = shouldCompactSection(resultContent, call.resultPreview)
        sections.push(h('section', {
          class: ['tool-row-section', { 'tool-row-section--error': call.isError }],
        }, [
          h('div', { class: 'tool-row-section__label' }, kindLabel),
          compact
            ? h('div', { class: 'tool-row-section__compact' }, [
                h('span', { class: 'tool-row-section__compact-meta' }, compactMeta(resultContent)),
                h('span', { class: 'tool-row-section__compact-snippet' }, compactSnippet(resultContent)),
              ])
            : h('pre', { class: 'tool-row-section__pre' }, call.resultPreview),
          call.result.length > SECTION_PREVIEW_LIMIT || compact
            ? h('button', {
                type: 'button',
                class: 'step-view-btn',
                onClick: (event: Event) => {
                  event.stopPropagation()
                  emit(
                    'showResult',
                    call.result,
                    `${props.label} · ${kindLabel}`,
                    toolResultContext(call, call.isError ? 'error' : 'result'),
                  )
                },
              }, t('shared.runTrace.viewFull'))
            : null,
        ]))
      }
      return sections
    }
  },
})

export default { components: { ToolRowSections } }
</script>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Icon from '@/components/Icon.vue'
import ActivityToolDetails from '@/components/chat/ActivityToolDetails.vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
} from '@/types/chat'
import type { NodeStep, RunTraceStatus, RunTraceSummary } from '@/types/runTrace'
import {
  toolGroupStatusText as defaultToolGroupStatusText,
  toolSecondaryText as defaultToolSecondaryText,
  toolStatusText as defaultToolStatusText,
  toolIconName,
  toolOperationKey,
  toolResultCount,
} from '@/utils/chat/toolDisplay'
import { toolState } from '@/utils/chat/toParts'
import { composeTree, statusVisual, type StatusVisual } from '@/components/run/runTrace'
import { copyTextWithFallback } from '@/utils/browser'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'

const { t } = useI18n()
const { mode: toolDetailDisplayMode } = useToolDetailPreference()

const MAX_TOOL_ROWS = 30

// Reads and searches collapse to a pill by default; writes, exec, and unknown
// tools stay expanded; error rows auto-expand. Manual toggles invert the
// default, so a user collapse is always respected.
const COLLAPSED_BY_DEFAULT = new Set(['web.discover', 'web.search', 'web.read', 'file.inspect', 'memory.search'])

type TimelineRenderItem =
  | ChatStreamTimelineItem
  | { type: 'overflow'; key: string; hiddenCount: number }

type TimelineDisplayItem =
  | TimelineRenderItem
  | { type: 'bulk-control'; key: string; callCount: number }

const props = defineProps<{
  // Chat path: the proven timeline shape (full group data). The lifted markup
  // renders this unchanged.
  items?: ChatStreamTimelineItem[]
  // Flat path: SessionInspect / Logs pass NodeStep[]; RunTrace composes the tree.
  steps?: NodeStep[]
  // summary strip; omit to hide it entirely.
  summary?: RunTraceSummary
  // Open-state is caller-owned (chat keeps its toggle store; non-chat surfaces
  // use useRunTrace()).
  isToolGroupOpen?: (groupId: string) => boolean
  isToolItemOpen?: (renderKey: string) => boolean
  // Label helpers are injected; default to the toolDisplay exports when omitted.
  toolGroupStatusText?: (group: ChatToolCallGroup) => string
  toolStatusText?: (call: ChatToolCallRenderItem) => string
  toolSecondaryText?: (call: ChatToolCallRenderItem) => string
  // Live streams provide real per-call timings; replayed history omits this
  // prop, so no fabricated elapsed badges appear.
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  // 'checklist' drives the live work-card presentation: a running row shows a
  // pulsing ring, a completed row dims, an error row stays open. History
  // omits this, keeping the default pill timeline untouched.
  variant?: 'checklist'
  // Chat activity disclosures opt into a quieter visual treatment. This is
  // presentation-only: disclosure state and tool behavior stay unchanged.
  presentation?: 'activity'
  // Chat enables the turn-level bulk control through ToolCallTimeline. Direct
  // RunTrace consumers such as Logs and Session Inspect keep their current UI.
  showBulkToggle?: boolean
  // History can preserve provider group and tool ids that repeat across
  // messages. Scope only ephemeral disclosure keys; render ids stay unchanged.
  stateScope?: string
}>()

defineSlots<{
  interrupt?: (props: {
    part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>
  }) => unknown
}>()

const emit = defineEmits<{
  toggleGroup: [groupId: string]
  toggleItem: [renderKey: string]
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

const traceRoot = ref<HTMLElement | null>(null)
const showAllRows = ref(false)

function codeText(pre: HTMLPreElement): string {
  const code = pre.querySelector('code')
  return code?.textContent || ''
}

function hasCodeCopyButton(pre: HTMLPreElement): boolean {
  return Array.from(pre.children).some(child => child.classList.contains('code-copy-btn'))
}

function setCodeCopyButtonState(button: HTMLButtonElement, state: 'idle' | 'copied' | 'error') {
  const label = state === 'copied'
    ? t('chat.copied')
    : state === 'error'
      ? t('chat.toast.copyFailed')
      : t('chat.copy')
  button.replaceChildren(createCodeCopyIcon(state))
  button.title = label
  button.setAttribute('aria-label', label)
}

function createCodeCopyIcon(state: 'idle' | 'copied' | 'error'): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('width', '15')
  svg.setAttribute('height', '15')
  svg.setAttribute('aria-hidden', 'true')
  svg.setAttribute('focusable', 'false')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')

  if (state === 'copied') {
    svg.appendChild(svgNode('polyline', { points: '20 6 9 17 4 12' }))
    return svg
  }
  if (state === 'error') {
    svg.appendChild(svgNode('path', { d: 'M18 6 6 18' }))
    svg.appendChild(svgNode('path', { d: 'm6 6 12 12' }))
    return svg
  }

  svg.appendChild(svgNode('rect', { width: '14', height: '14', x: '8', y: '8', rx: '2', ry: '2' }))
  svg.appendChild(svgNode('path', { d: 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2' }))
  return svg
}

function svgNode(tag: string, attrs: Record<string, string>): SVGElement {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag)
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value)
  return node
}

function decorateCodeBlocks() {
  const root = traceRoot.value
  if (!root) return
  for (const pre of root.querySelectorAll<HTMLPreElement>('.msg-ai-text pre')) {
    if (hasCodeCopyButton(pre)) continue
    const text = codeText(pre)
    if (!text) continue

    pre.classList.add('code-block')
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'code-copy-btn'
    setCodeCopyButtonState(button, 'idle')
    button.addEventListener('click', async event => {
      event.preventDefault()
      event.stopPropagation()
      try {
        await copyTextWithFallback(codeText(pre))
        setCodeCopyButtonState(button, 'copied')
        button.classList.add('is-copied')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCodeCopyButtonState(button, 'idle')
          button.classList.remove('is-copied')
        }, 1600)
      } catch {
        setCodeCopyButtonState(button, 'error')
        button.classList.add('is-error')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCodeCopyButtonState(button, 'idle')
          button.classList.remove('is-error')
        }, 1600)
      }
    })
    pre.appendChild(button)
  }
}
// Chat passes `items` (proven group data); non-chat surfaces pass flat steps,
// which compose into the same tool-group timeline shape so the markup never
// branches on input source.
function withoutFailedActivityRows(
  items: ChatStreamTimelineItem[],
): ChatStreamTimelineItem[] {
  if (props.presentation !== 'activity') return items

  return items.flatMap((item): ChatStreamTimelineItem[] => {
    if (item.type !== 'tool-group') return [item]
    const documentApplyGroup = isDocumentApplyToolName(item.group.operationKey)

    const failedCalls = item.group.calls.filter(
      call => call.isError || call.status === 'error',
    )
    // Restored histories can retain only the group-level failure marker. In
    // that case none of the calls is safe to present as completed activity.
    if (
      (item.group.isError || item.group.status === 'error')
      && failedCalls.length === 0
      && !documentApplyGroup
    ) {
      return []
    }

    const calls = item.group.calls.filter(
      call => (
        (!call.isError && call.status !== 'error')
        || isDocumentApplyToolName(call.name)
      ),
    )
    if (calls.length === 0) return []

    const isRunning = calls.some(call => call.isRunning)
    const isError = calls.some(call => call.isError || call.status === 'error')
      || (documentApplyGroup && (item.group.isError || item.group.status === 'error'))
    return [{
      ...item,
      group: {
        ...item.group,
        calls,
        isRunning,
        isError,
        status: isError
          ? 'error'
          : isRunning
          ? ''
          : calls.every(call => call.status === 'success')
            ? 'success'
            : '',
      },
    }]
  })
}

function isDocumentApplyToolName(value: string | undefined): boolean {
  const normalized = String(value || '').trim().toLowerCase()
  if (!normalized) return false
  const segments = normalized.split(/[.:/]/)
  return segments[segments.length - 1] === 'document_apply'
}

const resolvedItems = computed<ChatStreamTimelineItem[]>(() => {
  const items = props.items ?? composeTree(props.steps ?? []).map((node): ChatStreamTimelineItem => {
    const members = node.children.length ? node.children.map(child => child.step) : [node.step]
    const calls = members.map(stepToRenderItem)
    const isError = calls.some(call => call.isError || call.status === 'error')
    const isRunning = calls.some(call => call.isRunning)
    const status: '' | 'success' | 'error' = isError
      ? 'error'
      : (calls.every(call => call.status === 'success') ? 'success' : '')
    const group: ChatToolCallGroup = {
      groupId: node.step.id,
      operationKey: node.step.operationKey,
      label: node.step.title,
      iconName: 'gear',
      calls,
      secondary: '',
      isRunning,
      isError,
      status,
    }
    return { type: 'tool-group', key: node.step.id, group }
  })
  return withoutFailedActivityRows(items)
})

function stepToRenderItem(step: NodeStep): ChatToolCallRenderItem {
  const status: '' | 'success' | 'error' = step.state === 'output-error'
    ? 'error'
    : (step.state === 'output-available' ? 'success' : '')
  return {
    toolId: step.id,
    renderKey: step.id,
    name: step.toolName || step.operationKey,
    displayName: step.title,
    inputRaw: step.input,
    inputPreview: step.inputPreview ?? '',
    isRunning: step.state === 'input-available',
    status,
    isError: step.isError ?? step.state === 'output-error',
    result: step.output ?? '',
    resultPreview: step.outputPreview ?? '',
    isOpen: false,
  }
}

const totalCalls = computed(() => resolvedItems.value.reduce(
  (count, item) => item.type === 'tool-group' ? count + item.group.calls.length : count,
  0,
))

// Cap rendered tool rows per turn; earliest calls collapse into one note.
const visibleItems = computed<TimelineRenderItem[]>(() => {
  if (showAllRows.value || totalCalls.value <= MAX_TOOL_ROWS) return resolvedItems.value
  let toHide = totalCalls.value - MAX_TOOL_ROWS
  const out: TimelineRenderItem[] = [{ type: 'overflow', key: 'overflow', hiddenCount: toHide }]
  for (const item of resolvedItems.value) {
    if (item.type !== 'tool-group' || toHide <= 0) {
      out.push(item)
      continue
    }
    if (item.group.calls.length <= toHide) {
      toHide -= item.group.calls.length
      continue
    }
    out.push({ ...item, group: { ...item.group, calls: item.group.calls.slice(toHide) } })
    toHide = 0
  }
  return out
})

type BulkToggleTarget =
  | { kind: 'group'; id: string; open: boolean }
  | { kind: 'item'; id: string; open: boolean }

type DefaultOpenSnapshot = {
  groups: Map<string, boolean>
  items: Map<string, boolean>
  topLevel: Map<string, DefaultOpenTarget>
}

type DefaultOpenTarget = {
  kind: 'group' | 'item'
  id: string
  defaultOpen: boolean
}

// A multi-call batch exposes one group disclosure; a single-call batch exposes
// the call itself. Use the effective state that RunTrace renders, not the
// caller-owned toggle bits that invert each row's default.
const bulkToggleTargets = computed<BulkToggleTarget[]>(() => {
  const targets: BulkToggleTarget[] = []
  for (const item of visibleItems.value) {
    if (item.type !== 'tool-group' || item.group.calls.length === 0) continue
    if (item.group.calls.length > 1) {
      targets.push({
        kind: 'group',
        id: groupStateId(item.group.groupId),
        open: groupOpen(item.group),
      })
      continue
    }
    const call = item.group.calls[0]
    targets.push({ kind: 'item', id: itemStateId(call.renderKey), open: callOpen(call) })
  }
  return targets
})

// Multi-call groups have a second disclosure level. Bulk expand/collapse owns
// those member details too, while the button label follows the visible
// top-level disclosures so a closed group never looks partially open.
const bulkMemberTargets = computed<BulkToggleTarget[]>(() => visibleItems.value.flatMap(item => {
  if (item.type !== 'tool-group' || item.group.calls.length <= 1) return []
  return item.group.calls.map(call => ({
    kind: 'item' as const,
    id: itemStateId(call.renderKey),
    open: callOpen(call),
  }))
}))

const showBulkControl = computed(
  () => props.showBulkToggle === true && bulkToggleTargets.value.length > 1,
)
const anyBulkTargetOpen = computed(() => bulkToggleTargets.value.some(target => target.open))
const bulkToggleLabel = computed(() => t(
  anyBulkTargetOpen.value ? 'chat.tool.collapseAll' : 'chat.tool.expandAll',
))
const visibleToolCallCount = computed(() => visibleItems.value.reduce(
  (count, item) => item.type === 'tool-group' ? count + item.group.calls.length : count,
  0,
))

// Keep the action next to the tools it controls without moving any preceding
// assistant text. A synthetic keyed item also keeps TransitionGroup children
// stable as the control appears during streaming.
const displayItems = computed<TimelineDisplayItem[]>(() => {
  const items: TimelineDisplayItem[] = [...visibleItems.value]
  if (!showBulkControl.value) return items
  const firstToolIndex = items.findIndex(item => item.type === 'tool-group')
  if (firstToolIndex < 0) return items
  items.splice(firstToolIndex, 0, {
    type: 'bulk-control',
    key: '__run-trace-bulk-control__',
    callCount: visibleToolCallCount.value,
  })
  return items
})

const defaultOpenSnapshot = computed<DefaultOpenSnapshot>(() => {
  const groups = new Map<string, boolean>()
  const items = new Map<string, boolean>()
  const topLevel = new Map<string, DefaultOpenTarget>()
  for (const item of resolvedItems.value) {
    if (item.type !== 'tool-group') continue
    for (const call of item.group.calls) {
      items.set(itemStateId(call.renderKey), callDefaultOpen(call))
    }
    if (item.group.calls.length > 1) {
      const defaultOpen = groupDefaultOpen(item.group)
      const stateId = groupStateId(item.group.groupId)
      groups.set(stateId, defaultOpen)
      topLevel.set(stateId, {
        kind: 'group',
        id: stateId,
        defaultOpen,
      })
    } else if (item.group.calls.length === 1) {
      const call = item.group.calls[0]
      topLevel.set(groupStateId(item.group.groupId), {
        kind: 'item',
        id: itemStateId(call.renderKey),
        defaultOpen: callDefaultOpen(call),
      })
    }
  }
  return { groups, items, topLevel }
})

// Toggle sets encode an inversion of the current default. When an error state
// or the global display preference changes that default, adjust existing bits
// so an explicit user choice does not flip underneath them.
watch(defaultOpenSnapshot, (current, previous) => {
  // A live operation starts as a single row and can become a group when a
  // second call arrives. Carry an explicit override to the disclosure that
  // replaces it; otherwise an expanded single row can suddenly collapse.
  for (const [groupId, target] of current.topLevel) {
    const previousTarget = previous.topLevel.get(groupId)
    if (!previousTarget
      || previousTarget.kind !== 'item'
      || target.kind !== 'group'
      || !hasToggleOverride(previousTarget)) {
      continue
    }
    const previousOpen = !previousTarget.defaultOpen
    const targetOverride = target.defaultOpen !== previousOpen
    if (hasToggleOverride(target) === targetOverride) continue
    toggleTargetOverride(target)
  }

  for (const [id, defaultOpen] of current.groups) {
    const previousDefault = previous.groups.get(id)
    if (previousDefault !== undefined
      && previousDefault !== defaultOpen
      && isGroupOpen(id)) {
      emit('toggleGroup', id)
    }
  }
  for (const [id, defaultOpen] of current.items) {
    const previousDefault = previous.items.get(id)
    if (previousDefault !== undefined
      && previousDefault !== defaultOpen
      && isItemOpen(id)) {
      emit('toggleItem', id)
    }
  }
}, { flush: 'sync' })

const codeBlockDecorationSignature = computed(() => visibleItems.value
  .map(item => item.type === 'text' ? `${item.key}:${item.html}` : item.key)
  .join('|'))

onMounted(async () => {
  await nextTick()
  decorateCodeBlocks()
})

watch(
  codeBlockDecorationSignature,
  async () => {
    await nextTick()
    decorateCodeBlocks()
  },
  { flush: 'post' },
)

function operationKey(call: ChatToolCallRenderItem): string {
  return toolOperationKey(call.name)
}

function callDefaultOpen(call: ChatToolCallRenderItem): boolean {
  if (call.isError || call.status === 'error') return true
  if (props.presentation === 'activity') return false
  if (toolDetailDisplayMode.value === 'compact') return false
  if (toolDetailDisplayMode.value === 'expanded') return true
  return !COLLAPSED_BY_DEFAULT.has(operationKey(call))
}

function callOpen(call: ChatToolCallRenderItem): boolean {
  // A recorded toggle inverts the default, so error auto-expand still honors
  // an explicit user collapse.
  return callDefaultOpen(call) !== isItemOpen(itemStateId(call.renderKey))
}

function groupDefaultOpen(group: ChatToolCallGroup): boolean {
  return group.calls.some(callDefaultOpen)
}

function groupStateId(groupId: string): string {
  return props.stateScope ? `${props.stateScope}:${groupId}` : groupId
}

function itemStateId(renderKey: string): string {
  return props.stateScope ? `${props.stateScope}:${renderKey}` : renderKey
}

function groupOpen(group: ChatToolCallGroup): boolean {
  return groupDefaultOpen(group) !== isGroupOpen(groupStateId(group.groupId))
}

function isGroupOpen(groupId: string): boolean {
  return props.isToolGroupOpen?.(groupId) ?? false
}

function isItemOpen(renderKey: string): boolean {
  return props.isToolItemOpen?.(renderKey) ?? false
}

function hasToggleOverride(target: DefaultOpenTarget): boolean {
  return target.kind === 'group' ? isGroupOpen(target.id) : isItemOpen(target.id)
}

function toggleTargetOverride(target: DefaultOpenTarget) {
  if (target.kind === 'group') emit('toggleGroup', target.id)
  else emit('toggleItem', target.id)
}

function toggleGroupDisclosure(group: ChatToolCallGroup) {
  emit('toggleGroup', groupStateId(group.groupId))
}

function toggleItemDisclosure(call: ChatToolCallRenderItem) {
  emit('toggleItem', itemStateId(call.renderKey))
}

function toggleAllTools() {
  const targetOpen = !anyBulkTargetOpen.value
  const targets = [...bulkToggleTargets.value, ...bulkMemberTargets.value]
  for (const target of targets) {
    if (target.open === targetOpen) continue
    if (target.kind === 'group') emit('toggleGroup', target.id)
    else emit('toggleItem', target.id)
  }
}

function iconFor(call: ChatToolCallRenderItem): StatusVisual {
  return statusVisual(toolState(call))
}

function rowClass(call: ChatToolCallRenderItem) {
  return {
    'tool-row--running': call.isRunning,
    'tool-row--error': call.status === 'error' || call.isError,
    'is-open': callOpen(call),
  }
}

function bulletClass(call: ChatToolCallRenderItem) {
  return {
    'tool-row__bullet--running': call.isRunning,
    'tool-row__bullet--ok': props.presentation !== 'activity' && call.status === 'success',
    'tool-row__bullet--err': call.status === 'error' || call.isError,
  }
}

function groupBulletClass(group: ChatToolCallGroup) {
  return {
    'tool-row__bullet--running': group.isRunning,
    'tool-row__bullet--ok': props.presentation !== 'activity' && group.status === 'success',
    'tool-row__bullet--err': group.isError,
  }
}

function activityIconClass(call: ChatToolCallRenderItem) {
  return {
    'tool-row__activity-icon--running': call.isRunning,
    'tool-row__activity-icon--error': call.status === 'error' || call.isError,
  }
}

function activityGroupIconClass(group: ChatToolCallGroup) {
  return {
    'tool-row__activity-icon--running': group.isRunning,
    'tool-row__activity-icon--error': group.isError,
  }
}

function callHasDetails(call: ChatToolCallRenderItem): boolean {
  return Boolean(
    call.inputRaw
    || call.inputPreview
    || call.result
    || call.resultPreview,
  )
}

function showGroupStatus(group: ChatToolCallGroup): boolean {
  return props.presentation !== 'activity'
    || group.isError
    || group.status === 'error'
}

function singleCallSecondary(
  group: ChatToolCallGroup,
  call: ChatToolCallRenderItem,
): string {
  return props.presentation === 'activity'
    ? group.secondary
    : resolvedSecondaryText(call)
}

function resultCountText(call: ChatToolCallRenderItem): string {
  if (call.isRunning || call.isError) return ''
  const count = toolResultCount(call.result, call.name)
  return count === null ? '' : t('shared.runTrace.resultsCount', { count })
}

function elapsedFor(call: ChatToolCallRenderItem): string {
  return props.toolElapsedText?.(call) || ''
}

function resolvedGroupStatusText(group: ChatToolCallGroup): string {
  const fallback = defaultToolGroupStatusText(group)
  const injected = (props.toolGroupStatusText ?? defaultToolGroupStatusText)(group).trim()
  if (
    props.presentation === 'activity'
    && group.isError
    && (!injected || injected === fallback)
  ) {
    return t('shared.runTrace.activityNotCompleted')
  }
  return injected || fallback
}

function resolvedSecondaryText(call: ChatToolCallRenderItem): string {
  return (props.toolSecondaryText ?? defaultToolSecondaryText)(call)
}

function activityTerminalStatusText(call: ChatToolCallRenderItem): string {
  if (props.presentation !== 'activity' || (!call.isError && call.status !== 'error')) {
    return ''
  }
  const fallback = defaultToolStatusText(call)
  const injected = props.toolStatusText?.(call)?.trim()
  if (!injected || injected === fallback) {
    return t('shared.runTrace.activityNotCompleted')
  }
  return injected
}

function forwardShowResult(content: string, title: string, context?: ToolResultContext) {
  emit('showResult', content, title, context)
}

// summary strip. The whole-run status rolls up to a dot tone + label; unknown
// scalars render an em-dash, never 0/blank.
const STATUS_TONE: Record<RunTraceStatus, string> = {
  idle: 'idle',
  queued: 'warn',
  running: 'running',
  success: 'ok',
  error: 'err',
  cancelled: 'idle',
}

const STATUS_LABEL_KEYS: Record<RunTraceStatus, string> = {
  idle: 'shared.runTrace.statusIdle',
  queued: 'shared.runTrace.statusQueued',
  running: 'shared.runTrace.statusRunning',
  success: 'shared.runTrace.statusDone',
  error: 'shared.runTrace.statusFailed',
  cancelled: 'shared.runTrace.statusCancelled',
}

const statusTone = computed(() => {
  const status = props.summary?.status
  return status ? STATUS_TONE[status] : 'idle'
})

const statusLabel = computed(() => {
  const status = props.summary?.status
  return status ? t(STATUS_LABEL_KEYS[status]) : '—'
})

function fmtMs(ms?: number | null): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${+seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}m ${rest}s`
}

function fmtTok(n?: number | null): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${+(n / 1_000).toFixed(1)}k`
  return String(n)
}
</script>

<style scoped>
.msg-ai-text {
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--text);
  word-break: break-word;
  margin-bottom: 0.5rem;
}

.msg-ai-text :deep(p) { margin: 0.375rem 0; }
.msg-ai-text :deep(p:first-child) { margin-top: 0; }
.msg-ai-text :deep(ul), .msg-ai-text :deep(ol) { margin: 0.375rem 0; padding-left: 1.25rem; }
.msg-ai-text :deep(li) { margin: 0.125rem 0; }
.msg-ai-text :deep(code) {
  background: var(--bg-hover);
  padding: 0.0625rem 0.25rem;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  color: var(--text-muted);
}
.msg-ai-text :deep(pre) {
  background: var(--code-block-bg);
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-md);
  padding: 0.625rem;
  overflow-x: auto;
  margin: 0.375rem 0;
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 4%, transparent);
}
.msg-ai-text :deep(pre.code-block) {
  position: relative;
  padding-top: 2.375rem;
  background: linear-gradient(
    to bottom,
    var(--code-block-header-bg) 0,
    var(--code-block-header-bg) 1.75rem,
    var(--code-block-bg) 1.75rem,
    var(--code-block-bg) 100%
  );
}

.msg-ai-text :deep(pre.code-block > .code-lang) {
  top: 0.375rem;
  right: 2.5rem;
  line-height: 1rem;
  background: transparent;
  color: var(--text-dim);
}

.msg-ai-text :deep(.code-copy-btn) {
  position: absolute;
  top: 0;
  right: 0.25rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.75rem;
  height: 1.75rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  opacity: 0.78;
  cursor: pointer;
  transition: color var(--transition), background var(--transition), opacity var(--transition);
}

.msg-ai-text :deep(.code-copy-btn svg) {
  display: block;
  width: 0.9375rem;
  height: 0.9375rem;
}

.msg-ai-text :deep(.code-copy-btn:hover) {
  color: var(--text);
  opacity: 1;
  background: var(--bg-hover);
}

.msg-ai-text :deep(.code-copy-btn:focus-visible) {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-ai-text :deep(.code-copy-btn.is-copied) {
  color: var(--ok);
  opacity: 1;
}

.msg-ai-text :deep(.code-copy-btn.is-error) {
  color: var(--danger);
  opacity: 1;
}
.msg-ai-text :deep(pre code) {
  background: transparent;
  padding: 0;
}

.step-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 0.25rem;
  overflow: hidden;
  margin: 0.625rem 0;
  box-shadow: var(--shadow-xs);
}

.step-group {
  border-radius: var(--radius-md);
}

.tool-timeline__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  width: 100%;
  min-width: 0;
  min-height: 2rem;
  padding: 0 var(--sp-1) var(--sp-1);
  border-bottom: 1px solid var(--hairline);
  color: var(--text-dim);
}

.tool-timeline__summary,
.tool-timeline__bulk-action {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  line-height: 1.2;
  white-space: nowrap;
}

.tool-timeline__summary {
  min-width: 0;
}

.tool-timeline__bulk-action {
  flex: 0 0 auto;
  min-height: 2rem;
  padding: 0 var(--sp-1);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 500;
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-standard),
    color var(--dur-fast) var(--ease-standard);
}

.tool-timeline__bulk-action:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.tool-timeline__bulk-action:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
}

.tool-timeline__bulk-icon {
  transition: transform var(--dur-fast) var(--ease-standard);
}

.tool-timeline__bulk-icon.is-collapse {
  transform: rotate(180deg);
}

.tool-timeline__toolbar + .step-card {
  margin-top: var(--sp-1);
}

.tool-overflow-note {
  display: block;
  margin: 0.375rem 0;
  padding: 0.25rem 0.5rem;
  border: 0;
  background: transparent;
  font: inherit;
  font-size: 0.8125rem;
  color: var(--text-muted);
  cursor: pointer;
  text-align: left;
}

.tool-overflow-note:hover {
  color: var(--text-muted);
  text-decoration: underline;
}

.tool-overflow-note:focus-visible {
  outline: none;
  border-radius: var(--radius-sm);
  box-shadow: var(--focus-ring);
}

.tool-row {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  width: 100%;
  padding: 0.625rem 0.875rem;
  cursor: pointer;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  font: inherit;
  text-align: left;
  transition: background var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
  min-height: 2.5rem;
  color: inherit;
}

.tool-row:hover {
  background: var(--bg-hover);
}

.tool-row:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
}

.tool-row.is-open,
.step-group.is-open > .tool-row--group {
  background: var(--bg-elevated);
}

.tool-row--running {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

.tool-row--member {
  padding: 0.5625rem 0.75rem;
}

.tool-row__bullet {
  width: 0.4375rem;
  height: 0.4375rem;
  border-radius: var(--radius-full);
  background: var(--text-dim);
  flex-shrink: 0;
}

.tool-row__bullet--running {
  background: var(--accent);
  animation: live-pulse var(--dur-pulse) var(--ease-standard) infinite;
}

.tool-row__bullet--ok {
  background: var(--ok);
}

.tool-row__bullet--err {
  background: var(--danger);
}

.tool-row__label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--text);
  line-height: 1.4;
  flex-shrink: 0;
}

.tool-row__label--member {
  font-size: 0.765625rem;
  color: var(--text-muted);
  max-width: 14rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-row--error .tool-row__label,
.tool-row--error .tool-row__status {
  color: var(--danger);
}

.tool-row__arg {
  min-width: 0;
  flex: 1;
  color: var(--text-dim);
  font-size: 0.8125rem;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-row__trailing {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  flex-shrink: 0;
  margin-left: auto;
  color: var(--text-dim);
}

.tool-row__status {
  font-size: 0.8125rem;
  color: var(--text-dim);
  white-space: nowrap;
}

.tool-row__elapsed {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 0.6875rem;
  line-height: 1.3;
  padding: 0.0625rem 0.375rem;
  border-radius: var(--radius-full);
  color: var(--text-muted);
  background: var(--bg-hover);
  white-space: nowrap;
}

.tool-row--running .tool-row__elapsed {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.tool-row__state-icon--ok {
  color: var(--ok);
}

.tool-row__state-icon--err {
  color: var(--danger);
}

.step-count {
  flex-shrink: 0;
  font-size: 0.6875rem;
  line-height: 1.3;
  padding: 0.0625rem 0.375rem;
  border-radius: var(--radius-full);
  color: var(--text-muted);
  background: var(--bg-hover);
}

.step-group-members {
  margin: 0.125rem 0 0.25rem;
  padding-left: 1.25rem;
}

.step-group-members::before {
  content: '';
  display: block;
  width: calc(100% - 1.25rem);
  height: 1px;
  margin: 0 0 0.125rem 1.25rem;
  background: var(--hairline);
}

.tool-row-body {
  padding: 0 0.875rem 0.5rem;
}

.step-chevron {
  transition: transform var(--dur-fast) var(--ease-standard);
}

.tool-row.is-open .step-chevron,
.step-group.is-open > .tool-row--group .step-chevron {
  transform: rotate(90deg);
}

/* ── Run summary strip (Status / Executor / Time / Tokens / Steps) ──────
   Reuses the global control-stat / control-stat-grid primitives; only the
   status dot, skeleton, and the value text overrides live here. */
.run-trace__summary {
  margin: 0 0 0.625rem;
}

.run-trace__summary .control-stat__value {
  align-items: center;
  display: flex;
  font-size: 0.9375rem;
  gap: 0.375rem;
}

.run-trace__summary-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-trace__dot {
  border-radius: var(--radius-full);
  flex-shrink: 0;
  height: 0.5rem;
  width: 0.5rem;
}

.run-trace__dot--ok { background: var(--ok); }
.run-trace__dot--err { background: var(--danger); }
.run-trace__dot--warn { background: var(--warn-fill); }
.run-trace__dot--running {
  background: var(--accent);
  animation: live-pulse var(--dur-pulse) var(--ease-standard) infinite;
}
.run-trace__dot--idle { background: var(--text-dim); }

.run-trace__skeleton {
  background: linear-gradient(
    90deg,
    var(--bg-hover) 25%,
    var(--bg-elevated) 50%,
    var(--bg-hover) 75%
  );
  background-size: 200% 100%;
  border-radius: var(--radius-sm);
  display: inline-block;
  height: 1em;
  width: 70%;
  animation: run-trace-shimmer 1.4s ease-in-out infinite;
}

@keyframes run-trace-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── Checklist variant (live work card) ───────────────────────────────
   The wrapper is layout-neutral for history; in the work card it stacks
   the tool rows into a single vertical sequence the eye can track. */
.tool-timeline {
  display: contents;
}

.tool-timeline--checklist {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
}

.tool-timeline--checklist .tool-timeline__toolbar {
  margin-bottom: var(--sp-1);
}

/* Flatten the per-group card chrome so the rows read as one running list. */
.tool-timeline--checklist .step-card {
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

/* A running row earns a steady outlined ring; completed rows dim so attention
   stays on what is in flight. The single live signal is the pulsing bullet —
   the ring is static so a running row carries one rhythm, not two. */
.tool-timeline--checklist .tool-row--running {
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 44%, transparent);
  border-radius: var(--radius-md);
}

.tool-timeline--checklist .tool-row__bullet--ok {
  animation: checklistCheckIn var(--dur-base) var(--ease-press, ease-out) both;
}

.tool-timeline--checklist .tool-row__state-icon--ok {
  animation: checklistCheckIn var(--dur-base) var(--ease-press, ease-out) both;
}

/* ── Activity presentation (chat's intermediate work) ─────────────────
   State is expressed through type strength and the status dot, never another
   nested card. Values are direct colors rather than parent opacity so muted
   rows do not get dimmed twice. */
.tool-timeline--activity .step-card {
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

.tool-timeline--activity .step-group {
  border-radius: 0;
}

.tool-timeline--activity .tool-row,
.tool-timeline--activity .tool-row--running,
.tool-timeline--activity .tool-row--error {
  min-height: 1.75rem;
  padding: 0.25rem 0.125rem;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

.tool-timeline--activity.tool-timeline--checklist
  .tool-row:not(.tool-row--running):not(.tool-row--error):not(.is-open),
.tool-timeline--activity.tool-timeline--checklist
  .tool-row:not(.tool-row--running):not(.tool-row--error):not(.is-open):hover {
  opacity: 1;
}

.tool-timeline--activity .tool-row__label {
  color: var(--text-muted);
  font-size: 0.8125rem;
}

.tool-timeline--activity .tool-row__arg,
.tool-timeline--activity .tool-row__trailing,
.tool-timeline--activity .tool-row__status {
  color: var(--text-muted);
}

.tool-timeline--activity .tool-row--error .tool-row__status {
  color: var(--warn);
}

.tool-timeline--activity .tool-row__arg {
  flex: 0 1 auto;
}

.tool-timeline--activity .tool-row__trailing {
  margin-left: 0;
}

.tool-timeline--activity .tool-row__activity-icon {
  color: color-mix(in srgb, var(--text) 46%, transparent);
  transform-origin: center;
  transition:
    color var(--dur-fast) var(--ease-standard),
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.tool-timeline--activity .tool-row:hover .tool-row__activity-icon,
.tool-timeline--activity .tool-row:focus-visible .tool-row__activity-icon,
.tool-timeline--activity .tool-row.is-open .tool-row__activity-icon {
  color: color-mix(in srgb, var(--text) 62%, transparent);
}

/* The icon breathes locally while this exact call is live. It is deliberately
   opacity/scale-only: enough feedback to make appended calls feel active,
   without moving copy or adding another progress label. */
.tool-timeline--activity .tool-row__activity-icon--running {
  color: var(--accent);
  animation: activity-tool-breathe var(--dur-pulse) var(--ease-standard) infinite;
}

.tool-timeline--activity .tool-row__activity-icon--error {
  color: var(--warn);
}

.tool-timeline--activity .tool-row__activity-arrow {
  flex: 0 0 auto;
  color: color-mix(in srgb, var(--text) 46%, transparent);
  opacity: 0;
  transform: translateX(-0.125rem);
  transition:
    color var(--dur-fast) var(--ease-standard),
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.tool-timeline--activity .tool-row:hover .tool-row__activity-arrow,
.tool-timeline--activity .tool-row:focus-visible .tool-row__activity-arrow {
  opacity: 0.8;
  transform: translateX(0);
}

.tool-timeline--activity .tool-row.is-open .tool-row__activity-arrow,
.tool-timeline--activity
  .step-group.is-open
  > .tool-row--group
  .tool-row__activity-arrow {
  opacity: 0.55;
  transform: rotate(90deg);
}

.tool-timeline--activity .tool-row.is-open:hover .tool-row__activity-arrow,
.tool-timeline--activity .tool-row.is-open:focus-visible .tool-row__activity-arrow,
.tool-timeline--activity
  .step-group.is-open
  > .tool-row--group:hover
  .tool-row__activity-arrow,
.tool-timeline--activity
  .step-group.is-open
  > .tool-row--group:focus-visible
  .tool-row__activity-arrow {
  opacity: 0.8;
}

.tool-timeline--activity .tool-row--running .tool-row__label {
  color: var(--text);
}

.tool-timeline--activity .tool-row--running .tool-row__arg {
  color: var(--text-muted);
}

.tool-timeline--activity .tool-row__elapsed,
.tool-timeline--activity .tool-row--running .tool-row__elapsed {
  padding: 0;
  background: transparent;
}

.tool-timeline--activity
  .tool-row:not(.tool-row--running):not(.tool-row--error).is-open,
.tool-timeline--activity
  .step-group:not(.step-group--running):not(.step-group--error).is-open
  > .tool-row--group {
  background: transparent;
}

/* One indent scale for the whole fold: a row's text starts after its icon at
   0.125rem padding + 0.875rem icon + 0.625rem gap = 1.625rem, and everything
   subordinate to that row (detail bodies, narration, member rows) aligns to
   the same origin. Each nesting level therefore adds 1.5rem (icon + gap);
   the fold's containment comes from the disclosure body's single left rule,
   not from per-level rules here. */
.tool-timeline--activity .tool-row-body {
  padding: 0 0 0.125rem 1.625rem;
}

.tool-timeline--activity .activity-tool-detail-enter-active {
  transform-origin: top left;
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
  will-change: opacity, transform;
}

.tool-timeline--activity .activity-tool-detail-leave-active {
  transform-origin: top left;
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
  will-change: opacity, transform;
}

.tool-timeline--activity .activity-tool-detail-enter-from,
.tool-timeline--activity .activity-tool-detail-leave-to {
  opacity: 0;
  transform: translateY(-0.25rem);
}

.tool-timeline--activity .step-group-members {
  padding-left: 1.5rem;
}

.tool-timeline--activity .step-group-members::before {
  display: none;
}

.tool-timeline--activity .msg-ai-text {
  margin: 0.125rem 0 0.25rem 1.625rem;
  color: var(--text-muted);
  font-size: 0.8125rem;
  line-height: 1.55;
}

.tool-timeline--activity .msg-ai-text :deep(p) {
  margin: 0;
}

.tool-timeline--activity .msg-ai-text + .msg-ai-text {
  margin-top: 0.5rem;
}

/* Completed, non-open rows soften and tuck in — kept for traceability, not
   deleted — so the running row reads as the live focus. */
.tool-timeline--checklist .tool-row-wrap:has(.tool-row--running) {
  opacity: 1;
}

.tool-timeline--checklist
  .tool-row:not(.tool-row--running):not(.tool-row--error):not(.is-open) {
  opacity: 0.62;
  transition: opacity var(--dur-base) var(--ease-standard);
}

.tool-timeline--checklist
  .tool-row:not(.tool-row--running):not(.tool-row--error):not(.is-open):hover {
  opacity: 1;
}

.tool-timeline--checklist .tool-row--error {
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 36%, transparent);
  border-radius: var(--radius-md);
}

.tool-timeline--activity.tool-timeline--checklist .tool-row--error {
  border-radius: 0;
  box-shadow: none;
}

@keyframes checklistCheckIn {
  0% { transform: scale(0.4); opacity: 0; }
  60% { transform: scale(1.12); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}

@keyframes activity-tool-breathe {
  0%, 100% {
    opacity: 0.68;
    transform: scale(0.94);
  }
  50% {
    opacity: 1;
    transform: scale(1);
  }
}

/* Calls appended to an already-open batch previously appeared in place because
   the outer group key did not change. Animate the member collection itself so
   each new local action is perceptible at the moment it arrives. */
.tool-member-enter-from {
  opacity: 0;
  transform: translateY(0.25rem);
}

.tool-member-enter-active,
.tool-member-move {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

/* ── Tool-row enter transition ─────────────────────────────────────────
   New rows slide up from a few pixels below and fade in. Leave is
   instant so completed rows don't visually linger. The TransitionGroup
   container is display:contents so the rows stay direct layout children of
   .tool-timeline exactly as before — the group element adds no box. */
.tool-row-group {
  display: contents;
}

.tool-row-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.tool-row-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

/* Touch devices have no hover to reveal the activity chevron, so rest it at
   a visible opacity there — otherwise the row reads as inert text. The
   higher-specificity .is-open rules above still win, keeping an open row's
   rotated chevron. */
@media (hover: none) {
  .tool-timeline--activity .tool-row__activity-arrow {
    opacity: 0.55;
    transform: translateX(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .tool-row__bullet--running {
    animation: none;
  }

  .tool-row__activity-icon,
  .tool-row__activity-arrow {
    transition: none;
  }

  .tool-row__activity-icon--running {
    animation: none;
  }

  .tool-member-enter-active,
  .tool-member-move {
    transition: none;
  }

  .tool-timeline--activity .activity-tool-detail-enter-active,
  .tool-timeline--activity .activity-tool-detail-leave-active {
    transition: none;
  }

  .tool-timeline--activity .activity-tool-detail-enter-from,
  .tool-timeline--activity .activity-tool-detail-leave-to {
    opacity: 1;
    transform: none;
  }

  .tool-timeline--checklist .tool-row--running,
  .tool-timeline--checklist .tool-row__bullet--ok,
  .tool-timeline--checklist .tool-row__state-icon--ok {
    animation: none;
  }

  .tool-timeline--checklist
    .tool-row:not(.tool-row--running):not(.tool-row--error):not(.is-open) {
    transition: none;
  }

  .run-trace__dot--running {
    animation: none;
  }

  .run-trace__skeleton {
    animation: none;
    background: var(--bg-hover);
  }

  .tool-row-enter-active {
    transition: none;
  }

  .tool-timeline__bulk-action,
  .tool-timeline__bulk-icon {
    transition: none;
  }
}
</style>

<!-- The expanded-row section content (labels, pre, "view full" button) is built
     by the ToolRowSections child via render functions h(), so those elements
     never receive a scoped data-v attribute and scoped rules cannot reach them
     (the button would fall back to native chrome — a white box on the dark
     surface). Their styling lives here, non-scoped. Tokens only — the
     chat-color guard covers this path. -->
<style>
.tool-row-section {
  margin-top: 0.5rem;
  padding: 0.5rem 0.625rem;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.tool-row-section--error {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 30%, var(--border));
}

.tool-row-section__label {
  font-size: 0.6875rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin-bottom: 0.25rem;
}

.tool-row-section--error .tool-row-section__label {
  color: var(--danger);
}

.tool-row-section__pre {
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 100px;
  overflow-y: auto;
  margin: 0;
}

.tool-row-section__compact {
  display: grid;
  gap: 0.25rem;
  min-width: 0;
}

.tool-row-section__compact-meta {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  line-height: 1.35;
}

.tool-row-section__compact-snippet {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.71875rem;
  line-height: 1.45;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-view-btn {
  margin-top: 0.25rem;
  padding: 0.125rem 0.375rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--accent);
  font: inherit;
  font-size: 0.6875rem;
  cursor: pointer;
}

.step-view-btn:hover {
  text-decoration: underline;
}

.step-view-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
</style>
