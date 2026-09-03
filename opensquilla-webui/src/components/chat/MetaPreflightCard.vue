<template>
  <Transition name="preflight-swap" mode="out-in">
  <!-- Collapsed summary: controller drives running/cancelled phases -->
  <section
    v-if="isCollapsedPhase"
    key="collapsed"
    class="meta-preflight meta-preflight--collapsed"
    :data-run-id="state.runId"
    :data-state="phase"
    :data-language="state.language"
    role="group"
    :aria-label="copy.aria(state.metaSkillName)"
  >
    <p class="meta-preflight-collapsed">{{ collapsedText }}</p>
  </section>

  <!-- Active checkpoint form -->
  <section
    v-else
    key="form"
    class="meta-preflight"
    :data-run-id="state.runId"
    :data-state="phase"
    :data-language="state.language"
    :aria-busy="phase === 'submitting' ? 'true' : 'false'"
    role="group"
    :aria-label="copy.aria(state.metaSkillName)"
  >
    <header class="meta-preflight-head">
      <span class="meta-preflight-title" :title="headline">{{ headline }}</span>
      <span class="meta-preflight-badge">{{ copy.badge }}</span>
    </header>
    <div class="meta-preflight-body">
      <section class="meta-preflight-understood">
        <h4>{{ copy.understood }}</h4>
        <p class="meta-preflight-request">{{ state.interpretedRequest }}</p>
        <p class="meta-preflight-muted">{{ copy.correctionHint }}</p>
      </section>

      <section v-if="state.assumptions.length > 0">
        <h4>{{ copy.assumptions }}</h4>
        <ul class="meta-preflight-list">
          <li v-for="(item, i) in state.assumptions" :key="i">{{ item }}</li>
        </ul>
      </section>

      <section v-if="state.requiresGate && fields.length > 0" class="meta-preflight-fields">
        <h4>{{ copy.missingFields }}</h4>
        <div class="meta-preflight-field-list">
          <label
            v-for="field in fields"
            :key="field.name"
            class="meta-preflight-field"
            :class="{ 'is-invalid': invalidFields.has(String(field.name)) }"
            :data-field-name="field.name"
          >
            <span class="meta-preflight-field-label">
              {{ labelFor(field) }}
              <span v-if="field.required === true" class="meta-preflight-required">{{ copy.required }}</span>
            </span>
            <span
              v-if="helperFor(field)"
              class="meta-preflight-field-help"
              :id="`${inputId(field)}-help`"
            >{{ helperFor(field) }}</span>

            <textarea
              v-if="typeFor(field) === 'textarea'"
              :id="inputId(field)"
              v-model="values[String(field.name)]"
              class="meta-preflight-field-control"
              :data-field-name="field.name"
              :aria-required="field.required === true ? 'true' : 'false'"
              :aria-invalid="invalidFields.has(String(field.name)) ? 'true' : 'false'"
              :aria-describedby="describedBy(field)"
              rows="3"
            />
            <input
              v-else-if="typeFor(field) === 'boolean'"
              :id="inputId(field)"
              v-model="checkboxValues[String(field.name)]"
              class="meta-preflight-field-control"
              :data-field-name="field.name"
              type="checkbox"
              :aria-required="field.required === true ? 'true' : 'false'"
              :aria-describedby="describedBy(field)"
            />
            <select
              v-else-if="typeFor(field) === 'select'"
              :id="inputId(field)"
              v-model="values[String(field.name)]"
              class="meta-preflight-field-control"
              :data-field-name="field.name"
              :aria-required="field.required === true ? 'true' : 'false'"
              :aria-invalid="invalidFields.has(String(field.name)) ? 'true' : 'false'"
              :aria-describedby="describedBy(field)"
            >
              <option value="">{{ copy.selectOne }}</option>
              <option v-for="(opt, i) in optionsFor(field)" :key="i" :value="opt.value">{{ opt.label }}</option>
            </select>
            <input
              v-else
              :id="inputId(field)"
              v-model="values[String(field.name)]"
              class="meta-preflight-field-control"
              :data-field-name="field.name"
              :type="typeFor(field) === 'number' ? 'number' : 'text'"
              :aria-required="field.required === true ? 'true' : 'false'"
              :aria-invalid="invalidFields.has(String(field.name)) ? 'true' : 'false'"
              :aria-describedby="describedBy(field)"
            />

            <span class="meta-preflight-field-error" :id="`${inputId(field)}-error`" aria-live="polite">
              {{ invalidFields.has(String(field.name)) ? copy.requiredError : '' }}
            </span>
          </label>
        </div>
      </section>

      <p v-if="phase === 'error'" class="meta-preflight-error" role="alert">{{ errorMessage }}</p>
    </div>
    <div class="meta-preflight-actions">
      <button
        v-if="state.requiresGate && state.canSkip"
        class="meta-preflight-link"
        type="button"
        data-action="defaults"
        :disabled="busy || turnActionsDisabled"
        @click="onAction('defaults')"
      >
        {{ copy.useDefaults }}
      </button>
      <button
        class="meta-preflight-secondary"
        type="button"
        data-action="dismiss"
        :disabled="busy"
        @click="onAction('dismiss')"
      >
        {{ state.requiresGate ? copy.cancel : copy.dismiss }}
      </button>
      <button
        v-if="state.requiresGate"
        class="meta-preflight-primary"
        type="button"
        data-action="continue"
        :disabled="busy || turnActionsDisabled"
        :aria-busy="phase === 'submitting' ? 'true' : 'false'"
        @click="onAction('continue')"
      >
        {{ phase === 'submitting' ? copy.starting : copy.start }}
      </button>
    </div>
  </section>
  </Transition>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  collectFieldValues,
  fieldDefaultString,
  fieldLabel,
  missingFieldSpecs,
  normalizeFieldOption,
  normalizeFieldType,
  preflightCopy,
  skillDisplayName,
  validateRequired,
  type MetaField,
  type MetaPreflightState,
} from '@/utils/chat/metaPreflight'

export type MetaPreflightPhase = 'ready' | 'submitting' | 'running' | 'cancelled' | 'error'

export interface MetaPreflightActionPayload {
  action: 'continue' | 'defaults' | 'dismiss'
  runId: string
  metaSkillName: string
  interpretedRequest: string
  missingFields: string[]
  confirmedFields: Record<string, unknown>
}

const props = defineProps<{
  state: MetaPreflightState
  phase: MetaPreflightPhase
  errorText?: string
  turnActionsDisabled?: boolean
}>()

const emit = defineEmits<{
  action: [payload: MetaPreflightActionPayload]
}>()

// Live field values, keyed by field name. String controls bind here;
// checkboxes bind to checkboxValues (kept boolean for v-model).
const values = reactive<Record<string, string>>({})
const checkboxValues = reactive<Record<string, boolean>>({})
const invalidFields = ref<Set<string>>(new Set())

const copy = computed(() => preflightCopy(props.state.language))

const isCollapsedPhase = computed(() => props.phase === 'running' || props.phase === 'cancelled')
const busy = computed(() => props.phase === 'submitting')

// Union of template fields and missing-field-only specs, in render order.
const fields = computed<MetaField[]>(() => {
  const seen = new Set<string>()
  const out: MetaField[] = []
  for (const spec of missingFieldSpecs(props.state)) {
    const name = String(spec.name || '')
    if (!name || seen.has(name)) continue
    seen.add(name)
    out.push(spec)
  }
  return out
})

const headline = computed(() => {
  const displayName = skillDisplayName(props.state.metaSkillName)
  return props.state.outcome
    ? copy.value.headlineWithOutcome(displayName, props.state.outcome)
    : copy.value.headline(displayName)
})

const errorMessage = computed(() => props.errorText || copy.value.error)

const collapsedText = computed(() => {
  const name = skillDisplayName(props.state.metaSkillName)
  return props.phase === 'running' ? copy.value.running(name) : copy.value.dismissed
})

function typeFor(field: MetaField) {
  return normalizeFieldType(field)
}

function labelFor(field: MetaField): string {
  return fieldLabel(field)
}

function helperFor(field: MetaField): string {
  return String(field.description || field.help || field.hint || '')
}

function optionsFor(field: MetaField) {
  const raw = Array.isArray(field.options) ? field.options : Array.isArray(field.choices) ? field.choices : []
  return raw.map(normalizeFieldOption)
}

function inputId(field: MetaField): string {
  return `meta-preflight-field-${String(field.name || '').replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

function describedBy(field: MetaField): string {
  return `${inputId(field)}-help ${inputId(field)}-error`.trim()
}

// Seed live values from template defaults whenever the run changes.
watch(
  () => props.state.runId,
  () => {
    for (const key of Object.keys(values)) delete values[key]
    for (const key of Object.keys(checkboxValues)) delete checkboxValues[key]
    invalidFields.value = new Set()
    for (const field of fields.value) {
      const name = String(field.name || '')
      if (!name) continue
      if (normalizeFieldType(field) === 'boolean') {
        const def = fieldDefaultString(field)
        checkboxValues[name] = def === 'true' || def === '1'
      } else {
        values[name] = fieldDefaultString(field)
      }
    }
  },
  { immediate: true },
)

function liveValues(): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...values }
  for (const [k, v] of Object.entries(checkboxValues)) merged[k] = v
  return merged
}

function onAction(action: 'continue' | 'defaults' | 'dismiss') {
  if (busy.value) return
  if (action === 'continue') {
    const invalid = validateRequired(props.state, liveValues())
    invalidFields.value = new Set(invalid)
    if (invalid.length > 0) return
  }
  const confirmedFields = collectFieldValues(props.state, liveValues(), {
    useDefaults: action === 'defaults',
  })
  emit('action', {
    action,
    runId: props.state.runId,
    metaSkillName: props.state.metaSkillName,
    interpretedRequest: props.state.interpretedRequest,
    missingFields: props.state.missingFields,
    confirmedFields,
  })
}
</script>

<style scoped>
.meta-preflight {
  width: calc(100% - 32px);
  max-width: min(760px, 100%);
  margin: 10px auto;
  padding: 12px;
  border: 1px solid color-mix(in srgb, var(--border) 84%, var(--accent) 16%);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
  color: var(--text);
  font-size: var(--fs-sm, 0.875rem);
  flex-shrink: 0;
}

.meta-preflight-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.meta-preflight-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-preflight-badge {
  border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
  border-radius: var(--radius-full);
  padding: 2px 8px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  font-size: var(--fs-xs, 0.75rem);
  font-weight: 650;
}

.meta-preflight-body {
  display: grid;
  gap: 10px;
}

.meta-preflight-body section {
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg-base, var(--bg)) 72%, transparent);
}

.meta-preflight-body h4 {
  margin: 0 0 5px;
  color: var(--text-muted);
  font-size: var(--fs-xs, 0.75rem);
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.meta-preflight-request,
.meta-preflight-outcome,
.meta-preflight-muted {
  margin: 0;
  color: var(--text-muted);
  line-height: 1.5;
}

.meta-preflight-request {
  color: var(--text);
  white-space: pre-wrap;
}

.meta-preflight-list {
  margin: 0;
  padding-left: 18px;
  color: var(--text-muted);
  line-height: 1.55;
}

.meta-preflight-field-list {
  display: grid;
  gap: 9px;
}

.meta-preflight-field {
  display: grid;
  gap: 5px;
}

.meta-preflight-field-label {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  font-size: var(--fs-xs, 0.75rem);
  font-weight: 700;
}

.meta-preflight-required {
  border: 1px solid color-mix(in srgb, var(--danger) 34%, transparent);
  border-radius: var(--radius-full);
  padding: 1px 6px;
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  font-size: 0.68rem;
  font-weight: 650;
}

.meta-preflight-field-help,
.meta-preflight-field-error {
  color: var(--text-muted);
  font-size: var(--fs-xs, 0.75rem);
  line-height: 1.45;
}

.meta-preflight-field-error {
  min-height: 1em;
  color: var(--danger);
}

.meta-preflight-field-control {
  width: 100%;
  min-height: 34px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 7px 9px;
  background: var(--bg-surface);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm, 0.875rem);
}

textarea.meta-preflight-field-control {
  min-height: 78px;
  resize: vertical;
}

.meta-preflight-field-control[type="checkbox"] {
  width: 18px;
  min-height: 18px;
  padding: 0;
}

.meta-preflight-field-control:focus {
  border-color: var(--accent);
  outline: 2px solid color-mix(in srgb, var(--accent) 22%, transparent);
  outline-offset: 1px;
}

.meta-preflight-field.is-invalid .meta-preflight-field-control {
  border-color: var(--danger);
}

.meta-preflight-error {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--danger) 32%, transparent);
  border-radius: var(--radius-md);
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  font-size: var(--fs-xs, 0.75rem);
  line-height: 1.45;
}

.meta-preflight-actions {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.meta-preflight-actions button {
  padding: 5px 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs, 0.75rem);
  transition: background var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.meta-preflight-actions button:hover,
.meta-preflight-actions button:focus-visible {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  outline: none;
}

.meta-preflight-actions .meta-preflight-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--accent-foreground);
  font-weight: 700;
}

.meta-preflight-actions .meta-preflight-primary:hover,
.meta-preflight-actions .meta-preflight-primary:focus-visible {
  border-color: var(--accent-hover);
  background: var(--accent-hover);
  color: var(--accent-foreground);
}

.meta-preflight-actions .meta-preflight-secondary {
  background: var(--bg-surface);
}

.meta-preflight-actions .meta-preflight-link {
  margin-right: auto;
  border-color: transparent;
  background: transparent;
  color: var(--text-muted);
}

.meta-preflight-actions button:disabled {
  cursor: wait;
  opacity: 0.72;
}

.meta-preflight--collapsed {
  padding: 9px 12px;
}

.meta-preflight-collapsed {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs, 0.75rem);
  font-weight: 650;
}

@media (max-width: 640px) {
  .meta-preflight {
    width: calc(100% - 16px);
    margin: 8px auto;
  }

  .meta-preflight-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .meta-preflight-actions .meta-preflight-link {
    margin-right: 0;
  }

  .meta-preflight-actions button {
    min-height: 44px;
  }
}

/* ── Preflight form↔summary crossfade ──────────────────────────────────
   When the form collapses to the one-line "running" summary (or expands
   back), the swap fades out the leaving element and fades in the entering
   one. mode="out-in" ensures no overlapping height jump. */
.preflight-swap-enter-from {
  opacity: 0;
  transform: translateY(4px);
}

.preflight-swap-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.preflight-swap-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.preflight-swap-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (prefers-reduced-motion: reduce) {
  .meta-preflight-field-control,
  .meta-preflight-actions button {
    scroll-behavior: auto;
    transition: none;
  }

  .preflight-swap-enter-active,
  .preflight-swap-leave-active {
    transition: none;
  }
}
</style>
