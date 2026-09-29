<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import SetupConnectionPanel from '@/components/settings/SetupConnectionPanel.vue'
import DesktopRuntimePanel from '@/components/settings/DesktopRuntimePanel.vue'
import DesktopLogLocationButton from '@/components/settings/DesktopLogLocationButton.vue'
import SettingsUpdatePanel from '@/components/settings/SettingsUpdatePanel.vue'
import SupportBundleButton from '@/components/SupportBundleButton.vue'
import GatewayLogViewer from '@/components/GatewayLogViewer.vue'

defineProps<{ isDesktop: boolean }>()

const { t } = useI18n()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.gateway.title') }}</h3>
      <p class="control-section__desc">{{ t(isDesktop ? 'settings.gateway.desktopDesc' : 'settings.gateway.desc') }}</p>
    </div>

    <section
      id="settings-gateway-support"
      class="gateway-support"
      aria-labelledby="settings-gateway-support-title"
      tabindex="-1"
    >
      <h4 id="settings-gateway-support-title" class="gateway-support__title">{{ t('monitorSupport.title') }}</h4>
      <div class="gateway-support__actions">
        <GatewayLogViewer />
        <DesktopLogLocationButton v-if="isDesktop" />
        <SupportBundleButton />
      </div>
    </section>

    <div id="settings-gateway-connection" class="settings-composite" tabindex="-1">
      <SetupConnectionPanel :managed="isDesktop" />
    </div>
    <div
      v-if="isDesktop"
      id="settings-gateway-runtime"
      class="settings-composite"
      tabindex="-1"
    >
      <DesktopRuntimePanel />
    </div>
    <section
      v-if="isDesktop"
      id="settings-gateway-updates"
      class="settings-composite"
      tabindex="-1"
      aria-labelledby="settings-gateway-updates-title"
    >
      <SettingsUpdatePanel />
    </section>
  </section>
</template>

<style scoped>
.gateway-support {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-3);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--sp-4);
  padding-bottom: var(--sp-3);
}

.gateway-support__title {
  font-size: var(--fs-sm);
  font-weight: 600;
  margin: 0;
}

.gateway-support__actions {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-left: auto;
  min-width: 0;
}

@media (max-width: 560px) {
  .gateway-support__actions { margin-left: 0; }
}

.gateway-support:focus {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.settings-composite + .settings-composite {
  border-top: 1px solid var(--border);
  margin-top: var(--sp-5);
  padding-top: var(--sp-5);
}

.settings-composite:focus { outline: none; }
</style>
