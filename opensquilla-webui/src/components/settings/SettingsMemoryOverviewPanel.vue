<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import MemoryLearningGroup from '@/components/settings/MemoryLearningGroup.vue'

defineProps<{
  autoCapture: boolean
  loaded: boolean
}>()

const emit = defineEmits<{
  updateAutoCapture: [enabled: boolean]
  openProfileImport: []
}>()

const { t } = useI18n()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.memoryOverview.title') }}</h3>
      <p class="control-section__desc">{{ t('settings.memoryOverview.desc') }}</p>
    </div>

    <label v-if="loaded" class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.memoryOverview.autoCaptureLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.memoryOverview.autoCaptureDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="autoCapture"
          name="memory_auto_capture"
          :aria-label="t('settings.memoryOverview.autoCaptureLabel')"
          @change="emit('updateAutoCapture', $event)"
        />
      </div>
    </label>
    <div v-else class="memory-loading" role="status">
      <LoadingSpinner />
      <span>{{ t('shared.loading') }}</span>
    </div>

    <details class="memory-learning">
      <summary>{{ t('settings.memoryOverview.learningSummary') }}</summary>
      <p>{{ t('settings.memoryOverview.learningDesc') }}</p>
      <MemoryLearningGroup />
    </details>

    <div class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.memoryOverview.profileImportLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.memoryOverview.profileImportDesc') }}</span>
      </div>
      <div class="control-row__control">
        <button type="button" class="btn btn--ghost" @click="emit('openProfileImport')">
          {{ t('settings.memoryOverview.profileImportAction') }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.memory-loading {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  padding: var(--sp-4) 0;
}

.memory-learning {
  border-bottom: 1px solid var(--border);
  padding: var(--sp-4) 0;
}

.memory-learning > summary {
  color: var(--text);
  cursor: pointer;
  font-size: var(--fs-sm);
  font-weight: 600;
}

.memory-learning > p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.55;
  margin: var(--sp-2) 0;
}
</style>
