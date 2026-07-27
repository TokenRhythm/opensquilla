<template>
  <div class="activity-tool-details">
    <div
      v-if="projection.lines.length"
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
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'
import {
  projectActivityToolDetail,
  type ActivityToolDetailLine,
} from '@/utils/chat/activityToolDetails'

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
</style>
