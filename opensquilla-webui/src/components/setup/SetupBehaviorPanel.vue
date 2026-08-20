<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface BehaviorPanelContract {
  autoSessionTitles: boolean
  autoSessionTitlesDirty: boolean
  statusText: string
}

defineProps<{
  panel: BehaviorPanelContract
  embedded?: boolean
}>()

const emit = defineEmits<{
  updateAutoSessionTitles: [enabled: boolean]
}>()
</script>

<template>
  <section class="control-section" :class="{ 'control-section--embedded': embedded }">
    <div v-if="!embedded" class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.behavior.title') }}</h3>
      <p class="control-section__desc">{{ panel.statusText }}</p>
    </div>
    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.behavior.autoTitlesLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.behavior.autoTitlesDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.autoSessionTitles"
          name="setup_auto_session_titles"
          :aria-label="t('setup.behavior.autoTitlesLabel')"
          @change="(value) => emit('updateAutoSessionTitles', value)"
        />
      </div>
    </label>
  </section>
</template>

<style scoped>
.control-section--embedded { display: contents; }
</style>
