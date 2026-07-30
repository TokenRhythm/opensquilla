<template>
  <div
    class="control-row"
    :class="{ 'control-row--stack': stack }"
    :data-name="field.name"
    :data-scope="scope"
    :data-show-when="showWhenAttr"
  >
    <div class="control-row__label-block">
      <label class="control-row__label" :for="fieldId">{{ field.label }}{{ field.required ? ' *' : '' }}</label>
      <span v-if="field.description" class="control-row__desc">{{ field.description }}</span>
    </div>
    <div class="control-row__control">
      <template v-if="field.type === 'bool'">
        <ControlSwitch
          :id="fieldId"
          :name="fieldName"
          :checked="fieldValue === true || fieldValue === 'true'"
          @change="onBoolChange"
        />
      </template>
      <select
        v-else-if="field.type === 'select'"
        :id="fieldId"
        class="control-input"
        :name="fieldName"
        :value="fieldValue"
        @change="onInputChange"
      >
        <option v-for="choice in field.choices" :key="choice" :value="choice">{{ choice }}</option>
      </select>
      <input
        v-else
        :id="fieldId"
        class="control-input"
        :name="fieldName"
        :type="inputType"
        :value="fieldValue"
        :placeholder="placeholder"
        :data-secret="isSecret"
        @input="onInputChange"
      >
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

interface FieldSpec {
  name: string
  label: string
  type?: string
  required?: boolean
  default?: string | boolean | number
  placeholder?: string
  description?: string
  secret?: boolean
  choices?: string[]
  showWhen?: Record<string, string>
}

const props = withDefaults(defineProps<{
  field: FieldSpec
  value: string | boolean | number
  scope: string
  /** Render label-on-top with a full-width control (long free-text: URLs, paths). */
  stack?: boolean
}>(), { stack: false })

const emit = defineEmits<{
  (e: 'update', name: string, value: unknown): void
}>()

const rawName = computed(() => String(props.field.name || 'field'))
const fieldName = computed(() => `setup_${props.scope}_${rawName.value}`)
const fieldId = computed(() => `setup-${props.scope}-${rawName.value.replace(/[^a-zA-Z0-9_-]+/g, '-')}`)

const isSecret = computed(() => props.field.secret || props.field.type === 'password')
const inputType = computed(() => {
  if (isSecret.value) return 'password'
  if (props.field.type === 'int' || props.field.type === 'float') return 'number'
  return 'text'
})
const { t } = useI18n()
// Secrets keep the blank-keeps-current contract, translated.
const placeholder = computed(() => {
  if (props.field.placeholder) return props.field.placeholder
  if (!isSecret.value) return ''
  return t('setup.field.keepCurrentPlaceholder')
})
const showWhenAttr = computed(() => {
  if (!props.field.showWhen || Object.keys(props.field.showWhen).length === 0) return ''
  return JSON.stringify(props.field.showWhen)
})

const fieldValue = computed(() => {
  if (props.value !== undefined && props.value !== null) return props.value
  if (props.field.type === 'bool') return props.field.default === true
  return String(props.field.default || '')
})

function onInputChange(event: Event) {
  const target = event.target as HTMLInputElement | HTMLSelectElement
  emit('update', props.field.name, target.value)
}

function onBoolChange(checked: boolean) {
  emit('update', props.field.name, checked)
}
</script>
