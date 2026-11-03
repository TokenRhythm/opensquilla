<script setup lang="ts">
import { computed, useId } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { RecommendationKind } from '@/modules/providerConfiguration'

const props = defineProps<{
  provider: string
  kind: RecommendationKind
  hasUpdate?: boolean
  disabled?: boolean
  busy?: boolean
  disabledReason?: string
  helpText: string
  actionLabel?: string
  actionTestId: string
  updateTestId?: string
}>()
const emit = defineEmits<{ review: [] }>()
const { t } = useI18n()
const id = useId()
const actionId = `model-recommendation-action-${id}`
const helpId = `model-recommendation-help-${id}`
const label = computed(() => props.actionLabel || (
  props.busy ? t('setup.modelStrategy.recommendations.saving')
    : `${t(props.kind === 'router' ? 'setup.modelStrategy.resetRecommended' : 'setup.modelStrategy.recommendations.useEnsemble', { provider: props.provider })}…`
))

function review() {
  if (!props.disabled && !props.busy && !props.disabledReason) emit('review')
}
</script>

<template>
  <section class="model-recommendation" :aria-labelledby="actionId">
    <div class="model-recommendation__actions">
      <button
        type="button"
        class="btn btn--ghost model-recommendation__action"
        :data-testid="actionTestId"
        :disabled="disabled || busy || Boolean(disabledReason)"
        :aria-busy="busy ? 'true' : undefined"
        :aria-describedby="helpId"
        @click="review"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span :id="actionId">{{ label }}</span>
      </button>
      <span v-if="hasUpdate" class="model-recommendation__update" :data-testid="updateTestId" role="status">
        {{ t('setup.modelStrategy.recommendations.updateAvailable') }}
      </span>
    </div>
    <p :id="helpId">{{ disabledReason || helpText }}</p>
  </section>
</template>

<style scoped>
.model-recommendation { min-width: 0; }
.model-recommendation__actions { align-items: center; display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.model-recommendation__update {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: var(--radius-pill);
  color: var(--accent);
  font-size: var(--fs-xs);
  padding: var(--sp-1) var(--sp-2);
}
.model-recommendation p { color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.5; margin: var(--sp-2) 0 0; overflow-wrap: anywhere; }
.model-recommendation__action { height: auto; max-width: 100%; text-align: start; white-space: normal; overflow-wrap: anywhere; }
.model-recommendation__action :deep(svg) { flex-shrink: 0; }
</style>
