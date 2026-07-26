<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const props = defineProps<{
  tokenRhythmSelected: boolean
  credentialReplacementRequired: boolean
}>()

const registrationUrl = 'https://tokenrhythm.studio/register'
const finalStepKey = computed(() => {
  if (!props.tokenRhythmSelected) {
    return 'setup.provider.recommendation.stepSelectAndPaste'
  }
  if (props.credentialReplacementRequired) {
    return 'setup.provider.recommendation.stepReplaceAndPaste'
  }
  return 'setup.provider.recommendation.stepPaste'
})
const stepKeys = computed(() => [
  'setup.provider.recommendation.stepRegister',
  'setup.provider.recommendation.stepCopy',
  finalStepKey.value,
])
</script>

<template>
  <div
    class="setup-provider-recommendation control-card control-card--compact control-card--accent"
    data-testid="tokenrhythm-recommendation"
  >
    <div class="setup-provider-recommendation__head">
      <p
        class="setup-provider-recommendation__title"
        data-testid="tokenrhythm-recommendation-title"
      >{{ t('setup.provider.recommendation.title') }}</p>
      <span class="setup-provider-recommendation__scope">
        {{ t('setup.provider.recommendationScope') }}
      </span>
    </div>
    <p
      class="setup-provider-recommendation__copy"
      data-testid="tokenrhythm-recommendation-value"
    >{{ t('setup.provider.recommendation.value') }}</p>
    <p
      class="setup-provider-recommendation__copy"
      data-testid="tokenrhythm-recommendation-registration"
    >{{ t('setup.provider.recommendation.registration') }}</p>
    <ol
      class="setup-provider-recommendation__steps"
      :aria-label="t('setup.provider.recommendation.stepsLabel')"
    >
      <li
        v-for="(stepKey, index) in stepKeys"
        :key="stepKey"
        class="setup-provider-recommendation__step"
        data-testid="tokenrhythm-recommendation-step"
      >
        <span class="setup-provider-recommendation__step-number" aria-hidden="true">{{ index + 1 }}</span>{{ ' ' }}
        <span>{{ t(stepKey) }}</span>
      </li>
    </ol>
    <a
      class="setup-provider-recommendation__link btn btn--primary"
      :href="registrationUrl"
      target="_blank"
      rel="noopener noreferrer"
      :aria-label="t('setup.provider.recommendation.externalLabel')"
    >{{ t('setup.provider.recommendation.cta') }}</a>
  </div>
</template>

<style scoped>
.setup-provider-recommendation {
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-elevated));
  border-left: 3px solid color-mix(in srgb, var(--accent) 72%, var(--border));
  margin: var(--sp-4) 0;
}

.setup-provider-recommendation__title,
.setup-provider-recommendation__copy {
  margin: 0;
}

.setup-provider-recommendation__title {
  font-size: var(--fs-sm);
  font-weight: 700;
}

.setup-provider-recommendation__head {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: space-between;
}

.setup-provider-recommendation__scope {
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: var(--radius-full);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 650;
  padding: 2px 8px;
}

.setup-provider-recommendation__copy {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
}

.setup-provider-recommendation__steps {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--sp-2);
  list-style: none;
  margin: var(--sp-1) 0;
  padding: 0;
}

.setup-provider-recommendation__step {
  display: flex;
  min-width: 0;
  min-height: 40px;
  align-items: center;
  gap: var(--sp-2);
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 4%, var(--bg-surface));
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.35;
  padding: 7px 10px;
}

.setup-provider-recommendation__step-number {
  display: inline-flex;
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--accent);
  color: var(--accent-foreground);
  font-size: var(--fs-xs);
  font-weight: 700;
  line-height: 1;
}

.setup-provider-recommendation__link {
  align-self: flex-start;
  max-width: 100%;
  color: var(--accent-foreground);
  overflow-wrap: anywhere;
  text-align: center;
  text-decoration: none;
  white-space: normal;
}

.setup-provider-recommendation__link:hover {
  text-decoration: none;
}

.setup-provider-recommendation__link:focus-visible {
  border-radius: var(--radius-sm);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (max-width: 720px) {
  .setup-provider-recommendation__steps {
    grid-template-columns: 1fr;
  }
}
</style>
