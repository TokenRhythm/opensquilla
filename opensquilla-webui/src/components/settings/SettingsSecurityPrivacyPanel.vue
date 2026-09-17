<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import DesktopPreviewModeSetting from '@/components/settings/DesktopPreviewModeSetting.vue'
import SandboxSettingsPanel from '@/components/settings/SandboxSettingsPanel.vue'
import SettingsPrivacyPanel from '@/components/settings/SettingsPrivacyPanel.vue'

interface PrivacyPanelContract {
  networkReportingEnabled: boolean
  networkReportingForcedOff: boolean
}

defineProps<{
  panel: PrivacyPanelContract
  loaded: boolean
  isDesktop: boolean
}>()

const emit = defineEmits<{
  updateNetworkReportingEnabled: [enabled: boolean]
}>()

const { t } = useI18n()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.securityPrivacy.title') }}</h3>
      <p class="control-section__desc">{{ t('settings.securityPrivacy.desc') }}</p>
    </div>

    <SettingsPrivacyPanel
      v-if="loaded"
      :panel="panel"
      @update-network-reporting-enabled="emit('updateNetworkReportingEnabled', $event)"
    />
    <div v-else class="security-loading" role="status">
      <LoadingSpinner />
      <span>{{ t('shared.loading') }}</span>
    </div>

    <div v-if="isDesktop" class="security-subsection">
      <DesktopPreviewModeSetting />
    </div>

    <div id="settings-security-sandbox" class="security-subsection" tabindex="-1">
      <SandboxSettingsPanel />
    </div>
  </section>
</template>

<style scoped>
.security-loading {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  padding: var(--sp-4) 0;
}

.security-subsection {
  border-top: 1px solid var(--border);
  margin-top: var(--sp-5);
  padding-top: var(--sp-5);
}

.security-subsection:focus { outline: none; }
</style>
