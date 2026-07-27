// MetaSkill preflight checkpoint: pure transforms, field logic, and copy.
// The helpers stay DOM-free; field collection reads the SFC's local v-model
// state and returns the protocol payload used to confirm a run.

import type {
  MetaPreflightFieldSpec,
  MetaPreflightPayload,
} from '@/types/rpc'

export type MetaPreflightLanguage = 'zh' | 'en'

export type MetaPreflightFieldType = 'textarea' | 'boolean' | 'number' | 'select' | 'text'

export interface MetaField extends MetaPreflightFieldSpec {
  name?: string
}

export interface MetaPreflightState {
  runId: string
  metaSkillName: string
  language: MetaPreflightLanguage
  interpretedRequest: string
  missingFields: string[]
  assumptions: string[]
  fields: MetaField[]
  outcome: string
  canSkip: boolean
  requiresGate: boolean
}

export function detectLanguage(text: unknown): MetaPreflightLanguage {
  return /[㐀-䶿一-鿿豈-﫿]/.test(String(text || '')) ? 'zh' : 'en'
}

/** Build a fresh preflight state from a meta_preflight payload. */
export function createPreflight(payload: MetaPreflightPayload): MetaPreflightState {
  const template = payload.request_template || {}
  const language = detectLanguage(
    payload.language ||
      template.language ||
      payload.interpreted_request ||
      template.outcome ||
      '',
  )
  return {
    runId: payload.run_id || '',
    metaSkillName: payload.meta_skill_name || '',
    language,
    interpretedRequest: payload.interpreted_request || '',
    missingFields: payload.missing_fields || [],
    assumptions: payload.assumptions || [],
    fields: Array.isArray(template.fields) ? template.fields : [],
    outcome: template.outcome || template.deliverable || '',
    canSkip: payload.can_skip !== false,
    requiresGate: payload.requires_confirmation === true,
  }
}

/** Resolve the field specs for the missing-field form (filling gaps). */
export function missingFieldSpecs(state: MetaPreflightState): MetaField[] {
  const byName: Record<string, MetaField> = {}
  state.fields.forEach((field) => {
    if (field && field.name) byName[field.name] = field
  })
  return state.missingFields
    .map((name) => byName[name] || ({ name, required: true } as MetaField))
    .filter((field) => field && field.name)
}

/** Template defaults keyed by field name. */
export function defaultFieldValues(fields: MetaField[]): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  ;(Array.isArray(fields) ? fields : []).forEach((field) => {
    if (!field || !field.name) return
    if (field.default != null) out[field.name] = field.default
  })
  return out
}

export function fieldOptions(field: MetaField): unknown[] {
  if (Array.isArray(field.options)) return field.options
  if (Array.isArray(field.choices)) return field.choices
  return []
}

export function normalizeFieldType(field: MetaField): MetaPreflightFieldType {
  const type = String(field.type || field.kind || '').toLowerCase()
  if (field.multiline === true || ['textarea', 'long_text', 'markdown'].includes(type)) {
    return 'textarea'
  }
  if (['bool', 'boolean', 'toggle'].includes(type)) return 'boolean'
  if (['number', 'integer', 'float'].includes(type)) return 'number'
  if (fieldOptions(field).length > 0) return 'select'
  return 'text'
}

export function fieldLabel(field: MetaField): string {
  return field.label || field.title || humanizeToken(field.name)
}

export interface MetaFieldOption {
  value: string
  label: string
}

/** Normalize a raw option entry (string or {value,label}) for a select. */
export function normalizeFieldOption(option: unknown): MetaFieldOption {
  if (typeof option === 'object' && option !== null) {
    const obj = option as { value?: unknown; label?: unknown }
    const raw = obj.value ?? obj.label
    const label = obj.label ?? obj.value
    return { value: String(raw ?? ''), label: String(label ?? '') }
  }
  return { value: String(option ?? ''), label: String(option ?? '') }
}

/** The template default for a single field, as a string for control binding. */
export function fieldDefaultString(field: MetaField): string {
  return field.default != null ? String(field.default) : ''
}

/**
 * Collect submitted field values. Pure read of the live values ref:
 * checkboxes always boolean, empty trimmed strings fall back to template
 * defaults via defaultFieldValues. With useDefaults, returns the defaults only.
 */
export function collectFieldValues(
  state: MetaPreflightState,
  liveValues: Record<string, unknown>,
  options?: { useDefaults?: boolean },
): Record<string, unknown> {
  const out = defaultFieldValues(state.fields)
  if (options && options.useDefaults) return out
  const byName: Record<string, MetaField> = {}
  state.fields.forEach((field) => {
    if (field && field.name) byName[field.name] = field
  })
  // Iterate over every control the form rendered: the union of template
  // fields and any missing-field-only specs.
  const names = new Set<string>()
  state.fields.forEach((f) => f.name && names.add(f.name))
  missingFieldSpecs(state).forEach((f) => f.name && names.add(f.name))
  names.forEach((name) => {
    const spec = byName[name]
    const isCheckbox = spec ? normalizeFieldType(spec) === 'boolean' : false
    const raw = liveValues[name]
    if (isCheckbox) {
      out[name] = raw === true
      return
    }
    const value = String(raw ?? '').trim()
    if (value) out[name] = value
  })
  return out
}

/**
 * Validate required text fields. Returns the names of invalid fields (empty
 * required, non-checkbox); the SFC marks rows is-invalid / aria-invalid.
 */
export function validateRequired(
  state: MetaPreflightState,
  liveValues: Record<string, unknown>,
): string[] {
  const invalid: string[] = []
  missingFieldSpecs(state).forEach((field) => {
    const name = String(field.name || '')
    if (!name) return
    const isCheckbox = normalizeFieldType(field) === 'boolean'
    const value = isCheckbox ? liveValues[name] === true : String(liveValues[name] ?? '').trim()
    if (field.required === true && !isCheckbox && value === '') invalid.push(name)
  })
  return invalid
}

export interface MetaPreflightCopy {
  aria: (name: string) => string
  badge: string
  headline: (name: string) => string
  headlineWithOutcome: (name: string, outcome: string) => string
  understood: string
  correctionHint: string
  assumptions: string
  missingFields: string
  useDefaults: string
  cancel: string
  dismiss: string
  start: string
  starting: string
  required: string
  requiredError: string
  selectOne: string
  error: string
  running: (name: string) => string
  dismissed: string
}

export function preflightCopy(language: MetaPreflightLanguage): MetaPreflightCopy {
  if (language === 'zh') {
    return {
      aria: (name) => `运行 ${name} 前确认`,
      badge: '检查点',
      headline: (name) => `我准备运行 ${name}`,
      headlineWithOutcome: (name, outcome) => `我准备运行 ${name}，产出 ${outcome}`,
      understood: '我理解的是',
      correctionHint: '不对的话，直接回复补充，我会重新理解。',
      assumptions: '我会先按这些假设处理',
      missingFields: '开始前还需要',
      useDefaults: '使用默认值运行',
      cancel: '取消',
      dismiss: '知道了',
      start: '开始运行',
      starting: '启动中...',
      required: '必填',
      requiredError: '必填。',
      selectOne: '选择一项',
      error: '没能启动，请重试或直接回复修改。',
      running: (name) => `正在运行 ${name}...`,
      dismissed: '已收起这条检查点。',
    }
  }
  return {
    aria: (name) => `Confirm before running ${name}`,
    badge: 'Checkpoint',
    headline: (name) => `Before running ${name}`,
    headlineWithOutcome: (name, outcome) => `Before running ${name}: ${outcome}`,
    understood: 'I understood',
    correctionHint: 'If this is off, reply with the correction and I will update it.',
    assumptions: 'I will use these assumptions',
    missingFields: 'Needed before starting',
    useDefaults: 'Use defaults',
    cancel: 'Cancel',
    dismiss: 'Dismiss',
    start: 'Start',
    starting: 'Starting...',
    required: 'Required',
    requiredError: 'Please fill this in.',
    selectOne: 'Choose one',
    error: 'Could not start. Retry or reply with a correction.',
    running: (name) => `Running ${name}...`,
    dismissed: 'Dismissed this checkpoint.',
  }
}

export function skillDisplayName(name: string): string {
  return humanizeToken(String(name || '').replace(/^meta[-_]/, ''))
}

export function humanizeToken(value: string | null | undefined): string {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
}

export function safeId(value: string | null | undefined): string {
  return String(value || '').replace(/[^a-zA-Z0-9_-]/g, '-')
}
