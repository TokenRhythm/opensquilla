import { computed, ref, type ComputedRef } from 'vue'
import { stripRedactionSentinels } from '@/composables/setup/channelRpc'

interface ChannelSpec {
  type: string
  label: string
  fields?: Array<{ name: string; label: string; default?: string | boolean | number; [key: string]: unknown }>
  whatYouNeed?: string[]
}

interface ChannelFieldSpec {
  name: string
  label: string
  default?: string | boolean | number
  secret?: boolean
  [key: string]: unknown
}

interface ChannelFieldRow {
  field: ChannelFieldSpec
  value: string
}

interface ChannelSecretRow {
  field: ChannelFieldSpec
  hasStored: boolean
  replacing: boolean
  value: string
}

export function buildChannelEntry(type: string, values: Record<string, unknown>): Record<string, unknown> {
  const entry: Record<string, unknown> = { type }
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value !== undefined) entry[key] = value
  })
  return entry
}

export function useSetupChannelsForm() {
  const channelType = ref('')
  const channelFieldValues = ref<Record<string, unknown>>({})
  // Fields of the currently-selected channel spec — kept so payload() can drop
  // values of fields that show_when has hidden.
  const activeFields = ref<ChannelFieldSpec[]>([])

  // Edit mode. Secrets NEVER enter channelFieldValues while editing — the
  // redacted '***' from channels.get has no path into an outbound value.
  const mode = ref<'compose' | 'edit'>('compose')
  const editName = ref('')
  interface SecretState { hasStored: boolean; replacing: boolean; value: string }
  const secretStates = ref<Record<string, SecretState>>({})
  // Stored-entry keys that are not spec fields (hand-edited config extras) —
  // carried through payload() so an edit round-trip doesn't drop them.
  const passthrough = ref<Record<string, unknown>>({})

  const isEditing = computed(() => mode.value === 'edit')

  function isSecretField(name: string): boolean {
    return activeFields.value.some(f => f.name === name && f.secret === true)
  }

  // Serialized form of the *effective outbound contribution*: an untouched
  // stored secret contributes '' (keep-current), so it can never read dirty.
  const serialized = computed(() => JSON.stringify({
    m: mode.value,
    n: editName.value,
    t: channelType.value,
    v: channelFieldValues.value,
    s: Object.fromEntries(Object.entries(secretStates.value)
      .map(([key, s]) => [key, s.replacing || !s.hasStored ? s.value : ''])),
  }))
  // Seed from the initial state so the pristine form is never dirty while config loads.
  const baseline = ref(serialized.value)
  const isDirty = computed(() => serialized.value !== baseline.value)

  // Switching channel type resets the entry form; type choice alone is not an
  // unsaved edit. `seed` overlays spec defaults BEFORE the baseline capture,
  // so a caller-provided prefill (e.g. the suggested channel name) is part of
  // the pristine form, never a dirty edit.
  function resetForSpec(spec: ChannelSpec | null | undefined, seed?: Record<string, unknown>) {
    mode.value = 'compose'
    editName.value = ''
    secretStates.value = {}
    passthrough.value = {}
    activeFields.value = (spec?.fields ?? []) as ChannelFieldSpec[]
    channelFieldValues.value = {}
    spec?.fields?.forEach(field => {
      channelFieldValues.value[field.name] = seed?.[field.name] ?? field.default ?? ''
    })
    baseline.value = serialized.value
  }

  /**
   * Seed the form from an existing (redacted) entry — edit mode. Non-secret
   * values load into the ordinary field map; secrets become masked
   * stored-credential state and their redacted values are discarded.
   */
  function initFromEntry(
    spec: ChannelSpec,
    entry: Record<string, unknown>,
    secretFields: string[],
  ) {
    mode.value = 'edit'
    channelType.value = spec.type
    editName.value = String(entry.name ?? '')
    activeFields.value = (spec.fields ?? []) as ChannelFieldSpec[]
    channelFieldValues.value = {}
    secretStates.value = {}
    const specNames = new Set(activeFields.value.map(f => f.name))
    for (const field of activeFields.value) {
      if (field.secret === true) {
        const hasStored = secretFields.includes(field.name)
        secretStates.value[field.name] = { hasStored, replacing: !hasStored, value: '' }
      } else {
        channelFieldValues.value[field.name] = entry[field.name] ?? field.default ?? ''
      }
    }
    passthrough.value = Object.fromEntries(
      Object.entries(entry).filter(([key]) => !specNames.has(key) && key !== 'type'),
    )
    baseline.value = serialized.value
  }

  function replaceSecret(name: string) {
    const state = secretStates.value[name]
    if (!state) return
    secretStates.value = { ...secretStates.value, [name]: { ...state, replacing: true, value: '' } }
  }

  function cancelSecretReplace(name: string) {
    const state = secretStates.value[name]
    if (!state || !state.hasStored) return
    secretStates.value = { ...secretStates.value, [name]: { ...state, replacing: false, value: '' } }
  }

  function updateField(name: string, value: unknown) {
    if (mode.value === 'edit' && isSecretField(name)) {
      const state = secretStates.value[name]
      if (state) {
        secretStates.value = { ...secretStates.value, [name]: { ...state, value: String(value ?? '') } }
      }
      return
    }
    channelFieldValues.value[name] = value
  }

  function selectChannelType(value: string) {
    channelType.value = value
  }

  /**
   * Visible required fields whose effective value is blank. Edit mode: a
   * stored secret satisfies its requirement; a replacing-but-empty box does
   * too (keep-current), so only never-stored empty secrets are missing.
   */
  function missingRequiredFields(): string[] {
    const missing: string[] = []
    for (const row of channelFieldRows(activeFields.value)) {
      const field = row.field
      if (field.required !== true) continue
      if (field.secret === true && mode.value === 'edit') {
        const state = secretStates.value[field.name]
        if (state && !state.hasStored && !state.value.trim()) missing.push(field.name)
        continue
      }
      const value = field.secret === true && mode.value === 'edit'
        ? ''
        : String(channelFieldValues.value[field.name] ?? '')
      if (!value.trim()) missing.push(field.name)
    }
    return missing
  }

  function payload(): Record<string, unknown> {
    // Only submit values for fields that are currently visible. A hidden
    // field's stale value (e.g. a Socket-mode app_token left over after the
    // user switched connection_mode to webhook) must not be sent.
    const visible = new Set(channelFieldRows(activeFields.value).map(row => row.field.name))
    const filtered: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(channelFieldValues.value)) {
      if (visible.has(key)) filtered[key] = value
    }
    if (mode.value !== 'edit') {
      return stripRedactionSentinels(buildChannelEntry(channelType.value, filtered))
    }
    // Edit mode: untouched stored secrets contribute '' → dropped by
    // buildChannelEntry → key omitted → server-side keep-current merge.
    for (const [key, state] of Object.entries(secretStates.value)) {
      if (!visible.has(key)) continue
      filtered[key] = state.replacing || !state.hasStored ? state.value : ''
    }
    const entry = { ...passthrough.value, ...buildChannelEntry(channelType.value, filtered) }
    // The identity key must survive even if a spec ever hides or blanks the
    // name field — it is what the server matches the existing entry on.
    entry.name = editName.value
    return stripRedactionSentinels(entry)
  }

  // Current value of a field (user edit, else its default) — used both to render
  // a field and to evaluate other fields' show_when conditions against it.
  function fieldCurrentValue(name: string, fields: ChannelFieldSpec[]): string {
    const v = channelFieldValues.value[name]
    if (v !== undefined) return String(v ?? '')
    const f = fields.find(x => x.name === name)
    return String(f?.default ?? '')
  }

  // A field is shown unless its show_when references a controlling field whose
  // current value doesn't match. Backend ships show_when as `field.showWhen`,
  // e.g. { connection_mode: 'socket' } or { transport_name: 'webhook' }; all
  // keys must match. This is what makes the form show the fields for the chosen
  // connection mode instead of every field at once.
  function fieldVisible(field: ChannelFieldSpec, fields: ChannelFieldSpec[]): boolean {
    const showWhen = field.showWhen as Record<string, unknown> | undefined
    if (!showWhen || typeof showWhen !== 'object') return true
    return Object.entries(showWhen).every(
      ([ctrl, expected]) => fieldCurrentValue(ctrl, fields) === String(expected),
    )
  }

  function channelFieldRows(fields: ChannelFieldSpec[]): ChannelFieldRow[] {
    return fields
      .filter(field => fieldVisible(field, fields))
      .map(field => ({ field, value: fieldCurrentValue(field.name, fields) }))
  }

  // Visible secret fields rendered as masked stored-credential rows in edit mode.
  function channelSecretRows(fields: ChannelFieldSpec[]): ChannelSecretRow[] {
    if (mode.value !== 'edit') return []
    return fields
      .filter(field => field.secret === true && fieldVisible(field, fields))
      .map(field => {
        const state = secretStates.value[field.name] || { hasStored: false, replacing: true, value: '' }
        return { field, hasStored: state.hasStored, replacing: state.replacing, value: state.value }
      })
  }

  function createPanel(specFields: ComputedRef<ChannelFieldSpec[]>) {
    return computed(() => ({
      // In edit mode secrets render via secretRows, not as plain fields.
      channelFields: channelFieldRows(specFields.value)
        .filter(row => mode.value !== 'edit' || row.field.secret !== true),
      secretRows: channelSecretRows(specFields.value),
    }))
  }

  return {
    isDirty,
    isEditing,
    initFromEntry,
    resetForSpec,
    replaceSecret,
    cancelSecretReplace,
    selectChannelType,
    updateField,
    payload,
    missingRequiredFields,
    createPanel,
  }
}
