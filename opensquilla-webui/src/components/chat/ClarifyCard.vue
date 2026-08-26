<template>
  <!-- Prominent outcome banner after the reply is sent -->
  <div
    v-if="submitted"
    class="clarify-outcome"
    :class="{
      'is-busy': busy,
      'clarify-outcome--plan': isPlanQuestionnaire,
    }"
    data-testid="clarify-outcome"
    role="status"
    aria-live="polite"
    aria-atomic="true"
  >
    <span v-if="!isPlanQuestionnaire" class="clarify-outcome__icon" aria-hidden="true">
      <Icon :name="busy ? 'clock' : 'check'" :size="18" />
    </span>
    <span class="clarify-outcome__copy">
      <span class="clarify-outcome__title">
        {{ busy ? t('chat.clarify.replyReceived') : t('chat.clarify.outcomeDoneTitle') }}
      </span>
      <span class="clarify-outcome__detail">
        {{ busy ? t('chat.clarify.outcomeBusyDetail') : t('chat.clarify.outcomeDoneDetail') }}
      </span>
    </span>
  </div>

  <!-- Pending clarify card -->
  <article
    v-else
    class="clarify-card"
    :class="{
      'clarify-card--plan': isPlanQuestionnaire,
      'clarify-card--docked': docked,
    }"
    data-testid="clarify-card"
    role="group"
    :aria-label="t('chat.clarify.needsInput')"
  >
    <!-- Concise live announcement: screen readers hear only this line, not the full card body -->
    <div
      class="clarify-card__announce"
      aria-live="polite"
      aria-atomic="true"
    >{{ t('chat.clarify.inputNeededFromAgent') }}</div>
    <header class="clarify-card__head">
      <span class="clarify-card__eyebrow">{{ t('chat.clarify.inputNeeded') }}</span>
      <p
        v-if="request.intro && !isPlanQuestionnaire"
        class="clarify-card__intro"
        :class="{ 'clarify-card__intro--long': hasLongIntro }"
        :tabindex="hasLongIntro ? 0 : undefined"
        data-testid="clarify-intro"
      >{{ request.intro }}</p>
      <p v-if="isPlanQuestionnaire" class="clarify-card__intro">
        {{ t('chat.clarify.planQuestionnaireHint') }}
      </p>
    </header>

    <div
      class="clarify-card__body"
      role="region"
      tabindex="0"
      :aria-label="t('chat.clarify.inputNeeded')"
    >
      <div v-for="field in displayedFields" :key="field.name" class="clarify-field">
        <div :id="fieldLabelId(field.name)" class="clarify-field__label">
          <span class="clarify-field__name">{{ field.header || field.name }}</span>
          <span v-if="field.prompt && field.prompt !== field.name" class="clarify-field__prompt">
            {{ field.prompt }}
          </span>
          <span class="clarify-field__req">{{ field.required ? t('chat.clarify.required') : t('chat.clarify.optional') }}</span>
        </div>

        <!-- Enum: numbered choices -->
        <div
          v-if="field.type === 'enum' && field.choices.length"
          class="clarify-field__choices"
          role="radiogroup"
          :aria-labelledby="fieldLabelId(field.name)"
        >
          <template v-if="isPlanQuestionnaire">
            <label
              v-for="(choice, idx) in field.choices"
              :key="choice"
              class="clarify-choice clarify-choice--radio"
              :class="{ 'is-selected': values[field.name] === choice && !otherSelected[field.name] }"
            >
              <input
                class="clarify-choice__radio"
                type="radio"
                :name="choiceName(field.name)"
                :value="choice"
                :checked="values[field.name] === choice && !otherSelected[field.name]"
                :disabled="busy"
                @change="selectChoice(field.name, choice)"
              />
              <span class="clarify-choice__num">{{ idx + 1 }}</span>
              <span class="clarify-choice__copy">
                <span class="clarify-choice__text">{{ choice }}</span>
                <span
                  v-if="optionDescription(field, choice)"
                  class="clarify-choice__description"
                >{{ optionDescription(field, choice) }}</span>
              </span>
            </label>
            <div
              v-if="field.allowOther"
              class="clarify-choice clarify-choice--other"
              :class="{ 'is-selected': otherSelected[field.name] }"
            >
              <input
                :id="otherRadioId(field.name)"
                class="clarify-choice__radio"
                type="radio"
                :name="choiceName(field.name)"
                :checked="otherSelected[field.name]"
                :aria-label="t('chat.clarify.other')"
                :disabled="busy"
                @change="selectOther(field.name)"
              />
              <span class="clarify-choice__num">{{ field.choices.length + 1 }}</span>
              <span class="clarify-choice__copy clarify-choice__copy--other">
                <label class="clarify-choice__text" :for="otherInputId(field.name)">
                  {{ t('chat.clarify.other') }}
                </label>
                <input
                  :id="otherInputId(field.name)"
                  class="clarify-choice__other-input"
                  type="text"
                  :value="otherValues[field.name]"
                  :placeholder="t('chat.clarify.otherPlaceholder')"
                  :disabled="busy"
                  @focus="selectOther(field.name)"
                  @input="updateOther(field.name, $event)"
                />
              </span>
            </div>
          </template>
          <template v-else>
            <button
              v-for="(choice, idx) in field.choices"
              :key="choice"
              type="button"
              class="clarify-choice"
              :class="{ 'is-selected': values[field.name] === choice && !otherSelected[field.name] }"
              role="radio"
              :aria-checked="values[field.name] === choice && !otherSelected[field.name]"
              :disabled="busy"
              @click="selectChoice(field.name, choice)"
            >
              <span class="clarify-choice__num">{{ idx + 1 }}</span>
              <span class="clarify-choice__copy">
                <span class="clarify-choice__text">{{ choice }}</span>
                <span
                  v-if="optionDescription(field, choice)"
                  class="clarify-choice__description"
                >{{ optionDescription(field, choice) }}</span>
              </span>
            </button>
            <label v-if="field.allowOther" class="clarify-field__other">
              <span>{{ t('chat.clarify.other') }}</span>
              <input
                :id="fieldId(field.name)"
                class="clarify-field__input"
                type="text"
                :value="otherValues[field.name]"
                :placeholder="t('chat.clarify.otherPlaceholder')"
                :disabled="busy"
                @focus="selectOther(field.name)"
                @input="updateOther(field.name, $event)"
              />
            </label>
          </template>
        </div>

        <!-- Bool: explicit true/false select -->
        <select
          v-else-if="field.type === 'bool'"
          :id="fieldId(field.name)"
          v-model="values[field.name]"
          class="clarify-field__input"
          :disabled="busy"
          :aria-labelledby="fieldLabelId(field.name)"
        >
          <option value="">—</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>

        <!-- Default: free text -->
        <input
          v-else
          :id="fieldId(field.name)"
          v-model="values[field.name]"
          class="clarify-field__input"
          type="text"
          :placeholder="field.defaultValue ? `default: ${field.defaultValue}` : ''"
          :disabled="busy"
          :aria-labelledby="fieldLabelId(field.name)"
        />
      </div>
    </div>

    <footer class="clarify-card__footer">
      <div v-if="isPlanQuestionnaire" class="clarify-card__plan-actions">
        <button
          type="button"
          class="clarify-card__nav-button"
          data-testid="clarify-previous"
          :disabled="busy || activeFieldIndex === 0"
          :aria-label="t('chat.clarify.previousQuestion')"
          @click="goPrevious"
        >
          <Icon name="chevronLeft" :size="16" aria-hidden="true" />
          <span>{{ t('chat.clarify.previousQuestion') }}</span>
        </button>
        <span
          class="clarify-card__progress"
          data-testid="clarify-question-progress"
          role="status"
          aria-live="polite"
        >{{ activeFieldIndex + 1 }} / {{ request.fields.length }}</span>
        <button
          v-if="!isLastPlanField"
          type="button"
          class="clarify-card__nav-button clarify-card__nav-button--next"
          data-testid="clarify-next"
          :disabled="busy || !canAdvanceActiveField"
          :aria-label="t('chat.clarify.nextQuestion')"
          @click="goNext"
        >
          <span>{{ t('chat.clarify.nextQuestion') }}</span>
          <Icon name="chevronRight" :size="16" aria-hidden="true" />
        </button>
        <button
          v-else
          class="clarify-card__nav-button clarify-card__nav-button--submit"
          data-testid="clarify-submit"
          type="button"
          :disabled="busy || !canSubmit"
          @click="onSubmit"
        >
          {{ busy ? t('chat.clarify.sendingReply') : t('chat.clarify.sendReply') }}
        </button>
      </div>
      <div v-else class="clarify-card__actions">
        <button
          class="btn btn--primary"
          type="button"
          :disabled="busy || !canSubmit"
          @click="onSubmit"
        >
          {{ busy ? t('chat.clarify.sendingReply') : t('chat.clarify.sendReply') }}
        </button>
        <button
          class="btn btn--ghost"
          type="button"
          :disabled="busy"
          @click="$emit('dismiss')"
        >
          {{ t('chat.clarify.dismiss') }}
        </button>
      </div>
      <p
        v-if="busy"
        class="clarify-card__status"
        data-testid="clarify-submit-status"
        role="status"
        aria-live="polite"
      >
        {{ t('chat.clarify.sendingContinuing') }}
      </p>
      <p v-if="error" class="clarify-card__error" role="alert">{{ error }}</p>
    </footer>
  </article>
</template>

<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatClarifyRequest } from '@/composables/chat/useChatApprovals'

const { t } = useI18n()

const props = withDefaults(defineProps<{
  request: ChatClarifyRequest
  submitted?: boolean
  busy?: boolean
  error?: string
  docked?: boolean
}>(), {
  submitted: false,
  busy: false,
  error: '',
  docked: false,
})

const emit = defineEmits<{
  submit: [fields: Record<string, string>]
  dismiss: []
}>()

const values = reactive<Record<string, string>>({})
const hasLongIntro = computed(() => props.request.intro.length > 2_000)
const otherValues = reactive<Record<string, string>>({})
const otherSelected = reactive<Record<string, boolean>>({})
const activeFieldIndex = ref(0)
const componentId = useId()
const isPlanQuestionnaire = computed(
  () => props.request.presentation === 'plan_questionnaire_v1',
)
const displayedFields = computed(() => (
  isPlanQuestionnaire.value
    ? props.request.fields.slice(activeFieldIndex.value, activeFieldIndex.value + 1)
    : props.request.fields
))
const activeField = computed(() => props.request.fields[activeFieldIndex.value])
const isLastPlanField = computed(
  () => activeFieldIndex.value >= props.request.fields.length - 1,
)
const canAdvanceActiveField = computed(() => {
  const field = activeField.value
  return !field || !field.required || Boolean((values[field.name] || '').trim())
})
const canSubmit = computed(() => (
  !isPlanQuestionnaire.value
  || props.request.fields.every(
    field => !field.required || Boolean((values[field.name] || '').trim()),
  )
))

// The parent rebuilds the request object while messages stream and when the
// conversation is re-rendered. Watching that object directly used to reset
// every field to its default even though the user was still answering the same
// clarify request. Reset only when the logical request itself changes.
watch(
  () => props.request.requestId || `${props.request.runId}\u0000${props.request.step}`,
  () => {
    const request = props.request
    for (const key of Object.keys(values)) delete values[key]
    for (const key of Object.keys(otherValues)) delete otherValues[key]
    for (const key of Object.keys(otherSelected)) delete otherSelected[key]
    activeFieldIndex.value = 0
    for (const field of request.fields) {
      const defaultValue = field.defaultValue || ''
      const isCustomDefault = Boolean(
        field.allowOther
        && defaultValue
        && !field.choices.includes(defaultValue),
      )
      values[field.name] = defaultValue
      otherValues[field.name] = isCustomDefault ? defaultValue : ''
      otherSelected[field.name] = isCustomDefault
    }
  },
  { immediate: true },
)

function fieldId(name: string): string {
  return `clarify-${componentId}-${name}`
}

function fieldLabelId(name: string): string {
  return `${fieldId(name)}-label`
}

function choiceName(name: string): string {
  return `${fieldId(name)}-choices`
}

function otherRadioId(name: string): string {
  return `${fieldId(name)}-other-radio`
}

function otherInputId(name: string): string {
  return `${fieldId(name)}-other-input`
}

function optionDescription(
  field: ChatClarifyRequest['fields'][number],
  choice: string,
): string {
  return field.options?.find(option => option.label === choice)?.description || ''
}

function selectChoice(name: string, choice: string) {
  otherSelected[name] = false
  values[name] = choice
}

function selectOther(name: string) {
  otherSelected[name] = true
  values[name] = otherValues[name] || ''
}

function updateOther(name: string, event: Event) {
  const input = event.target as HTMLInputElement
  otherValues[name] = input.value
  otherSelected[name] = true
  values[name] = input.value
}

function goPrevious() {
  if (props.busy || activeFieldIndex.value === 0) return
  activeFieldIndex.value -= 1
}

function goNext() {
  if (props.busy || !canAdvanceActiveField.value || isLastPlanField.value) return
  activeFieldIndex.value += 1
}

function onSubmit() {
  if (props.busy || !canSubmit.value) return
  const fields: Record<string, string> = {}
  for (const field of props.request.fields) {
    const value = (values[field.name] || '').trim()
    if (value) fields[field.name] = value
  }
  emit('submit', fields)
}
</script>

<style scoped>
/* Visually-hidden but announced by screen readers */
.clarify-card__announce {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.clarify-card {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: var(--sp-2) auto;
  background: var(--bg-surface);
  border: 1px solid color-mix(in srgb, var(--info) 35%, var(--border));
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* Direct child of the .chat-thread flex column: overflow:hidden drops the
     automatic min-height, so without this the card collapses when the thread
     scrolls. */
  flex-shrink: 0;
  animation: card-enter var(--dur-enter) var(--ease-out) both;
}

.clarify-card--plan {
  border-color: var(--border);
  border-radius: var(--radius-md);
  box-shadow: none;
  animation: none;
}

.clarify-card--docked {
  width: min(100%, var(--composer-col, 820px));
  max-height: min(64dvh, 520px);
  margin: 0 auto;
  box-shadow: var(--shadow-md);
}

.clarify-card--docked .clarify-card__head,
.clarify-card--docked .clarify-card__footer {
  flex-shrink: 0;
}

.clarify-card--docked .clarify-card__body {
  min-height: 0;
}

.clarify-card__head {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-3) var(--sp-4) 0;
}

.clarify-card__eyebrow {
  color: var(--info);
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.clarify-card__intro {
  color: var(--text);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: 0;
}

.clarify-card__intro--long {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  max-block-size: clamp(14rem, 42vh, 28rem);
  overflow-wrap: anywhere;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--sp-3);
  scrollbar-gutter: stable;
  white-space: pre-wrap;
}

.clarify-card__intro--long:focus-visible {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
  outline: none;
}

.clarify-card__body {
  max-height: 320px;
  overflow: auto;
  padding: var(--sp-3) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.clarify-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.clarify-field__label {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.clarify-field__name {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.clarify-field__prompt {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.clarify-field__req {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.clarify-field__input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--fs-sm);
  padding: var(--sp-2) var(--sp-3);
  width: 100%;
}

.clarify-field__input:focus-visible {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
  outline: none;
}

.clarify-field__choices {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.clarify-choice__copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.clarify-choice__description {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.35;
  text-align: left;
}

.clarify-field__other {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.clarify-choice {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  font-size: var(--fs-sm);
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
  transition: border-color var(--transition), background var(--transition);
}

.clarify-choice:hover:not(:disabled) {
  background: var(--bg-hover);
}

.clarify-choice:focus-visible {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
  outline: none;
}

.clarify-choice.is-selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg));
}

.clarify-choice__num {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.clarify-choice__text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.clarify-choice__radio {
  width: 15px;
  height: 15px;
  margin: 0;
  flex: none;
  accent-color: var(--text);
}

.clarify-choice__other-input {
  width: 100%;
  min-width: 0;
  padding: 2px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  outline: none;
  background: transparent;
  color: var(--text);
  font: inherit;
}

.clarify-choice__other-input:focus-visible {
  border-bottom-color: var(--border-focus);
}

.clarify-card--plan .clarify-card__head {
  gap: 3px;
  padding: var(--sp-3) var(--sp-3) var(--sp-1);
}

.clarify-card--plan .clarify-card__eyebrow {
  color: var(--text-muted);
  font-weight: 650;
  letter-spacing: 0;
  text-transform: none;
}

.clarify-card--plan .clarify-card__intro {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.clarify-card--plan .clarify-card__body {
  max-height: min(36vh, 300px);
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3) var(--sp-3);
}

.clarify-card--plan .clarify-field {
  min-width: 0;
  gap: var(--sp-2);
}

.clarify-card--plan .clarify-field__label {
  display: grid;
  gap: 2px;
}

.clarify-card--plan .clarify-field__name {
  font-family: inherit;
  font-size: var(--fs-base);
}

.clarify-card--plan .clarify-field__prompt {
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.clarify-card--plan .clarify-field__req {
  display: none;
}

.clarify-card--plan .clarify-field__choices {
  gap: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
}

.clarify-card--plan .clarify-choice {
  min-width: 0;
  min-height: 44px;
  padding: var(--sp-2) var(--sp-3);
  border: 0;
  border-bottom: 1px solid var(--hairline);
  border-radius: 0;
  background: var(--bg-surface);
  transition: background var(--transition);
}

.clarify-card--plan .clarify-choice:last-child {
  border-bottom: 0;
}

.clarify-card--plan .clarify-choice:hover:not(:has(input:disabled)) {
  background: var(--bg-hover);
}

.clarify-card--plan .clarify-choice.is-selected {
  border-color: var(--hairline);
  background: var(--bg-hover);
}

.clarify-card--plan .clarify-choice:has(.clarify-choice__radio:focus-visible) {
  box-shadow: var(--focus-ring-inset);
}

.clarify-card--plan .clarify-choice__num {
  display: none;
}

.clarify-card--plan .clarify-choice__text {
  overflow: visible;
  text-overflow: clip;
  white-space: normal;
  overflow-wrap: anywhere;
}

.clarify-card--plan .clarify-choice__copy--other {
  display: grid;
  width: 100%;
  grid-template-columns: max-content minmax(7rem, 1fr);
  align-items: center;
  gap: var(--sp-2);
}

/* Sticky action bar below the scrollable body. */
.clarify-card__footer {
  position: sticky;
  bottom: 0;
  background: var(--bg-surface);
  border-top: 1px solid var(--hairline);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
}

.clarify-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.clarify-card__plan-actions {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: var(--sp-2);
}

.clarify-card__nav-button {
  display: inline-flex;
  min-width: 0;
  min-height: 36px;
  align-items: center;
  justify-content: flex-start;
  gap: 4px;
  padding: var(--sp-1) var(--sp-2);
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-sm);
}

.clarify-card__nav-button--next,
.clarify-card__nav-button--submit {
  justify-self: end;
  justify-content: flex-end;
  color: var(--text);
  font-weight: 600;
}

.clarify-card__nav-button:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text);
}

.clarify-card__nav-button:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.clarify-card__nav-button:disabled {
  opacity: var(--state-disabled-opacity);
  cursor: not-allowed;
}

.clarify-card__progress {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.clarify-card--plan .clarify-card__footer {
  position: static;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
}

.clarify-card__error {
  color: var(--danger);
  font-size: var(--fs-sm);
  margin: 0;
}

.clarify-card__status {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 0;
}

.clarify-outcome {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: var(--sp-2) auto;
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  background: color-mix(in srgb, var(--ok) 9%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--ok) 42%, var(--border));
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 22px color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok);
  padding: var(--sp-3) var(--sp-4);
  animation: card-enter var(--dur-enter) var(--ease-out) both;
}

.clarify-outcome.is-busy {
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  box-shadow: 0 8px 22px color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
}

.clarify-outcome--plan,
.clarify-outcome--plan.is-busy {
  gap: var(--sp-2);
  background: var(--bg-surface);
  border-color: var(--border);
  box-shadow: none;
  color: var(--text-muted);
  animation: none;
}

.clarify-outcome--plan .clarify-outcome__title {
  font-weight: 600;
}

.clarify-outcome__icon {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, currentColor 13%, transparent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.clarify-outcome.is-busy .clarify-outcome__icon {
  animation: submit-pulse 1.2s ease-in-out infinite;
}

.clarify-outcome__copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.clarify-outcome__title {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 700;
  line-height: 1.35;
}

.clarify-outcome__detail {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.4;
}

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(7px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes submit-pulse {
  0%, 100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.08);
  }
}

@media (max-width: 768px) {
  .clarify-card__intro--long {
    max-block-size: min(45vh, 24rem);
    padding: var(--sp-2);
  }

  .clarify-card__actions {
    flex-direction: column;
    align-items: stretch;
  }

  .clarify-card__actions .btn {
    justify-content: center;
  }
}

@media (max-width: 420px) {
  .clarify-card--plan .clarify-card__head,
  .clarify-card--plan .clarify-card__body,
  .clarify-card--plan .clarify-card__footer {
    padding-right: var(--sp-2);
    padding-left: var(--sp-2);
  }

  .clarify-card--plan .clarify-choice {
    padding-right: var(--sp-2);
    padding-left: var(--sp-2);
  }

  .clarify-card__nav-button:not(.clarify-card__nav-button--submit) span {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .clarify-card {
    animation: none;
  }
}
</style>
