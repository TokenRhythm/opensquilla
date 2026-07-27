import type { InterruptClarifyData, InterruptClarifyField } from '@/types/parts'

function recordFromValue(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text.startsWith('{')) return null
    try {
      value = JSON.parse(text)
    } catch {
      return null
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

/**
 * Normalize the paused user-input contract emitted by interactive tools.
 *
 * Gateway events may preserve the contract as an object or serialize it into
 * the tool result JSON. Keeping this parser shared prevents live events and
 * restored history from disagreeing about whether a Clarify card exists.
 */
export function clarifyRequestFromValue(value: unknown): InterruptClarifyData | null {
  const raw = recordFromValue(value)
  if (!raw || raw.kind !== 'user_input' || raw.paused !== true) return null

  const schema = recordFromValue(raw.clarify_schema)
  if (!schema) return null
  const rawFields = Array.isArray(schema.fields) ? schema.fields : []
  const fields: InterruptClarifyField[] = []
  for (const entry of rawFields) {
    const field = recordFromValue(entry)
    if (!field) continue
    const name = String(field.name || '').trim()
    if (!name) continue
    const rawType = String(field.type || 'string').toLowerCase()
    const options = Array.isArray(field.options)
      ? field.options.flatMap(entry => {
          const option = recordFromValue(entry)
          const label = String(option?.label || '').trim()
          return label
            ? [{ label, description: String(option?.description || '') }]
            : []
        })
      : undefined
    fields.push({
      name,
      prompt: String(field.prompt || ''),
      type: rawType === 'choice' ? 'enum' : rawType,
      required: field.required === true,
      defaultValue: field.default == null ? '' : String(field.default),
      choices: Array.isArray(field.choices) ? field.choices.map(String) : [],
      ...(field.header ? { header: String(field.header) } : {}),
      ...(options?.length ? { options } : {}),
      ...(field.allow_other === true || field.allowOther === true
        ? { allowOther: true }
        : {}),
    })
  }
  if (!fields.length) return null

  const requestId = typeof raw.request_id === 'string'
    ? raw.request_id
    : typeof raw.requestId === 'string' ? raw.requestId : ''
  return {
    intro: String(schema.intro || ''),
    fields,
    ...(requestId ? { requestId } : {}),
    runId: typeof raw.run_id === 'string' ? raw.run_id : '',
    step: typeof raw.step === 'string' ? raw.step : '',
  }
}

export interface UserInputOutcome {
  requestId: string
  status: 'answered' | 'cancelled' | 'expired'
}

/** Recognize the terminal half of a deferred user-input tool result. */
export function userInputOutcomeFromValue(value: unknown): UserInputOutcome | null {
  const raw = recordFromValue(value)
  if (!raw || raw.kind !== 'user_input' || raw.paused !== false) return null
  const status = String(raw.status || '')
  if (!['answered', 'cancelled', 'expired'].includes(status)) return null
  const requestId = String(raw.request_id || raw.requestId || '').trim()
  if (!requestId) return null
  return { requestId, status: status as UserInputOutcome['status'] }
}
