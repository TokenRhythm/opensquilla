<template>
  <div class="goal-outcome" :data-status="goal.status" role="status">
    <span class="goal-outcome__icon" aria-hidden="true">
      <Icon name="target" :size="14" />
    </span>
    <span class="goal-outcome__title">{{ titleText }}</span>
    <span v-if="elapsed" class="goal-outcome__meta">{{ elapsed }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'

const props = defineProps<{
  goal: GoalSnapshot
  elapsed: string
}>()

const { t } = useI18n()

const titleText = computed(() => {
  switch (props.goal.status) {
    case 'complete': return t('chat.goal.completeTitle')
    case 'blocked': return t('chat.goal.blockedTitle')
    case 'cancelled': return t('chat.goal.cancelledTitle')
    default: return t('chat.goal.completeTitle')
  }
})
</script>

<style scoped>
.goal-outcome {
  display: flex;
  align-items: center;
  gap: 8px;
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  max-width: calc(100% - 48px);
  box-sizing: border-box;
  margin: var(--sp-2, 8px) auto;
  padding: 6px 0;
  font-size: 0.8125rem;
  line-height: 1.4;
  color: var(--text-muted, var(--muted));
}
.goal-outcome__icon {
  flex: 0 0 auto;
  display: inline-flex;
  color: var(--ok, var(--accent));
}
.goal-outcome[data-status='blocked'],
.goal-outcome[data-status='cancelled'] {
  color: var(--danger, var(--text-muted, var(--muted)));
}
.goal-outcome[data-status='blocked'] .goal-outcome__icon,
.goal-outcome[data-status='cancelled'] .goal-outcome__icon {
  color: var(--danger, var(--accent));
}
.goal-outcome__title {
  flex: 0 0 auto;
  font-weight: 600;
  color: var(--text, var(--text-muted, var(--muted)));
  white-space: nowrap;
}
.goal-outcome__meta {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
</style>
