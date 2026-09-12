<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface PrivacyPanelContract {
  networkReportingEnabled: boolean
  networkReportingForcedOff: boolean
  reliabilityDiagnosticsEnabled: boolean
  reliabilityDiagnosticsDecision: boolean | null
  reliabilityDiagnosticsForcedOff: boolean
  productAnalyticsEnabled: boolean
  productAnalyticsDecision: boolean | null
  productAnalyticsForcedOff: boolean
}

defineProps<{
  panel: PrivacyPanelContract
}>()

const emit = defineEmits<{
  updateNetworkReportingEnabled: [enabled: boolean]
  updateReliabilityDiagnosticsEnabled: [enabled: boolean]
  updateProductAnalyticsEnabled: [enabled: boolean]
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

    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.privacy.reliabilityDiagnosticsLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.privacy.reliabilityDiagnosticsDesc') }}</span>
        <span v-if="panel.reliabilityDiagnosticsForcedOff" class="control-row__desc">
          {{ t('setup.privacy.statusDisabledByEnv') }}
        </span>
        <span v-else-if="panel.reliabilityDiagnosticsDecision === null" class="control-row__desc">
          {{ t('setup.privacy.statusNotChosen') }}
        </span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.reliabilityDiagnosticsEnabled"
          :disabled="(
            panel.reliabilityDiagnosticsForcedOff
            && panel.reliabilityDiagnosticsDecision !== true
          )"
          name="setup_reliability_diagnostics"
          :aria-label="t('setup.privacy.reliabilityDiagnosticsLabel')"
          @change="(value) => emit('updateReliabilityDiagnosticsEnabled', value)"
        />
      </div>
    </label>

    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.privacy.productAnalyticsLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.privacy.productAnalyticsDesc') }}</span>
        <span v-if="panel.productAnalyticsForcedOff" class="control-row__desc">
          {{ t('setup.privacy.statusDisabledByEnv') }}
        </span>
        <span v-else-if="panel.productAnalyticsDecision === null" class="control-row__desc">
          {{ t('setup.privacy.statusNotChosen') }}
        </span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.productAnalyticsEnabled"
          :disabled="(
            panel.productAnalyticsForcedOff
            && panel.productAnalyticsDecision !== true
          )"
          name="setup_product_analytics"
          :aria-label="t('setup.privacy.productAnalyticsLabel')"
          @change="(value) => emit('updateProductAnalyticsEnabled', value)"
        />
      </div>
    </label>
  </div>
</template>

<style scoped>
.settings-subsection:focus { outline: none; }
</style>
