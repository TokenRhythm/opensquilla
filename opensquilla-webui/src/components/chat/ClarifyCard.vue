<template>
  <!-- Prominent outcome banner after the reply is sent -->
  <div
    v-if="submitted"
    class="clarify-outcome"
    :class="{ 'is-busy': busy }"
    data-testid="clarify-outcome"
    role="status"
    aria-live="polite"
    aria-atomic="true"
  >
    <span class="clarify-outcome__icon" aria-hidden="true">
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
      <p v-if="request.intro" class="clarify-card__intro">{{ request.intro }}</p>
    </header>

    <div class="clarify-card__body">
      <div v-for="field in request.fields" :key="field.name" class="clarify-field">
        <label class="clarify-field__label" :for="fieldId(field.name)">
          <span class="clarify-field__name">{{ field.header || field.name }}</span>
          <span v-if="field.prompt && field.prompt !== field.name" class="clarify-field__prompt">
            {{ field.prompt }}
          </span>
          <span class="clarify-field__req">{{ field.required ? t('chat.clarify.required') : t('chat.clarify.optional') }}</span>
        </label>

        <!-- Enum: numbered choices -->
        <div
          v-if="field.type === 'enum' && field.choices.length"
          class="clarify-field__choices"
          role="radiogroup"
          :aria-label="field.name"
        >
          <button
            v-for="(choice, idx) in field.choices"
            :key="choice"
            type="button"
            class="clarify-choice"
            :class="{ 'is-selected': values[field.name] === choice }"
            role="radio"
            :aria-checked="values[field.name] === choice"
            :disabled="busy"
            @click="values[field.name] = choice"
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
              v-model="values[field.name]"
              class="clarify-field__input"
              type="text"
              :placeholder="t('chat.clarify.otherPlaceholder')"
              :disabled="busy"
            />
          </label>
        </div>

        <!-- Bool: explicit true/false select -->
        <select
          v-else-if="field.type === 'bool'"
          :id="fieldId(field.name)"
          v-model="values[field.name]"
          class="clarify-field__input"
          :disabled="busy"
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
        />
      </div>
    </div>

    <footer class="clarify-card__footer">
      <div class="clarify-card__actions">
        <button
          class="btn btn--primary"
          type="button"
          :disabled="busy"
          @click="onSubmit"
        >
          {{ busy ? t('chat.clarify.sendingReply') : t('chat.clarify.sendReply') }}
        </button>
        <button class="btn btn--ghost" type="button" :disabled="busy" @click="$emit('dismiss')">
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
import { reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatClarifyRequest } from '@/composables/chat/useChatApprovals'

const { t } = useI18n()

const props = defineProps<{
  request: ChatClarifyRequest
  submitted?: boolean
  busy?: boolean
  error?: string
}>()

const emit = defineEmits<{
  submit: [fields: Record<string, string>]
  dismiss: []
}>()

const values = reactive<Record<string, string>>({})

// The parent rebuilds the request object while messages stream and when the
// conversation is re-rendered. Watching that object directly used to reset
// every field to its default even though the user was still answering the same
// clarify request. Reset only when the logical request itself changes.
watch(
  () => `${props.request.runId}\u0000${props.request.step}`,
  () => {
    const request = props.request
    for (const key of Object.keys(values)) delete values[key]
    for (const field of request.fields) {
      values[field.name] = field.defaultValue || ''
    }
  },
  { immediate: true },
)

function fieldId(name: string): string {
  return `clarify-field-${name}`
}

function optionDescription(
  field: ChatClarifyRequest['fields'][number],
  choice: string,
): string {
  return field.options?.find(option => option.label === choice)?.description || ''
}

function onSubmit() {
  if (props.busy) return
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
  .clarify-card__actions {
    flex-direction: column;
    align-items: stretch;
  }

  .clarify-card__actions .btn {
    justify-content: center;
  }
}

@media (prefers-reduced-motion: reduce) {
  .clarify-card {
    animation: none;
  }
}
</style>
