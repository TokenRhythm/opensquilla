<script setup lang="ts">
import { useI18n } from 'vue-i18n'
defineProps<{ modelValue: string; disabled?: boolean; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const { t } = useI18n()
const levels = ['', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh']
</script>

<template>
  <select
    class="control-input control-input--narrow"
    :value="modelValue === 'none' ? 'off' : modelValue"
    :aria-label="label || t('setup.provider.thinkingLabel')"
    :disabled="disabled"
    @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
  >
    <option v-for="level in levels" :key="level" :value="level">{{ level || t('setup.provider.thinkingDefault') }}</option>
  </select>
</template>
