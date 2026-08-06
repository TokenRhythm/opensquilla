<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface PrivacyPanelContract {
  disableNetworkObservability: boolean
  disableNetworkObservabilityDirty: boolean
  memoryAutoCapture: boolean
  memoryAutoCaptureDirty: boolean
  statusText: string
}

defineProps<{
  panel: PrivacyPanelContract
}>()

const emit = defineEmits<{
  updateDisableNetworkObservability: [enabled: boolean]
  updateMemoryAutoCapture: [enabled: boolean]
}>()
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.privacy.title') }}</h3>
      <p class="control-section__desc">{{ panel.statusText }}</p>
    </div>

    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.privacy.memoryAutoCaptureLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.privacy.memoryAutoCaptureDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.memoryAutoCapture"
          name="setup_memory_auto_capture"
          :aria-label="t('setup.privacy.memoryAutoCaptureLabel')"
          @change="(value) => emit('updateMemoryAutoCapture', value)"
        />
      </div>
    </label>

    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.privacy.disableNetworkObservabilityLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.privacy.disableNetworkObservabilityDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.disableNetworkObservability"
          name="setup_disable_network_observability"
          :aria-label="t('setup.privacy.disableNetworkObservabilityLabel')"
          @change="(value) => emit('updateDisableNetworkObservability', value)"
        />
      </div>
    </label>
  </section>
</template>
