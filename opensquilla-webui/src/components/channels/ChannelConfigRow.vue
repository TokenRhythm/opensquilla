<script setup lang="ts">
// One configuration row on the shared 148px label rail. Read mode renders the
// value as text inside the same box metrics the edit-mode control uses, so
// flipping read ⇄ edit swaps the control IN PLACE — same rail, same x/y
// geometry, zero reflow. That geometric identity is the point of this file.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'
import type { ChannelEditorFieldSpec } from '@/composables/channels/useChannelEditor'

export interface ConfigRowModel {
  kind: 'name' | 'field' | 'secret'
  field: ChannelEditorFieldSpec
  /** Display label (catalog-i18n overlay applied; falls back to the spec label). */
  label: string
  /** Display helper line (catalog-i18n overlay applied; may be empty). */
  description: string
  value: string
  edited: boolean
  hasStored?: boolean
  replacing?: boolean
  /** Select fields: localized display label per raw choice value. */
  choiceLabels?: Record<string, string>
}

const props = defineProps<{
  row: ConfigRowModel
  edit: boolean
  error?: string
}>()

const emit = defineEmits<{
  update: [name: string, value: unknown]
  replace: [name: string]
  cancelReplace: [name: string]
}>()

const { t } = useI18n()

const fieldType = computed(() => String(props.row.field.type || 'text'))
const inputId = computed(() => `cfge-${props.row.field.name.replace(/[^a-zA-Z0-9_-]+/g, '-')}`)
// Compose mode routes secret fields through here as plain rows (nothing is
// stored yet) — they must still render as password inputs, never text.
// These are API credentials, not login passwords: autocomplete="off" plus the
// password-manager opt-out attributes (1Password/LastPass/Bitwarden) keep
// autofill prompts from claiming bot tokens and app secrets.
const isSecretInput = computed(
  () => props.row.field.secret === true || fieldType.value === 'password',
)
const inputType = computed(() => {
  if (isSecretInput.value) return 'password'
  if (fieldType.value === 'int' || fieldType.value === 'float') return 'number'
  return 'text'
})
const placeholder = computed(() => {
  if (props.row.field.placeholder) return props.row.field.placeholder
  return isSecretInput.value ? t('setup.field.secretComposePlaceholder') : ''
})
const displayValue = computed(() => {
  if (props.row.value === '') return '—'
  return props.row.choiceLabels?.[props.row.value] ?? props.row.value
})
const isEmptyValue = computed(() => props.row.value === '')

// A two-option select renders as a segmented binary — the choice reads at a
// glance (both options visible) instead of hiding behind a dropdown. Longer
// choice lists keep the native select.
const segmentedChoices = computed(() => {
  const choices = props.row.field.choices || []
  return fieldType.value === 'select' && choices.length === 2 ? choices : null
})

function choiceLabel(choice: string): string {
  return props.row.choiceLabels?.[choice] ?? choice
}

function onInput(event: Event) {
  emit('update', props.row.field.name, (event.target as HTMLInputElement | HTMLSelectElement).value)
}
</script>

<template>
  <div class="cfge__row" :data-field="row.field.name">
    <div class="cfge__rail">
      <!-- A segmented group has no single labelable control; its buttons carry
           the row label via the group's aria-label instead, so `for` must not
           dangle (a label click would otherwise activate the first option). -->
      <label class="cfge__label" :for="edit && segmentedChoices ? undefined : inputId">
        {{ row.label }}<span v-if="edit && row.field.required" aria-hidden="true"> *</span>
      </label>
      <span
        v-if="edit && row.edited"
        class="cfge__tick"
        :title="t('console.channels.editor.edited')"
        :aria-label="t('console.channels.editor.edited')"
      >●</span>
    </div>
    <div class="cfge__control">
      <!-- Name: identity key. In edit mode it is locked text with a lock
           glyph — deliberately not a disabled input. -->
      <template v-if="row.kind === 'name'">
        <span v-if="!edit" class="cfge__value">{{ displayValue }}</span>
        <span v-else class="cfge__value cfge__value--locked" :title="t('setup.channels.nameLocked')">
          <span>{{ row.value }}</span>
          <Icon name="lock" :size="13" aria-hidden="true" />
          <span class="cfge__sr-only">{{ t('setup.channels.nameLocked') }}</span>
        </span>
      </template>

      <!-- Secret: a stored credential reads materially different from an
           empty input; Replace swaps to a fresh password box in place. -->
      <template v-else-if="row.kind === 'secret'">
        <template v-if="row.hasStored && (!edit || !row.replacing)">
          <span class="cfge__secretline">
            <span class="cfge__value cfge__value--secret">{{ t('console.channels.editor.storedSecret') }}</span>
            <button
              v-if="edit"
              type="button"
              class="btn btn--ghost cfge__secretbtn"
              @click="emit('replace', row.field.name)"
            >{{ t('setup.channels.secretReplace') }}</button>
          </span>
        </template>
        <template v-else-if="!edit">
          <span class="cfge__value">—</span>
        </template>
        <template v-else>
          <span class="cfge__secretline">
            <input
              :id="inputId"
              class="cfge__input"
              type="password"
              autocomplete="off"
              data-secret="true"
              data-1p-ignore
              data-lpignore="true"
              data-bwignore
              :value="row.value"
              :placeholder="t('setup.channels.secretReplacePlaceholder')"
              :name="`setup_channel_${row.field.name}`"
              @input="onInput"
            />
            <button
              v-if="row.hasStored"
              type="button"
              class="btn btn--ghost cfge__secretbtn"
              @click="emit('cancelReplace', row.field.name)"
            >{{ t('setup.channels.secretKeepStored') }}</button>
          </span>
        </template>
      </template>

      <template v-else-if="fieldType === 'bool'">
        <span v-if="!edit" class="cfge__value cfge__value--bool" :class="row.value === 'true' ? 'is-on' : 'is-off'">
          <span class="cfge__booldot" aria-hidden="true"></span>
          {{ row.value === 'true' ? t('console.channels.editor.boolOn') : t('console.channels.editor.boolOff') }}
        </span>
        <span v-else class="cfge__switchline">
          <ControlSwitch
            :id="inputId"
            :checked="row.value === 'true'"
            :name="`setup_channel_${row.field.name}`"
            :aria-label="row.label"
            @change="value => emit('update', row.field.name, value)"
          />
        </span>
      </template>

      <template v-else-if="fieldType === 'select'">
        <span v-if="!edit" class="cfge__value">{{ displayValue }}</span>
        <span
          v-else-if="segmentedChoices"
          class="cfge__seg"
          role="group"
          :aria-label="row.label"
        >
          <button
            v-for="choice in segmentedChoices"
            :key="choice"
            type="button"
            class="cfge__seg-opt"
            :class="{ 'is-on': row.value === choice }"
            :aria-pressed="row.value === choice"
            @click="emit('update', row.field.name, choice)"
          >{{ choiceLabel(choice) }}</button>
        </span>
        <select
          v-else
          :id="inputId"
          class="cfge__input cfge__input--select"
          :name="`setup_channel_${row.field.name}`"
          :value="row.value"
          @change="onInput"
        >
          <option v-for="choice in row.field.choices || []" :key="choice" :value="choice">{{ choiceLabel(choice) }}</option>
        </select>
      </template>

      <template v-else>
        <span v-if="!edit" class="cfge__value" :class="{ 'cfge__value--empty': isEmptyValue }">{{ displayValue }}</span>
        <input
          v-else
          :id="inputId"
          class="cfge__input"
          :type="inputType"
          :value="row.value"
          :placeholder="placeholder"
          :name="`setup_channel_${row.field.name}`"
          :autocomplete="isSecretInput ? 'off' : undefined"
          :data-secret="isSecretInput ? 'true' : undefined"
          :data-1p-ignore="isSecretInput ? '' : undefined"
          :data-lpignore="isSecretInput ? 'true' : undefined"
          :data-bwignore="isSecretInput ? '' : undefined"
          @input="onInput"
        />
      </template>

      <span v-if="edit && row.description" class="cfge__desc">{{ row.description }}</span>
      <p v-if="edit && error" class="cfge__field-error" role="alert">{{ error }}</p>
    </div>
  </div>
</template>
