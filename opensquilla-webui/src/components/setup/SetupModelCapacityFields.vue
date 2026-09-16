<script setup lang="ts">
import { computed, useId } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ModelCapacity } from '@/modules/providerConfiguration'
import { parseCapacity, type CapacityField, type CapacityValues } from '@/composables/setup/useModelCapacityForm'

const { t } = useI18n()
const id = useId()
const props = defineProps<{ row: ModelCapacity; modelValue: CapacityValues; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: CapacityValues] }>()
const fields: CapacityField[] = ['contextWindow', 'maxOutputTokens']
const canReset = computed(() => fields.some(field => props.row[field].editable && props.modelValue[field] !== ''))
function invalid(field: CapacityField) {
  try { parseCapacity(props.modelValue[field]); return false } catch { return true }
}
function changed(field: CapacityField) {
  const override = props.row[field].override
  return props.row[field].editable && props.modelValue[field] !== (override == null ? '' : String(override))
}
function source(field: CapacityField) {
  const limit = props.row[field]
  if (!limit.editable) return t(`setup.capacity.source.${limit.source}`)
  if (props.modelValue[field].trim()) return t('setup.capacity.source.override')
  return t('setup.capacity.automaticSource', { source: t(`setup.capacity.source.${limit.automaticSource}`) })
}
function constrained(field: CapacityField) {
  const limit = props.row[field]
  return limit.editable && !changed(field) && limit.override != null && limit.override !== limit.value
}
function set(field: CapacityField, value: string) {
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}
function reset() {
  emit('update:modelValue', {
    contextWindow: props.row.contextWindow.editable ? '' : props.modelValue.contextWindow,
    maxOutputTokens: props.row.maxOutputTokens.editable ? '' : props.modelValue.maxOutputTokens,
  })
}
</script>

<template>
  <div class="model-capacity-fields">
    <p class="model-capacity-fields__identity">{{ row.provider }} · {{ row.model }}</p>
    <p class="control-section__desc">{{ t('setup.capacity.scope') }}</p>
    <label v-for="field in fields" :key="field" class="model-capacity-fields__row">
      <span class="control-row__label-block">
        <span class="control-row__label">{{ t(`setup.capacity.${field}`) }}</span>
        <span :id="`${id}-${field}-help`" class="control-row__desc">
          {{ source(field) }}<span v-if="changed(field)" class="model-capacity-fields__unsaved"> · {{ t('setup.capacity.unsaved') }}</span>
        </span>
        <span v-if="constrained(field)" class="control-row__desc">{{ t('setup.capacity.effective', { value: row[field].value.toLocaleString() }) }}</span>
        <span v-if="!row[field].editable" class="control-row__desc">{{ t('setup.capacity.serverControlled') }}</span>
      </span>
      <span class="model-capacity-fields__input">
        <span class="model-capacity-fields__control">
          <input
            class="control-input" type="text" inputmode="numeric" autocomplete="off"
            :aria-label="t(`setup.capacity.${field}`)"
            :aria-describedby="`${id}-${field}-help ${id}-${field}-unit${invalid(field) ? ` ${id}-${field}-error` : ''}`"
            :aria-invalid="invalid(field) || undefined" :disabled="disabled || !row[field].editable"
            :placeholder="row[field].automatic.toLocaleString()"
            :value="row[field].editable ? modelValue[field] : row[field].value.toLocaleString()"
            @input="set(field, ($event.target as HTMLInputElement).value)"
          />
          <span :id="`${id}-${field}-unit`" class="model-capacity-fields__unit">Token</span>
        </span>
        <span v-if="invalid(field)" :id="`${id}-${field}-error`" class="model-capacity-fields__error" role="alert">{{ t('setup.capacity.positiveInteger') }}</span>
      </span>
    </label>
    <p class="control-section__desc">{{ t('setup.capacity.autoHint') }}</p>
    <p class="control-section__desc">{{ t('setup.capacity.requestHint') }}</p>
    <p v-if="row.localRuntime" class="control-section__desc">{{ t('setup.capacity.localHint') }}</p>
    <button type="button" class="btn btn--ghost" :disabled="disabled || !canReset" @click="reset">{{ t('setup.capacity.restore') }}</button>
  </div>
</template>

<style scoped>
.model-capacity-fields { display: flex; flex-direction: column; gap: var(--sp-2); min-width: 0; container: model-capacity-fields / inline-size; }
.model-capacity-fields__identity { margin: 0; color: var(--text); font-size: var(--fs-sm); overflow-wrap: anywhere; }
.model-capacity-fields__row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.model-capacity-fields__row .control-row__label-block { min-width: 0; }
.model-capacity-fields__input { display: flex; flex-direction: column; gap: var(--sp-1); width: 190px; max-width: 100%; flex-shrink: 0; }
.model-capacity-fields__control { position: relative; display: block; }
.model-capacity-fields__input input { width: 100%; max-width: none; box-sizing: border-box; padding-inline-end: 54px; background: var(--bg-surface); border: 1px solid var(--border-strong); }
.model-capacity-fields__input input::placeholder { color: var(--text); opacity: 1; }
.model-capacity-fields__input input:not(:disabled):hover { border-color: var(--text-muted); }
.model-capacity-fields__input input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.model-capacity-fields__input input[aria-invalid="true"] { border-color: var(--danger); }
.model-capacity-fields__unit { position: absolute; inset-inline-end: var(--sp-2); top: 50%; transform: translateY(-50%); color: var(--text-muted); font-size: var(--fs-xs); pointer-events: none; }
.model-capacity-fields__unsaved { color: var(--accent); }
.model-capacity-fields__error { color: var(--danger); font-size: var(--fs-xs); }
.model-capacity-fields > .btn { align-self: flex-start; }
@container model-capacity-fields (max-width: 380px) {
  .model-capacity-fields__row { align-items: stretch; flex-direction: column; gap: var(--sp-2); }
  .model-capacity-fields__input { width: 100%; }
}
</style>
