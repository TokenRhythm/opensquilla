import type { TraceSpan } from '@/types/traceView'
import { traceEventCategory, type TraceDisplayCategory } from './traceProjection'

export interface InspectorBlock {
  id: string
  label: string
  labelKey?: string
  value: unknown
  format?: 'value' | 'code' | 'markdown' | 'diff'
  language?: string
  secondaryValue?: unknown
  collapsed?: boolean
  children?: InspectorBlock[]
}

export interface TraceInspectorModel {
  category: TraceDisplayCategory
  summaryKey: string
  facts: Array<{ key: string; value: unknown }>
  inputs: InspectorBlock[]
  outputs: InspectorBlock[]
}

type RecordValue = Record<string, unknown>
type BlockContext = { row: TraceSpan; path?: string; side: 'input' | 'output' }

function record(value: unknown): RecordValue | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : undefined
}

/** Decode structured JSON wrappers; ordinary string values remain authored text. */
function structured(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const text = value.trim()
  if (!text || !['{', '[', '"'].includes(text[0]!)) return value
  try {
    const decoded: unknown = JSON.parse(text)
    if (typeof decoded === 'string') {
      const nested = structured(decoded)
      return record(nested) || Array.isArray(nested) ? nested : value
    }
    return record(decoded) || Array.isArray(decoded) ? decoded : value
  } catch {
    return value
  }
}

function languageFor(path?: string): string | undefined {
  const extension = path?.split(/[\\/]/).pop()?.split('.').pop()?.toLowerCase()
  const languages: Record<string, string> = {
    py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'typescript', jsx: 'javascript', vue: 'html',
    json: 'json', jsonl: 'json', html: 'html', htm: 'html', css: 'css',
    scss: 'scss', md: 'markdown', sh: 'bash', bash: 'bash', zsh: 'bash',
    ps1: 'powershell', yaml: 'yaml', yml: 'yaml', toml: 'ini', rs: 'rust',
    go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp', sql: 'sql', xml: 'xml',
  }
  return extension ? languages[extension] : undefined
}

function pathFrom(value: RecordValue): string | undefined {
  return [value.path, value.file_path, value.filePath, value.filename]
    .find((item): item is string => typeof item === 'string' && item.length > 0)
}

function block(
  id: string, label: string, value: unknown,
  options: Omit<InspectorBlock, 'id' | 'label' | 'value'> = {},
): InspectorBlock {
  return { id, label, value, format: 'value', ...options }
}

function messageBlocks(value: unknown[], id: string): InspectorBlock[] {
  return value.flatMap((message, index) => {
    const item = record(message)
    const prefix = `${id}.${index}`
    if (!item) return [block(prefix, `message ${index + 1}`, message)]
    const role = typeof item.role === 'string' ? item.role : 'message'
    const roleKey = ['system', 'developer', 'user', 'assistant', 'tool'].includes(role)
      ? `roles.${role}` : undefined
    const content = item.content
    const blocks: InspectorBlock[] = []
    if (typeof content === 'string') {
      blocks.push(block(`${prefix}.content`, role, content, {
        labelKey: roleKey, format: 'markdown',
      }))
    } else if (Array.isArray(content)) {
      content.forEach((part, partIndex) => {
        const partRecord = record(part)
        if (partRecord && typeof partRecord.text === 'string') {
          blocks.push(block(`${prefix}.content.${partIndex}`, role, partRecord.text, {
            labelKey: roleKey, format: 'markdown',
          }))
          const remaining = Object.fromEntries(Object.entries(partRecord)
            .filter(([key]) => key !== 'text'))
          if (Object.keys(remaining).length) {
            blocks.push(block(`${prefix}.content.${partIndex}.metadata`, 'metadata', remaining, {
              labelKey: 'blocks.metadata', collapsed: true,
            }))
          }
        } else if (part != null) {
          blocks.push(block(`${prefix}.content.${partIndex}`, role, part, {
            labelKey: roleKey, collapsed: true,
          }))
        }
      })
    } else if (content != null) {
      blocks.push(block(`${prefix}.content`, role, content, { labelKey: roleKey }))
    }
    const metadata = Object.fromEntries(Object.entries(item)
      .filter(([key, entry]) => key !== 'content' && entry != null))
    // Roles remain visible even when a message only contains tool calls.
    if (Object.keys(metadata).some(key => key !== 'role') || !blocks.length) {
      blocks.push(block(`${prefix}.metadata`, role, metadata, {
        labelKey: roleKey, collapsed: blocks.length > 0,
      }))
    }
    return blocks
  })
}

function lineText(value: unknown): string | undefined {
  if (!Array.isArray(value)) return undefined
  if (value.every(line => typeof line === 'string')) return value.join('\n')
  if (!value.every(line => {
    const item = record(line)
    return item && typeof item.text === 'string'
      && Object.keys(item).every(key => ['text', 'number', 'line', 'line_number'].includes(key))
  })) return undefined
  return value.map(line => {
    const item = line as RecordValue
    const number = item.number ?? item.line_number ?? item.line
    return number == null ? item.text : `${number}\t${item.text}`
  }).join('\n')
}

function fieldBlocks(value: unknown, id: string, context: BlockContext): InspectorBlock[] {
  if (value == null || (context.side === 'output' && value === '')) return []
  const decoded = structured(value)
  const fields = record(decoded)
  if (!fields) {
    if (Array.isArray(decoded)) {
      return decoded.flatMap((item, index) => fieldBlocks(item, `${id}.${index}`, context))
    }
    return [block(id, context.side, decoded, {
      labelKey: `blocks.${context.side}`,
      format: typeof decoded === 'string' ? 'code' : 'value',
      language: languageFor(context.path) || 'text',
    })]
  }
  if (typeof fields.kind === 'string' && 'payload' in fields) {
    const metadata = Object.fromEntries(Object.entries(fields).filter(([key]) => key !== 'payload'))
    return [
      ...fieldBlocks(fields.payload, `${id}.payload`, context),
      block(`${id}.metadata`, 'metadata', metadata, { labelKey: 'blocks.metadata', collapsed: true }),
    ]
  }
  const path = pathFrom(fields) || context.path
  const nestedContext = { ...context, path }
  const blocks: InspectorBlock[] = []
  const consumed = new Set<string>()
  const oldKey = ['old_text', 'oldText', 'old_string', 'old_str', 'old']
    .find(key => typeof fields[key] === 'string')
  const newKey = ['new_text', 'newText', 'new_string', 'new_str', 'new']
    .find(key => typeof fields[key] === 'string')
  if (oldKey && newKey) {
    blocks.push(block(`${id}.changes`, 'changes', fields[oldKey], {
      labelKey: 'blocks.changes', format: 'diff', secondaryValue: fields[newKey],
      language: languageFor(path),
    }))
    consumed.add(oldKey)
    consumed.add(newKey)
  }
  for (const [key, entry] of Object.entries(fields)) {
    if (consumed.has(key) || entry == null) continue
    const fieldId = `${id}.${key}`
    if (key === 'arguments' || key === 'result' || key === 'result_json') {
      const inner = structured(entry)
      if (record(inner) || Array.isArray(inner)) {
        blocks.push(...fieldBlocks(inner, fieldId, nestedContext))
      } else {
        blocks.push(block(fieldId, key, inner, {
          labelKey: key === 'arguments' ? 'blocks.arguments' : 'blocks.result',
          format: typeof inner === 'string' ? 'code' : 'value',
          language: languageFor(path) || 'text',
        }))
      }
    } else if (key === 'messages' && Array.isArray(entry)) {
      blocks.push(...messageBlocks(entry, fieldId))
    } else if (key === 'config' && record(entry)) {
      blocks.push(...fieldBlocks(entry, fieldId, nestedContext).map(item => ({
        ...item, label: `config.${item.label}`,
      })))
    } else if ((key === 'results' || key === 'matches') && Array.isArray(entry)) {
      entry.forEach((item, index) => {
        if (item == null) return
        const details = record(item)
        const title = [details?.title, details?.name, details?.path]
          .find((value): value is string => typeof value === 'string' && value.length > 0)
        blocks.push(block(`${fieldId}.${index}`, title || `${key} ${index + 1}`, item, {
          labelKey: 'blocks.results',
          children: fieldBlocks(item, `${fieldId}.${index}`, nestedContext),
        }))
      })
      if (!entry.length) blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.results' }))
    } else if ((key === 'edits' || key === 'diffs') && Array.isArray(entry)) {
      entry.forEach((item, index) => blocks.push(...fieldBlocks(item, `${fieldId}.${index}`, nestedContext)))
      if (!entry.length) blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.changes' }))
    } else if (key === 'tool_calls' && Array.isArray(entry)) {
      entry.forEach((item, index) => {
        if (item == null) return
        const details = record(item)
        const name = [details?.name, details?.tool_name, record(details?.function)?.name]
          .find((value): value is string => typeof value === 'string' && value.length > 0)
        blocks.push(block(`${fieldId}.${index}`, name || `tool ${index + 1}`, item, {
          children: fieldBlocks(item, `${fieldId}.${index}`, nestedContext),
        }))
      })
      if (!entry.length) blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.toolCalls', collapsed: true }))
    } else if (key === 'function' && context.row.phase === 'model_execution' && record(entry)) {
      blocks.push(...fieldBlocks(entry, fieldId, nestedContext))
    } else if (key === 'lines' && /read/.test(context.row.toolName || '') && lineText(entry) != null) {
      blocks.push(block(fieldId, key, lineText(entry), {
        labelKey: 'blocks.content', format: 'code', language: languageFor(path) || 'text',
      }))
    } else if (['path', 'file_path', 'filePath', 'filename'].includes(key)) {
      blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.path' }))
    } else if (['command', 'cmd'].includes(key)) {
      blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.command', format: 'code', language: 'bash' }))
    } else if (['stdout', 'stderr'].includes(key)) {
      if (entry !== '') blocks.push(block(fieldId, key, entry, {
        labelKey: `blocks.${key}`, format: 'code', language: 'text',
      }))
    } else if (['exit_code', 'exitCode', 'returncode'].includes(key)) {
      blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.exitCode' }))
    } else if (['diff', 'patch'].includes(key) && typeof entry === 'string') {
      blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.changes', format: 'code', language: 'diff' }))
    } else if (['content', 'file_text', 'arguments_text'].includes(key) && typeof entry === 'string') {
      blocks.push(block(fieldId, key, entry, {
        labelKey: key === 'arguments_text' ? 'blocks.arguments' : 'blocks.content',
        format: 'code', language: key === 'arguments_text' ? 'json' : languageFor(path) || 'text',
      }))
    } else if (['text', 'final_text', 'reasoning_content', 'reasoning', 'message', 'prompt'].includes(key)
      && typeof entry === 'string') {
      if (entry === '') continue
      const reasoning = key === 'reasoning' || key === 'reasoning_content'
      const isModel = context.row.phase === 'model_execution' || context.row.phase === 'finalize'
        || context.row.phase === 'intake' || context.row.phase === 'context'
      blocks.push(block(fieldId, key, entry, {
        labelKey: reasoning ? 'blocks.reasoning' : key === 'message' ? 'blocks.message' : 'blocks.text',
        format: isModel ? 'markdown' : 'code', collapsed: reasoning,
      }))
    } else if (key === 'error' && context.row.status === 'error') {
      blocks.push(block(fieldId, key, entry, { labelKey: 'blocks.error', collapsed: false }))
    } else {
      const labels: Record<string, string> = {
        tools: 'tools', usage: 'usage', artifact: 'artifact', artifacts: 'artifact',
        metadata: 'metadata', attachments: 'attachments', error: 'error', stage: 'stage', partial: 'partial',
        reason: 'reason', fallback_reason: 'reason', requested_mode: 'requestedMode', effective_mode: 'effectiveMode',
        previous_model: 'previousModel', next_model: 'nextModel', selected_model: 'selectedModel',
        candidates: 'candidates', decision: 'decision', policy_trail: 'decision',
      }
      const importantScalar = [
        'name', 'title', 'query', 'pattern', 'snippet', 'url', 'description', 'cwd', 'workdir',
        'stage', 'temperature', 'max_tokens', 'thinking', 'reasoning_effort', 'model', 'provider',
        'limit', 'offset',
        'reason', 'fallback_reason', 'requested_mode', 'effective_mode', 'previous_model',
        'next_model', 'selected_model', 'requested_model', 'tier', 'decision_id',
      ].includes(key)
      blocks.push(block(fieldId, key, entry, {
        labelKey: labels[key] ? `blocks.${labels[key]}` : undefined,
        collapsed: !importantScalar || record(entry) != null || Array.isArray(entry),
      }))
    }
  }
  return blocks
}

/** Derive display blocks without mutating records, truncating text, or opening resources. */
export function traceInspectorModel(row: TraceSpan, input: unknown, output: unknown): TraceInspectorModel {
  const category = traceEventCategory(row)
  const summary = category === 'input' ? 'intake' : category === 'output' || category === 'result' ? 'finalize' : category
  const inputEnvelope = record(structured(input))
  const inputRecord = typeof inputEnvelope?.kind === 'string' && 'payload' in inputEnvelope
    ? record(structured(inputEnvelope.payload)) : inputEnvelope
  const outputEnvelope = record(structured(output))
  const outputRecord = typeof outputEnvelope?.kind === 'string' && 'payload' in outputEnvelope
    ? record(structured(outputEnvelope.payload)) : outputEnvelope
  const result = record(structured(outputRecord?.result)) || outputRecord
  const usage = record(outputRecord?.usage) || row.usage
  const args = record(structured(inputRecord?.arguments)) || inputRecord
  const path = args ? pathFrom(args) : undefined
  const facts: TraceInspectorModel['facts'] = []
  const decision = { ...row.attrs, ...inputRecord, ...outputRecord }
  for (const [key, value] of Object.entries({
    provider: row.provider, model: row.model, tool: row.toolName, path,
    role: row.role, attempt: row.attemptIndex, call_id: row.logicalCallId,
    command: args?.command ?? args?.cmd,
    partial: outputRecord?.partial,
    exit_code: result?.exit_code ?? result?.exitCode ?? result?.returncode,
    result_count: Array.isArray(result?.results) ? result.results.length : undefined,
    message_count: Array.isArray(inputRecord?.messages) ? inputRecord.messages.length : undefined,
    tool_count: Array.isArray(inputRecord?.tools) ? inputRecord.tools.length : undefined,
    attachment_count: Array.isArray(inputRecord?.attachments) ? inputRecord.attachments.length : undefined,
    input_tokens: usage?.input_tokens, output_tokens: usage?.output_tokens,
    reasoning_tokens: usage?.reasoning_tokens, total_tokens: usage?.total_tokens,
    stop_reason: outputRecord?.stop_reason ?? usage?.stop_reason,
    parent_span: row.parentId,
  })) {
    if (value != null && value !== '') facts.push({ key, value })
  }
  if (['routing', 'approval', 'maintenance', 'subagent', 'retry', 'fallback'].includes(category)) {
    for (const key of [
      'requested_mode', 'effective_mode', 'requested_model', 'selected_model',
      'previous_model', 'next_model', 'reason', 'fallback_reason', 'decision_id',
      'tier', 'confidence', 'parent_run_id', 'child_run_id', 'approval_id',
      'candidate_index', 'candidate_count', 'attempt',
    ]) {
      const value = decision[key]
      if (value != null && value !== '' && !facts.some(fact => fact.key === key)) facts.push({ key, value })
    }
  }
  return {
    category,
    summaryKey: `summaries.${summary}`,
    facts,
    inputs: fieldBlocks(input, 'input', { row, path, side: 'input' }),
    outputs: fieldBlocks(output, 'output', { row, path, side: 'output' }),
  }
}
