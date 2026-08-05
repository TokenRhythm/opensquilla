<template>
  <div class="goal-ribbon" :data-status="goal.status" role="status" aria-live="polite">
    <span class="goal-ribbon__icon" aria-hidden="true">
      <Icon name="target" :size="15" />
    </span>
    <span class="goal-ribbon__title">{{ titleText }}</span>
    <span class="goal-ribbon__text" :title="goal.goalText">{{ goal.goalText }}</span>
    <span v-if="metaText" class="goal-ribbon__meta">{{ metaText }}</span>
    <span class="goal-ribbon__actions">
      <button
        v-if="goal.status === 'running'"
        type="button"
        class="goal-ribbon__action"
        :title="t('chat.goal.pause')"
        :aria-label="t('chat.goal.pause')"
        :disabled="busy"
        @click="emit('pause')"
      >
        <Icon name="pause" :size="13" />
      </button>
      <button
        v-else-if="goal.status === 'paused'"
        type="button"
        class="goal-ribbon__action"
        :title="t('chat.goal.resume')"
        :aria-label="t('chat.goal.resume')"
        :disabled="busy"
        @click="emit('resume')"
      >
        <Icon name="play" :size="13" />
      </button>
      <button
        type="button"
        class="goal-ribbon__action"
        :title="t('chat.goal.clear')"
        :aria-label="t('chat.goal.clear')"
        :disabled="busy"
        @click="emit('clear')"
      >
        <Icon name="trash" :size="13" />
      </button>
      <button
        type="button"
        class="goal-ribbon__action"
        :title="t('chat.goal.dismiss')"
        :aria-label="t('chat.goal.dismiss')"
        @click="emit('dismiss')"
      >
        <Icon name="x" :size="12" />
      </button>
    </span>
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
  busy?: boolean
}>()

const emit = defineEmits<{
  pause: []
  resume: []
  clear: []
  dismiss: []
}>()

const { t } = useI18n()

const titleText = computed(() => {
  switch (props.goal.status) {
    case 'running': return t('chat.goal.activeTitle')
    case 'paused': return t('chat.goal.pausedTitle')
    case 'complete': return t('chat.goal.completeTitle')
    case 'blocked': return t('chat.goal.blockedTitle')
    case 'cancelled': return t('chat.goal.cancelledTitle')
    default: return t('chat.goal.activeTitle')
  }
})

const metaText = computed(() => {
  const parts: string[] = []
  if (props.elapsed) parts.push(props.elapsed)
  if (props.goal.turns > 0) parts.push(t('chat.goal.turns', { turns: props.goal.turns }))
  if (props.goal.status === 'blocked' && props.goal.blockedReason) {
    parts.push(props.goal.blockedReason)
  }
  if (props.goal.status === 'paused' && props.goal.pauseReason === 'user_paused') {
    parts.push(t('chat.goal.pausedByUser'))
  }
  return parts.join(' · ')
})
</script>

<style scoped>
.goal-ribbon {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 100%;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 26%, transparent);
  border-radius: var(--radius-card);
  background: color-mix(in srgb, var(--bg-elevated, var(--card)) 92%, transparent);
  box-shadow: var(--shadow-md);
  font-size: 0.8125rem;
  line-height: 1.4;
}
.goal-ribbon[data-status='paused'] {
  border-color: color-mix(in srgb, var(--warn) 45%, transparent);
}
.goal-ribbon[data-status='blocked'],
.goal-ribbon[data-status='cancelled'] {
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
}
.goal-ribbon[data-status='complete'] {
  border-color: color-mix(in srgb, var(--ok) 40%, transparent);
}
.goal-ribbon__icon {
  flex: 0 0 auto;
  display: inline-flex;
  color: var(--accent);
}
.goal-ribbon__title {
  flex: 0 0 auto;
  font-weight: 600;
  white-space: nowrap;
}
.goal-ribbon__text {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted, var(--muted));
}
.goal-ribbon__meta {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted, var(--muted));
  white-space: nowrap;
}
.goal-ribbon__actions {
  flex: 0 0 auto;
  display: inline-flex;
  gap: 2px;
}
.goal-ribbon__action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted, var(--muted));
  cursor: pointer;
}
.goal-ribbon__action:hover:not(:disabled),
.goal-ribbon__action:focus-visible {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--text);
}
.goal-ribbon__action:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
