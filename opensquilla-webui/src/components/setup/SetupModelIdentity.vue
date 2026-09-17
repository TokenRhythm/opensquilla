<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ provider: string; providerLabel: string; model: string; note?: string }>()
const { t } = useI18n()
const label = computed(() => {
  if (props.provider === 'custom') return t('setup.capacity.customOpenAI')
  if (props.provider === 'custom_anthropic') return t('setup.capacity.customAnthropic')
  return props.providerLabel || props.provider
})
</script>

<template>
  <span class="setup-model-identity">
    <span class="setup-model-identity__model" :title="model">{{ model }}</span>
    <span class="setup-model-identity__provider" :title="providerLabel">
      {{ label }}<span v-if="note"> · {{ note }}</span>
    </span>
  </span>
</template>

<style scoped>
.setup-model-identity { display: grid; flex: 1 1 0; gap: 2px; min-width: 0; }
.setup-model-identity__model { color: var(--text); font-size: var(--fs-sm); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.setup-model-identity__provider { color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.4; overflow-wrap: anywhere; }
</style>
