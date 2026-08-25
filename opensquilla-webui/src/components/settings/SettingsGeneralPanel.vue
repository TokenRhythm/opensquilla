<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import SetupBehaviorPanel from '@/components/setup/SetupBehaviorPanel.vue'
import DesktopCloseBehaviorSetting from '@/components/settings/DesktopCloseBehaviorSetting.vue'
import SettingsLanguageControl from '@/components/settings/SettingsLanguageControl.vue'

interface BehaviorPanelContract {
  autoSessionTitles: boolean
  autoSessionTitlesDirty: boolean
  statusText: string
}

defineProps<{
  panel: BehaviorPanelContract
  loaded: boolean
  isDesktop: boolean
}>()

const emit = defineEmits<{
  updateAutoSessionTitles: [enabled: boolean]
}>()

const { t } = useI18n()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.general.title') }}</h3>
      <p class="control-section__desc">{{ t('settings.general.desc') }}</p>
    </div>

    <SettingsLanguageControl />
    <SetupBehaviorPanel
      v-if="loaded"
      embedded
      :panel="panel"
      @update-auto-session-titles="emit('updateAutoSessionTitles', $event)"
    />
    <div v-else class="general-loading" role="status">
      <LoadingSpinner />
      <span>{{ t('shared.loading') }}</span>
    </div>
    <DesktopCloseBehaviorSetting v-if="isDesktop" />
  </section>
</template>

<style scoped>
.general-loading {
  align-items: center;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  padding: var(--sp-4) 0;
}
</style>
