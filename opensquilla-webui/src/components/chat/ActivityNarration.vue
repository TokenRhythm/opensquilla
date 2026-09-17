<template>
  <details
    v-if="shouldFold"
    class="activity-narration activity-narration--folded"
    :class="{ 'activity-narration--technical': technical }"
    data-activity-entry="true"
    data-activity-type="segment"
    :data-activity-id="item.key"
    :data-activity-order="item.activityOrder"
  >
    <summary class="activity-narration__summary">
      <Icon
        class="activity-narration__chevron"
        name="chevronRight"
        :size="12"
        aria-hidden="true"
      />
      <span class="activity-narration__summary-text">
        {{ technical ? t('chat.activityTechnicalDetails') : preview }}
      </span>
      <span v-if="!technical" class="activity-narration__hint">
        {{ t('shared.runTrace.activityViewDetails') }}
      </span>
    </summary>
    <div class="activity-narration__body" v-html="item.html" />
  </details>
  <div
    v-else
    class="activity-narration activity-narration--plain"
    data-activity-entry="true"
    data-activity-type="segment"
    :data-activity-id="item.key"
    :data-activity-order="item.activityOrder"
    v-html="item.html"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatStreamTimelineItem } from '@/types/chat'

const props = defineProps<{
  item: Extract<ChatStreamTimelineItem, { type: 'text' }>
}>()

const { t } = useI18n()

const normalizedText = computed(() =>
  String(props.item.rawText || '')
    .replace(/\s+/g, ' ')
    .trim(),
)

const technical = computed(() => {
  const text = normalizedText.value
  if (!text) return false
  return [
    /(?:^|\s)(?:exit[_ -]?code|stdout|stderr|traceback|stack trace|permission denied|eperm|enoent)\b/i,
    /(?:^|\s)(?:npm|pnpm|yarn|git|code-task)\s/i,
    /(?:\/users\/|\/library\/|[a-z]:\\|\.vue\b|\.tsx?\b|\.py\b|\.json\b)/i,
    /\b(?:session|task|process|pid|rpc|sandbox|profile[- ]?lock)\s*[:=#]/i,
    /```|`[^`]{2,}`|--[a-z][\w-]*/i,
  ].some(pattern => pattern.test(text))
})

const shouldFold = computed(() => {
  const raw = String(props.item.rawText || '')
  return technical.value
    || normalizedText.value.length > 280
    || raw.split(/\r\n|\r|\n/).length > 3
})

const preview = computed(() => {
  const text = normalizedText.value
  if (text.length <= 170) return text
  const sentence = text.slice(0, 180).match(/^(.{40,170}?[。！？.!?])(?:\s|$)/)?.[1]
  return `${(sentence || text.slice(0, 170)).trim()}…`
})
</script>

<style scoped>
.activity-narration {
  min-width: 0;
  color: var(--text-muted);
  font-size: 0.8125rem;
  line-height: 1.58;
}

.activity-narration--plain {
  margin: 0.375rem 0 0.625rem 1.625rem;
}

.activity-narration--plain :deep(p),
.activity-narration__body :deep(p) {
  margin: 0;
}

.activity-narration--plain :deep(p + p),
.activity-narration__body :deep(p + p) {
  margin-top: 0.5rem;
}

.activity-narration--folded {
  margin: 0.125rem 0 0.375rem;
}

.activity-narration__summary {
  display: flex;
  align-items: center;
  min-height: 1.75rem;
  gap: 0.5rem;
  padding: 0.25rem 0.125rem;
  color: color-mix(in srgb, var(--text) 68%, transparent);
  cursor: pointer;
  list-style: none;
}

.activity-narration__summary::-webkit-details-marker {
  display: none;
}

.activity-narration__summary:hover {
  color: var(--text);
}

.activity-narration__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.activity-narration__chevron {
  flex: 0 0 auto;
  margin: 0 0.125rem;
  opacity: 0.5;
  transition: transform var(--dur-fast) var(--ease-standard);
}

.activity-narration[open] > .activity-narration__summary .activity-narration__chevron {
  transform: rotate(90deg);
}

.activity-narration__summary-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-narration__hint {
  flex: 0 0 auto;
  margin-left: auto;
  color: var(--text-dim);
  font-size: 0.75rem;
}

.activity-narration--technical .activity-narration__summary {
  color: var(--text-dim);
}

.activity-narration__body {
  max-height: 18rem;
  margin: 0.25rem 0 0.625rem 1.625rem;
  padding-right: 0.5rem;
  overflow-y: auto;
  color: var(--text-muted);
  white-space: normal;
  word-break: break-word;
}

@media (prefers-reduced-motion: reduce) {
  .activity-narration__chevron {
    transition: none;
  }
}
</style>
