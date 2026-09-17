<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface PrivacyPanelContract {
  networkReportingEnabled: boolean
  networkReportingForcedOff: boolean
}

defineProps<{
  panel: PrivacyPanelContract
}>()

const emit = defineEmits<{
  updateNetworkReportingEnabled: [enabled: boolean]
}>()
</script>

<template>
  <div class="settings-subsection" id="settings-security-privacy" tabindex="-1">
    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.privacy.networkReportingLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.privacy.networkReportingDesc') }}</span>
        <span v-if="panel.networkReportingForcedOff" class="control-row__desc">
          {{ t('setup.privacy.statusDisabledByEnv') }}
        </span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.networkReportingEnabled"
          :disabled="panel.networkReportingForcedOff"
          name="setup_disable_network_observability"
          :aria-label="t('setup.privacy.networkReportingLabel')"
          @change="(value) => emit('updateNetworkReportingEnabled', value)"
        />
      </div>
    </label>
  </div>
</template>

<style scoped>
.settings-subsection:focus { outline: none; }
</style>
