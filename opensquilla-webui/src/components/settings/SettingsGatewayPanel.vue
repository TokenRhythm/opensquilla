<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import SetupConnectionPanel from '@/components/settings/SetupConnectionPanel.vue'
import DesktopRuntimePanel from '@/components/settings/DesktopRuntimePanel.vue'

defineProps<{ isDesktop: boolean }>()

const { t } = useI18n()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.gateway.title') }}</h3>
      <p class="control-section__desc">{{ t('settings.gateway.desc') }}</p>
    </div>

    <div id="settings-gateway-connection" class="settings-composite" tabindex="-1">
      <SetupConnectionPanel />
    </div>
    <div
      v-if="isDesktop"
      id="settings-gateway-runtime"
      class="settings-composite"
      tabindex="-1"
    >
      <DesktopRuntimePanel />
    </div>
  </section>
</template>

<style scoped>
.settings-composite + .settings-composite {
  border-top: 1px solid var(--border);
  margin-top: var(--sp-5);
  padding-top: var(--sp-5);
}

.settings-composite:focus { outline: none; }
</style>
