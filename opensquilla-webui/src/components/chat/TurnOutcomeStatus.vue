<template>
  <div
    class="turn-outcome"
    :class="[
      `turn-outcome--${presentation}`,
      { 'turn-outcome--process-restart': processRestart },
    ]"
    role="status"
    :data-testid="`turn-outcome-${presentation}`"
  >
    <span class="turn-outcome__dot" aria-hidden="true" />
    <span v-if="processRestart" class="turn-outcome__content">
      <span class="turn-outcome__title">
        {{ t('chat.restartInterruptedTitle') }}
        <span v-if="durationLabel" class="turn-outcome__duration">· {{ durationLabel }}</span>
      </span>
      <span class="turn-outcome__guidance">{{ t('chat.restartInterruptedGuidance') }}</span>
    </span>
    <template v-else>
      <span>{{ label }}</span>
      <span v-if="durationLabel" class="turn-outcome__duration">· {{ durationLabel }}</span>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatTurnOutcome } from '@/types/chat'
import {
  isProcessRestartOutcome,
  turnOutcomeDurationSeconds,
  turnOutcomePresentation,
} from '@/utils/chat/turnOutcome'

const props = defineProps<{ outcome: ChatTurnOutcome }>()
const { t } = useI18n()
const presentation = computed(() => turnOutcomePresentation(props.outcome))
const processRestart = computed(() => isProcessRestartOutcome(props.outcome))
const label = computed(() => t({
  completed: 'chat.activity.lifecycle.settled',
  stopped: 'sessions.status.cancelled',
  interrupted: 'sessions.status.interrupted',
  timeout: 'sessions.status.timeout',
  failed: 'sessions.status.failed',
}[presentation.value]))
const durationLabel = computed(() => {
  const seconds = turnOutcomeDurationSeconds(props.outcome)
  return seconds > 0 ? t('chat.activityDurationSeconds', { seconds }) : ''
})
</script>

<style scoped>
.turn-outcome {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  gap: 0.4rem;
  min-height: 1.5rem;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.4;
}

.turn-outcome__dot {
  width: 0.4rem;
  height: 0.4rem;
  flex: 0 0 auto;
  border-radius: var(--radius-full);
  background: currentColor;
  opacity: 0.65;
}

.turn-outcome--process-restart {
  align-items: flex-start;
  width: min(100%, 42rem);
}

.turn-outcome--process-restart .turn-outcome__dot {
  margin-top: 0.5rem;
}

.turn-outcome__content {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 0.15rem;
}

.turn-outcome__title {
  color: var(--text-muted);
  font-weight: 600;
}

.turn-outcome__guidance {
  color: var(--text-dim);
}

.turn-outcome--failed,
.turn-outcome--timeout {
  color: var(--danger);
}

.turn-outcome__duration {
  color: var(--text-dim);
}
</style>
