<template>
  <div
    class="goal-outcome"
    :class="{ 'goal-outcome--inline': inline }"
    :data-status="goal.status"
  >
    <span class="goal-outcome__summary" role="status">
      <span class="goal-outcome__icon" aria-hidden="true">
        <Icon :name="inline ? 'check' : 'target'" :size="inline ? 11 : 14" />
      </span>
      <span class="goal-outcome__title">{{ titleText }}</span>
      <span
        v-if="!inline"
        class="goal-outcome__objective"
        :title="goal.objective"
      >
        {{ goal.objective }}
      </span>
      <span v-if="metaText && !inline" class="goal-outcome__meta">{{ metaText }}</span>
    </span>

  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'

const props = withDefaults(defineProps<{
  goal: GoalSnapshot
  elapsed: string
  inline?: boolean
}>(), {
  inline: false,
})

const { t } = useI18n()

const titleText = computed(() => props.inline
  ? t('chat.goal.achievedIn', { duration: props.elapsed || '0s' })
  : t('chat.goal.completeTitle'))

const metaText = computed(() => {
  const parts: string[] = []
  if (props.elapsed) parts.push(t('chat.goal.activeTime', { duration: props.elapsed }))
  if (props.goal.turnsSettled > 0) {
    parts.push(t('chat.goal.turns', { turns: props.goal.turnsSettled }))
  }
  if (props.goal.usage.totalTokens > 0) {
    parts.push(t('chat.goal.tokens', { tokens: props.goal.usage.totalTokens.toLocaleString() }))
  }
  return parts.join(' · ')
})
</script>

<style scoped>
.goal-outcome {
  display: flex;
  align-items: center;
  gap: 10px;
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  max-width: calc(100% - 48px);
  box-sizing: border-box;
  margin: var(--sp-2, 8px) auto;
  padding: 6px 0;
  font-size: 0.8125rem;
  line-height: 1.4;
  color: var(--text-muted, var(--muted));
}
.goal-outcome--inline {
  display: inline-flex;
  width: auto;
  max-width: 100%;
  margin: 0;
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--text-dim);
}
.goal-outcome__summary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.goal-outcome__summary {
  flex: 1 1 auto;
}
.goal-outcome__icon {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--ok, var(--accent));
}
.goal-outcome--inline .goal-outcome__icon {
  width: 1rem;
  height: 1rem;
  border: 1px solid currentColor;
  border-radius: var(--radius-full);
}
.goal-outcome__title {
  flex: 0 0 auto;
  font-weight: 600;
  color: var(--text, var(--text-muted, var(--muted)));
  white-space: nowrap;
}
.goal-outcome--inline .goal-outcome__title {
  color: inherit;
  font-weight: 400;
}
.goal-outcome__meta {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.goal-outcome__objective {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 720px) {
  .goal-outcome {
    align-items: stretch;
    flex-direction: column;
  }
  .goal-outcome--inline {
    display: flex;
  }
}
</style>
