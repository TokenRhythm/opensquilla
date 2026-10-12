import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'
import { isLegacyDocumentTool } from './legacyDocumentTool'
import { isGenericTool, isProcessTool, processToolTarget } from './toolDisplay'
import { normalizeToolFileSnapshot } from './toolFileSnapshot'

export type ActivityToolDetailLine =
  | { kind: 'target' | 'code' | 'error'; text: string }
  | { kind: 'bytes'; bytes: number }
  | { kind: 'content-size'; lines: number; characters: number }
  | { kind: 'exit-code'; code: number }
  | { kind: 'published' }
  | {
      kind: 'document-category'
      category: 'DOCUMENT_PREVIEW_UNAVAILABLE'
        | 'DOCUMENT_ACTION_RESULT_UNKNOWN'
        | 'DOCUMENT_EDIT_FAILED'
    }
  | {
      kind: 'document-message'
      messageKey: 'document.previewUnavailable'
        | 'document.actionResultUnknown'
        | 'document.editFailed'
    }
  | { kind: 'document-retry'; policy: 'same_turn' | 'new_turn' | 'never' }
  | {
      kind: 'document-next-action'
      action: 'retry' | 'reinspect' | 'finalize_without_tools' | 'start_new_turn' | 'stop'
    }

export interface ActivityToolCodeView {
  kind: 'read' | 'write' | 'edit' | 'command'
  path?: string
  language?: string
  content?: string
  command?: string
  output?: string
  unavailable?: boolean
  partial?: boolean
  truncated?: boolean
  error?: boolean
  exitCode?: number
}

export interface ActivityToolDetailProjection {
  lines: ActivityToolDetailLine[]
  rawContent: string
  rawSection?: ToolResultContext['section']
  detailMode?: 'changes' | 'parameters' | 'result'
  codeView?: ActivityToolCodeView
}

export type ActivityToolTarget =
  | { kind: 'path'; text: string }
  | { kind: 'url'; text: string; url: string }

const INLINE_TEXT_LIMIT = 140
const SENSITIVE_KEY_NAME = [
  '(?:',
  'api[_-]?key',
  '|access[_-]?key',
  '|private[_-]?key',
  '|signing[_-]?key',
  '|client[_-]?secret',
  '|token',
  '|password',
  '|passwd',
  '|secret',
  '|auth(?:orization)?',
  '|cookie',
  '|webhook',
  '|credential',
  ')',
].join('')
const SENSITIVE_KEY = `(?:[a-z0-9]+[_-])*${SENSITIVE_KEY_NAME}`
const SENSITIVE_KEY_SUFFIX = new RegExp(`(?:^|[_-])${SENSITIVE_KEY_NAME}$`, 'i')

function redactAssignments(source: string): string {
  // Visit each maximal key once. Searching again at every hyphen in a long
  // ordinary value makes the prefix matcher quadratic before it reaches '='.
  const keys = /(?<![a-z0-9_-])([a-z0-9_-]+)[ \t]*[:=][ \t]*/gi
  const value = /(?!(?:bearer|basic)\b)(?!\[redacted\])(?:"[^"]*"|'[^']*'|[^\s,;"'\}\]]+)/iy
  const parts: string[] = []
  let cursor = 0
  for (let key = keys.exec(source); key; key = keys.exec(source)) {
    if (!SENSITIVE_KEY_SUFFIX.test(key[1]!)) continue
    value.lastIndex = keys.lastIndex
    const secret = value.exec(source)
    if (!secret) continue
    parts.push(source.slice(cursor, secret.index), '[redacted]')
    cursor = value.lastIndex
    keys.lastIndex = cursor
  }
  return parts.length ? parts.join('') + source.slice(cursor) : source
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function parseRecord(raw: string | undefined): Record<string, unknown> | null {
  const source = String(raw || '').trim()
  if (!source) return null
  try {
    return asRecord(JSON.parse(source))
  } catch {
    return null
  }
}

function recordString(
  record: Record<string, unknown> | null,
  keys: string[],
): string {
  if (!record) return ''
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function truncateInline(value: string, limit = INLINE_TEXT_LIMIT): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (normalized.length <= limit) return normalized
  return `${normalized.slice(0, Math.max(1, limit - 1)).trimEnd()}…`
}

export function redactActivityDetail(value: string): string {
  const redacted = String(value || '')
    .replace(
      /(?<![a-z0-9+.-])([a-z0-9+.-]+:\/\/)([^/\s:@]+):([^@/\s]+)@/gi,
      (match, scheme: string) => /[a-z]/i.test(scheme) ? `${scheme}[redacted]@` : match,
    )
    .replace(
      /(authorization[ \t]*:[ \t]*bearer[ \t]+)[^\s"',;]+/gi,
      '$1[redacted]',
    )
    .replace(
      /\b(bearer[ \t]+)[a-z0-9._~+/=-]{8,}/gi,
      '$1[redacted]',
    )
    .replace(
      new RegExp(
        `(^|[\\s"'=])((?:--?|\\/)${SENSITIVE_KEY})(?:[ \\t]+|=)(?!\\[redacted\\])(?:"[^"]*"|'[^']*'|[^\\s,;"'\\}\\]]+)`,
        'gim',
      ),
      '$1$2 [redacted]',
    )
    .replace(
      new RegExp(`("(?:${SENSITIVE_KEY})"\\s*:\\s*)"(?:[^"\\\\]|\\\\.)*"`, 'gi'),
      '$1"[redacted]"',
    )
  return redactAssignments(redacted)
    .replace(
      new RegExp(`([?&](?:${SENSITIVE_KEY})=)[^&#\\s]+`, 'gi'),
      '$1[redacted]',
    )
    // Known-token sweep. Browser-side defense in depth only: the durable
    // boundary is the server-side pass (src/opensquilla/redaction.py), whose
    // known-secrets exact replacement and entropy heuristic a browser
    // projection cannot have.
    .replace(
      new RegExp(
        [
          '\\b(?:',
          'sk-[a-z0-9_-]{8,}',
          '|sk_(?:live|test|proj)_[a-z0-9_]{8,}',
          '|gh[pousr]_[a-z0-9_]{12,}',
          '|xox[baprs]-[a-z0-9-]{12,}',
          '|AKIA[A-Z0-9]{12,}',
          '|eyJ[a-z0-9_-]{8,}\\.[a-z0-9_-]{8,}\\.[a-z0-9_-]{8,}',
          ')\\b',
        ].join(''),
        'gi',
      ),
      '[redacted]',
    )
}

export function activityDisplayPath(value: string): string {
  const source = String(value || '').trim()
  if (!source) return ''
  const normalized = source.replace(/\\/g, '/')
  const workspaceMarker = '/workspace/'
  const workspaceIndex = normalized.toLowerCase().lastIndexOf(workspaceMarker)
  if (workspaceIndex >= 0) {
    const workspaceRelative = normalized.slice(
      workspaceIndex + workspaceMarker.length,
    )
    if (
      workspaceRelative
      && !workspaceRelative.split('/').includes('..')
      && !workspaceRelative.includes('://')
      && !workspaceRelative.startsWith('/')
    ) {
      return truncateInline(workspaceRelative, 96)
    }
  }

  const relative = normalized.replace(/^\.\//, '')
  // `~`, `$HOME`, and `%USERPROFILE%` are absolute paths in disguise: letting
  // them into the relative branch would print the out-of-workspace structure
  // this function exists to hide.
  const isAbsolute = relative.startsWith('/')
    || /^[A-Za-z]:\//.test(relative)
    || /^(?:~|\$home\b|%userprofile%)/i.test(relative)
  const hasParentTraversal = relative.split('/').includes('..')
  if (!isAbsolute && !hasParentTraversal && !relative.includes('://')) {
    return truncateInline(relative, 96)
  }

  const pathOnly = normalized.split(/[?#]/, 1)[0] || ''
  const basename = pathOnly.split('/').filter(part => part && part !== '..').pop()
  return basename ? `…/${safeInline(basename).slice(0, 88)}` : ''
}

function safeUrl(value: string): string {
  const source = String(value || '').trim()
  if (!source) return ''
  try {
    const parsed = new URL(source)
    if (parsed.protocol === 'file:') {
      return activityDisplayPath(parsed.pathname)
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return ''
    }
    return truncateInline(`${parsed.host}${parsed.pathname}`, 112)
  } catch {
    const pathShaped = source.includes('/') || source.includes('\\')
    return pathShaped
      ? activityDisplayPath(source)
      : truncateInline(redactActivityDetail(source), 112)
  }
}

function activityBrowserUrl(value: unknown): string {
  if (typeof value !== 'string') return ''
  const source = value.trim()
  if (!source) return ''
  try {
    const parsed = new URL(source)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return ''
    parsed.username = ''
    parsed.password = ''
    return parsed.toString()
  } catch {
    return ''
  }
}

function activityUrlLabel(value: string): string {
  try {
    const parsed = new URL(value)
    return truncateInline(`${parsed.protocol}//${parsed.host}${parsed.pathname}`, 112)
  } catch {
    return safeUrl(value)
  }
}

function safeInline(value: string): string {
  return truncateInline(redactActivityDetail(value))
}

const NON_EXPANDABLE_READ_OPERATIONS = new Set([
  'memory.search',
  'web.discover',
  'web.read',
  'web.search',
])
const NON_APPLIED_MUTATION_STATUSES = new Set([
  'approval_required',
  'approval_pending',
  'elevation_required',
  'blocked',
  'cancelled',
  'denied',
  'error',
  'failed',
])

function safeTarget(value: string): string {
  const source = String(value || '').trim()
  if (!source) return ''
  const normalized = source.replace(/\\/g, '/')
  if (normalized.includes('://')) return safeUrl(source)
  if (
    normalized.includes('/')
    || normalized.startsWith('.')
    || /^[A-Za-z]:/.test(normalized)
  ) {
    return activityDisplayPath(source)
  }
  return safeInline(source)
}

function safeError(value: string, allowRawRecord = true): string {
  const source = String(value || '')
  const record = parseRecord(source)
  const preferred = recordString(record, [
    'user_message',
    'userMessage',
    'message',
    'error_message',
    'errorMessage',
    'detail',
    'error',
    'stderr',
  ]) || ['error', 'cause', 'details']
    .map(key => recordString(asRecord(record?.[key]), [
      'user_message',
      'userMessage',
      'message',
      'error_message',
      'errorMessage',
      'detail',
    ]))
    .find(Boolean)
    || (record && !allowRawRecord ? '' : source)
  const firstLine = preferred.split(/\r?\n/, 1)[0] || ''
  const withoutAbsolutePaths = firstLine.replace(
    /(?:[A-Za-z]:[\\/]|\/)(?:[^<>:"|?*\s]+[\\/])+([^<>:"|?*\s]+)/g,
    '…/$1',
  )
  return safeInline(withoutAbsolutePaths)
}

function looksLikeStructuredError(value: string): boolean {
  // Also catches truncated object/array histories that JSON.parse cannot read.
  // Bracketed log levels such as [error] remain ordinary readable text.
  return /^(?:\{|\[\s*(?:[\[{\]"0-9-]|true\b|false\b|null\b|$))/.test(value.trim())
}

/** A compact failure remains readable when its diagnostic body is collapsed. */
export function projectActivityToolErrorSummary(call: ChatToolCallRenderItem): string {
  if (!call.isError && call.status !== 'error') return ''
  // Legacy document errors have a separate safe, localized projection. Their
  // diagnostic payload can contain whole source documents rather than a hint.
  if (isLegacyDocumentTool(call.name)) return ''
  const source = String(call.result || call.resultPreview || '')
  if (!parseRecord(source) && looksLikeStructuredError(source)) return ''
  const summary = safeError(source, false)
  return looksLikeStructuredError(summary) ? '' : summary
}

function activityExitCode(value: string): number | null {
  const source = String(value || '')
  const record = parseRecord(source)
  const structured = record?.exit_code ?? record?.exitCode
  if (
    typeof structured === 'number'
    && Number.isSafeInteger(structured)
  ) {
    return structured
  }
  if (
    typeof structured === 'string'
    && /^-?\d+$/.test(structured.trim())
  ) {
    const parsed = Number(structured)
    return Number.isSafeInteger(parsed) ? parsed : null
  }
  const match = /(?:^|\r?\n)\s*exit_code\s*=\s*(-?\d+)\b/i.exec(source)
  if (!match) return null
  const parsed = Number(match[1])
  return Number.isSafeInteger(parsed) ? parsed : null
}

function contentSize(value: string): { lines: number; characters: number } | null {
  const source = String(value || '')
  if (!source) return null
  const normalized = source.replace(/\r\n/g, '\n').replace(/\n$/, '')
  if (normalized.length <= 80 && !normalized.includes('\n')) return null
  return {
    lines: normalized ? normalized.split('\n').length : 0,
    characters: source.length,
  }
}

function rawDetails(
  call: ChatToolCallRenderItem,
): Pick<ActivityToolDetailProjection, 'rawContent' | 'rawSection'> {
  const input = String(call.inputRaw || call.inputPreview || '').trim()
  const result = String(call.result || call.resultPreview || '').trim()
  const sections = []
  if (input) sections.push(`INPUT\n${input}`)
  if (result) sections.push(`${call.isError ? 'ERROR' : 'RESULT'}\n${result}`)
  const rawSection = input && result
    ? (call.isError || call.status === 'error' ? 'error' : undefined)
    : result
      ? (call.isError || call.status === 'error' ? 'error' : 'result')
      : input
        ? 'input'
        : undefined
  return {
    rawContent: redactActivityDetail(sections.join('\n\n')),
    rawSection,
  }
}

function stringField(
  record: Record<string, unknown> | null,
  keys: string[],
): string | undefined {
  if (!record) return undefined
  for (const key of keys) {
    if (typeof record[key] === 'string') return record[key] as string
  }
  return undefined
}

function parsedArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  if (typeof value !== 'string') return []
  try {
    const parsed: unknown = JSON.parse(value)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

/** Tool semantics are independent of whether this history retained its payload. */
export function activityToolCodeKind(
  call: ChatToolCallRenderItem,
  operationKey: string,
): ActivityToolCodeView['kind'] | undefined {
  if (isLegacyDocumentTool(call.name) || isLegacyDocumentTool(operationKey)) return undefined
  if (operationKey === 'file.inspect') return 'read'
  if (operationKey === 'file.write') return 'write'
  if (operationKey === 'file.edit') return 'edit'
  if (operationKey === 'command.run') return 'command'
  return undefined
}

// These are durable projection envelopes from session_sanitize.py and
// projected_arguments.py, not source bytes. Their head/tail snippets cannot be
// treated as a complete historical file or an executable command.
export function isOmittedActivityPayload(value: string | undefined): boolean {
  if (value === undefined) return false
  return /^\s*\[(?:historical_tool_argument_omitted|tool_use_argument_projection|historical_tool_result(?:_projection_body)?_compacted|tool_result_projection|aggregate_tool_result_compacted)\](?:\r?\n|$)/.test(value)
    || /^\s*\[(?:invalid_provider_context_projection|provider_request_tool_input_compacted):/.test(value)
    || /^\s*\[provider_request_[a-z0-9_]*(?:compacted|omitted):[^\]\n]*(?:\d+ chars|original_chars=\d+)[^\]\n]*\](?:\r?\n|$)/.test(value)
    || /^\s*\[provider_request_(?:image_url|base64_image|base64_media)_omitted\]\s*$/.test(value)
    || /^\s*\[opensquilla_compacted:[A-Za-z0-9_.-]+:\d+:[0-9a-f]{8,64}\](?:\r?\n|$)/.test(value)
}

const isOmittedPayload = isOmittedActivityPayload

function isProjectedInput(record: Record<string, unknown> | null): boolean {
  return ['_opensquilla_compacted_tool_arguments', '_opensquilla_compacted_tool_input', '_invalid_provider_context_arguments']
    .some(key => record?.[key] === true || /^(true|1|yes|on)$/i.test(String(record?.[key] ?? '').trim()))
}

/** A retained empty file is different from an absent historical payload. */
export function hasActivityToolCodeContent(view: ActivityToolCodeView): boolean {
  const body = view.kind === 'command' || view.error ? view.output : view.content
  const readableBody = typeof body === 'string' && !isOmittedPayload(body)
  if (view.kind === 'command') {
    return Boolean(readableBody && body || view.command && !isOmittedPayload(view.command))
  }
  if (view.kind === 'edit') return Boolean(readableBody && body?.trim())
  return readableBody && (Boolean(body) || !view.unavailable && !view.error)
}

export function activityToolCodeContent(view: ActivityToolCodeView): string {
  const body = view.kind === 'command' || view.error ? view.output : view.content
  const content = isOmittedPayload(body) ? '' : body || ''
  if (view.kind !== 'command') return content
  const command = isOmittedPayload(view.command) ? '' : view.command || ''
  const exit = view.exitCode !== undefined && !/^\s*exit[_ ]code\s*[:=]/im.test(content)
    ? `exit_code=${view.exitCode}` : ''
  return [command ? `$ ${command}` : '', content, exit].filter(Boolean).join('\n\n')
}

function partialPayload(value: string, record: Record<string, unknown> | null): boolean {
  return record?.truncated === true || record?.result_truncated === true
    || /\[(?:diff_summary_truncated|line truncated):\s*omitted_chars=\d+\]/.test(value)
}

function partialCallResult(call: ChatToolCallRenderItem, result: string): boolean {
  const record = asRecord(call)
  const originalChars = record?.resultOriginalChars ?? record?.result_original_chars
  return record?.resultTruncated === true || record?.result_truncated === true
    || (typeof originalChars === 'number' && originalChars > result.length)
    || (!call.result && Boolean(call.resultPreview))
}

function codeLanguage(path: string): string {
  const extension = path.split('.').pop()?.toLowerCase() || ''
  return ({ js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
    py: 'python', html: 'html', htm: 'html', vue: 'html', css: 'css', scss: 'scss',
    json: 'json', md: 'markdown', yaml: 'yaml', yml: 'yaml', sh: 'bash', ps1: 'powershell',
    bat: 'dos', cmd: 'dos', sql: 'sql', xml: 'xml', rs: 'rust', go: 'go' } as Record<string, string>)[extension] || 'text'
}

function fileCodeView(
  kind: 'read' | 'write' | 'edit',
  inputRecord: Record<string, unknown> | null,
  resultRecord: Record<string, unknown> | null,
): ActivityToolCodeView {
  const path = redactActivityDetail(activityDisplayPath(
    recordString(inputRecord, ['path', 'file', 'file_path', 'filePath', 'target'])
      || recordString(resultRecord, ['path', 'file_path']),
  ))
  return { kind, path: path || undefined, language: kind === 'edit' ? 'diff' : codeLanguage(path) }
}

function callFailed(call: ChatToolCallRenderItem, resultRecord: Record<string, unknown> | null): boolean {
  return Boolean(call.isError || call.status === 'error' || resultRecord?.ok === false
    || NON_APPLIED_MUTATION_STATUSES.has(recordString(resultRecord, ['status', 'state']).toLowerCase()))
}

function readCodeDetails(
  call: ChatToolCallRenderItem,
  inputRecord: Record<string, unknown> | null,
  resultRecord: Record<string, unknown> | null,
): ActivityToolDetailProjection {
  const view = fileCodeView('read', inputRecord, resultRecord)
  const result = String(call.result || call.resultPreview || '')
  if (callFailed(call, resultRecord)) {
    const error = safeError(result)
    return {
      lines: error ? [{ kind: 'error', text: error }] : [], rawContent: '',
      codeView: { ...view, error: true, output: error, content: '' },
    }
  }
  let content = result
  // read_source returns a revision receipt containing numbered line records.
  // Other file reads may return a JSON file with the same field names. Its
  // values are source bytes, not an instruction to unwrap a protocol receipt.
  const isSourceRead = String(call.name || '').trim().toLowerCase().split(/\.|\/|:|__/).pop() === 'read_source'
  const sourceReceipt = isSourceRead && Array.isArray(resultRecord?.lines)
    && typeof resultRecord?.revision === 'string' ? resultRecord : null
  if (sourceReceipt) {
    content = (sourceReceipt.lines as unknown[]).map(value => stringField(asRecord(value), ['text']) ?? '').join('\n')
  }
  const recordedEmpty = call.resultOriginalChars === 0
    || (sourceReceipt && (sourceReceipt.lines as unknown[]).length === 0 && sourceReceipt.total_lines === 0)
  const unavailable = isOmittedPayload(content) || (!content && !recordedEmpty)
  const range = sourceReceipt?.range
  const partial = partialPayload(result, resultRecord) || partialCallResult(call, result)
    || (Array.isArray(range) && typeof sourceReceipt?.total_lines === 'number'
      && (Number(range[0]) > 1 || Number(range[1]) < sourceReceipt.total_lines))
    || Number(inputRecord?.offset || 0) > 1 || Number(inputRecord?.limit || 0) > 0
  return {
    lines: [], ...rawDetails(call), detailMode: 'result',
    codeView: { ...view, content: unavailable ? '' : redactActivityDetail(content),
      ...(unavailable ? { unavailable: true } : {}), ...(partial ? { partial: true } : {}) },
  }
}

function commandCodeView(
  call: ChatToolCallRenderItem,
  inputRecord: Record<string, unknown> | null,
  resultRecord: Record<string, unknown> | null,
): ActivityToolCodeView {
  const command = stringField(inputRecord, ['command', 'cmd', 'script'])
  const result = String(call.result || call.resultPreview || '')
  let output = result
  if (resultRecord) {
    const stdout = stringField(resultRecord, ['stdout'])
    const stderr = stringField(resultRecord, ['stderr'])
    const structuredOutput = stringField(resultRecord, ['output'])
    const exitCode = activityExitCode(result)
    output = structuredOutput
      ?? (stdout !== undefined || stderr !== undefined ? [stdout, stderr].filter(Boolean).join('\n')
        : exitCode !== null ? '' : result)
    // Structured execution failures can carry multiline diagnostics outside
    // stdout/stderr. Keep the retained error rather than reducing it to the
    // compact row's first-line/140-character summary.
    if (!output && (callFailed(call, resultRecord) || (exitCode ?? 0) !== 0)) output = result
  }
  const omittedCommand = isProjectedInput(inputRecord) || isOmittedPayload(command)
  const omittedOutput = isOmittedPayload(output)
  const unavailable = (!command && !output) || omittedCommand || omittedOutput
  const exitCode = activityExitCode(result)
  return {
    kind: 'command', language: 'text',
    command: omittedCommand ? '' : redactActivityDetail(command || ''),
    output: omittedOutput ? '' : redactActivityDetail(output),
    ...(unavailable ? { unavailable: true } : {}),
    ...(partialPayload(output, resultRecord) || partialCallResult(call, result) ? { partial: true } : {}),
    ...(callFailed(call, resultRecord) || (activityExitCode(result) ?? 0) !== 0 ? { error: true } : {}),
    ...(exitCode !== null ? { exitCode } : {}),
  }
}

function replacementDiff(before: string, after: string, index?: number): string {
  const suffix = index === undefined ? '' : ` ${index + 1}`
  const removed = before
    ? before.split(/\r\n|\r|\n/).map(line => `-${line}`).join('\n')
    : ''
  const added = after
    ? after.split(/\r\n|\r|\n/).map(line => `+${line}`).join('\n')
    : ''
  return [`--- before${suffix}`, `+++ after${suffix}`, '@@', removed, added].filter(Boolean).join('\n')
}

function fileMutationDetails(
  call: ChatToolCallRenderItem,
  inputRecord: Record<string, unknown> | null,
  resultRecord: Record<string, unknown> | null,
  kind: 'write' | 'edit',
): Pick<ActivityToolDetailProjection, 'rawContent' | 'rawSection' | 'detailMode' | 'codeView'> {
  const view = fileCodeView(kind, inputRecord, resultRecord)
  const result = String(call.result || call.resultPreview || '').trim()
  if (call.isError || call.status === 'error' || resultRecord?.ok === false) {
    return {
      rawContent: redactActivityDetail(result),
      rawSection: 'error',
      detailMode: 'result',
      codeView: { ...view, error: true, output: redactActivityDetail(result), content: '' },
    }
  }

  const resultStatus = recordString(resultRecord, ['status', 'state']).toLowerCase()
  if (NON_APPLIED_MUTATION_STATUSES.has(resultStatus)) {
    return {
      rawContent: redactActivityDetail(result),
      rawSection: 'result',
      detailMode: 'result',
      codeView: { ...view, error: true, output: redactActivityDetail(result), content: '' },
    }
  }

  // A successful tool's recorded display snapshot survives compacted model
  // arguments and takes precedence over inferred changes or result previews.
  const snapshot = normalizeToolFileSnapshot(call.fileSnapshot)
  if (snapshot && !call.isRunning && call.status === 'success') {
    const path = redactActivityDetail(activityDisplayPath(snapshot.path))
    const unavailable = Boolean(snapshot.sha256_ref || snapshot.legacy_ref || snapshot.content_unavailable)
    const content = unavailable ? '' : redactActivityDetail(snapshot.content)
    return {
      rawContent: content,
      rawSection: 'result',
      detailMode: snapshot.kind === 'edit' ? 'changes' : 'result',
      codeView: { kind: snapshot.kind, path, content, ...(unavailable ? { unavailable: true } : {}),
        language: snapshot.kind === 'edit' ? 'diff' : codeLanguage(path) },
    }
  }

  // Revision-based source tools already return the authoritative file diff.
  // Prefer it over reconstructing a view from their invocation contract.
  const resultDiff = stringField(resultRecord, ['diff_summary', 'diffSummary'])
  if (resultDiff !== undefined && !isOmittedPayload(resultDiff)) {
    return {
      rawContent: redactActivityDetail(resultDiff),
      rawSection: 'result',
      detailMode: 'changes',
      codeView: { ...view, kind: 'edit', language: 'diff', content: redactActivityDetail(resultDiff),
        ...(partialPayload(resultDiff, resultRecord) || partialCallResult(call, result) ? { partial: true } : {}) },
    }
  }

  if (isProjectedInput(inputRecord)) {
    return { ...rawDetails(call), detailMode: 'result', codeView: { ...view, unavailable: true } }
  }

  // apply_patch's patch argument is itself the most accurate available change
  // representation. A path-only patch cannot be expanded without reading a
  // second file in the browser, so it falls back to the execution result.
  const patch = stringField(inputRecord, ['patch'])
  if (patch !== undefined && patch.trim() && !isOmittedPayload(patch)) {
    return {
      rawContent: redactActivityDetail(patch),
      rawSection: 'input',
      detailMode: 'changes',
      codeView: { ...view, kind: 'edit', language: 'diff', content: redactActivityDetail(patch) },
    }
  }

  const replacements: Array<{ before: string; after: string }> = []
  const oldText = stringField(inputRecord, ['old_text', 'oldText', 'old_string', 'oldString'])
  const newText = stringField(inputRecord, ['new_text', 'newText', 'new_string', 'newString'])
  let incompleteReplacements = isOmittedPayload(oldText) || isOmittedPayload(newText)
  if (oldText !== undefined && newText !== undefined && !isOmittedPayload(oldText) && !isOmittedPayload(newText)) {
    replacements.push({ before: oldText, after: newText })
  }
  for (const value of parsedArray(inputRecord?.edits)) {
    const edit = asRecord(value)
    const before = stringField(edit, ['old_text', 'oldText', 'old_string', 'oldString'])
    const after = stringField(edit, ['new_text', 'newText', 'new_string', 'newString'])
    incompleteReplacements ||= before === undefined || after === undefined || isOmittedPayload(before) || isOmittedPayload(after)
    if (before !== undefined && after !== undefined && !isOmittedPayload(before) && !isOmittedPayload(after)) {
      replacements.push({ before, after })
    }
  }
  if (replacements.length && !incompleteReplacements) {
    const diff = redactActivityDetail(replacements.map((replacement, index) => replacementDiff(
      replacement.before, replacement.after, replacements.length > 1 ? index : undefined,
    )).join('\n\n'))
    return {
      rawContent: diff,
      rawSection: 'input',
      detailMode: 'changes',
      codeView: { ...view, kind: 'edit', language: 'diff', content: diff },
    }
  }

  // Full-file writers expose the resulting content but not the previous file
  // bytes. Say that explicitly instead of fabricating a before/after diff.
  const content = stringField(inputRecord, ['content'])
  if (content !== undefined && !isOmittedPayload(content)) {
    return {
      rawContent: redactActivityDetail(content),
      rawSection: 'input',
      detailMode: 'result',
      codeView: { ...view, kind: 'write', language: codeLanguage(view.path || ''), content: redactActivityDetail(content) },
    }
  }

  return {
    ...rawDetails(call),
    detailMode: 'result',
    codeView: { ...view, content: '', unavailable: true },
  }
}

function pushUnique(
  lines: ActivityToolDetailLine[],
  line: ActivityToolDetailLine | null,
) {
  if (!line) return
  const key = JSON.stringify(line)
  const duplicate = lines.some(item => (
    JSON.stringify(item)
  ) === key)
  if (!duplicate) lines.push(line)
}

function targetValues(record: Record<string, unknown> | null, keys: string[]): unknown[] {
  if (!record) return []
  return keys.flatMap((key) => {
    const value = record[key]
    return Array.isArray(value) ? value : value == null ? [] : [value]
  })
}

function sourceRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map(asRecord).filter((record): record is Record<string, unknown> => record !== null)
    : []
}

/**
 * Project only the resource identity for read-only activity. These targets are
 * deliberately separate from the invocation/result detail projection: paths
 * are plain text, while HTTP(S) URLs are the only actionable target kind.
 */
/** A compact, factual target; never use a raw JSON result as the row label. */
export function projectActivityToolSummary(call: ChatToolCallRenderItem, operationKey: string): string {
  if (isLegacyDocumentTool(call.name)) return ''
  if (isGenericTool(call.name)) return call.name || ''
  if (isProcessTool(call.name)) return redactActivityDetail(processToolTarget(call))
  const input = parseRecord(redactActivityDetail(call.inputRaw || call.inputPreview || ''))
  if (!input) return ''
  if (operationKey === 'command.run') {
    const command = recordString(input, ['command', 'cmd', 'script'])
    return isProjectedInput(input) || isOmittedPayload(command) ? '' : truncateInline(command, 110)
  }
  if (operationKey.startsWith('file.')) {
    const paths = targetValues(input, ['paths', 'path', 'file_path', 'filePath', 'file', 'target'])
      .filter((value): value is string => typeof value === 'string')
      .map(activityDisplayPath).filter(Boolean)
    return [...new Set(paths)].slice(0, 2).join(' · ')
  }
  if (operationKey === 'web.search' || operationKey === 'web.discover' || operationKey === 'memory.search') {
    return safeInline(recordString(input, ['query', 'q', 'search', 'text']))
  }
  if (operationKey === 'web.read') return safeUrl(recordString(input, ['url', 'uri', 'href']))
  const names = ['name', 'title', 'tool', 'skill', 'operation', 'action']
  return safeInline(recordString(input, names))
}

export function projectActivityToolTargets(
  call: ChatToolCallRenderItem,
  operationKey: string,
): ActivityToolTarget[] {
  if (isLegacyDocumentTool(call.name) || isLegacyDocumentTool(operationKey)) return []
  const input = parseRecord(call.inputRaw || call.inputPreview)
  const targets: ActivityToolTarget[] = []
  const seen = new Set<string>()
  const pushPath = (value: unknown) => {
    if (typeof value !== 'string') return
    const text = activityDisplayPath(value)
    if (!text || seen.has(`path:${text}`)) return
    seen.add(`path:${text}`)
    targets.push({ kind: 'path', text })
  }
  const pushUrl = (value: unknown) => {
    const url = activityBrowserUrl(value)
    if (!url) return
    const key = url.split('#', 1)[0] || url
    if (seen.has(`url:${key}`)) return
    seen.add(`url:${key}`)
    targets.push({ kind: 'url', text: activityUrlLabel(url), url })
  }

  if (call.presentation?.category === 'file_read' || operationKey === 'file.inspect') {
    const declared = call.presentation?.primaryArguments.filter(key => /(?:^|_)(?:path|paths|workdir)$/.test(key))
    const keys = declared?.length ? declared : ['paths', 'path', 'file_path', 'workdir']
    for (const value of targetValues(input, keys)) pushPath(value)
    return targets.slice(0, 12)
  }

  if (call.presentation?.category === 'network_read' || operationKey === 'web.read') {
    const declared = call.presentation?.primaryArguments.filter(key => key === 'url' || key === 'urls')
    const keys = declared?.length ? declared : ['urls', 'url']
    for (const value of targetValues(input, keys)) pushUrl(value)
    return targets.slice(0, 12)
  }

  if (operationKey === 'web.search' || operationKey === 'web.discover') {
    const result = parseRecord(call.result || call.resultPreview)
    const records = [
      ...sourceRecords(call.sources),
      ...sourceRecords(result?.sources),
      ...sourceRecords(result?.results),
    ]
    for (const record of records) {
      pushUrl(record.url ?? record.final_url ?? record.canonical_url)
      if (targets.length >= 12) break
    }
  }

  return targets.slice(0, 12)
}

export function projectActivityToolDetail(
  call: ChatToolCallRenderItem,
  operationKey: string,
): ActivityToolDetailProjection {
  // Persisted results from retired tools can contain source and old grants.
  // Keep their read-only projection safe after removing active tool routing.
  if (isLegacyDocumentTool(call.name) || isLegacyDocumentTool(operationKey)) {
    const resultRecord = parseRecord(call.result || call.resultPreview)
    const nestedError = asRecord(resultRecord?.error)
    const category = recordString(resultRecord, ['category', 'error_category', 'errorCategory', 'code'])
      || recordString(nestedError, ['category', 'code'])
    const recordedStatus = recordString(resultRecord, ['status', 'state']).toLowerCase()
    const recordedFailure = Boolean(
      call.isError
      || call.status === 'error'
      || resultRecord?.ok === false
      || nestedError
      || ['error', 'failed', 'failure'].includes(recordedStatus)
      || category,
    )
    // Compatibility history can lose the transient call error flags while
    // retaining the structured bridge result.  Derive failure only from
    // stable structured fields; successful document calls remain completely
    // non-disclosable and never enter a raw/result/copy path.
    if (!recordedFailure) return { lines: [], rawContent: '' }
    const messageKey = recordString(resultRecord, ['message_key', 'messageKey'])
      || recordString(nestedError, ['message_key', 'messageKey'])
    const retryPolicy = recordString(resultRecord, ['retry_policy', 'retryPolicy'])
      || recordString(nestedError, ['retry_policy', 'retryPolicy'])
    const nextAction = recordString(resultRecord, ['next_action', 'nextAction'])
      || recordString(nestedError, ['next_action', 'nextAction'])
    const allowedCategory = new Set([
      'DOCUMENT_PREVIEW_UNAVAILABLE',
      'DOCUMENT_ACTION_RESULT_UNKNOWN',
      'DOCUMENT_EDIT_FAILED',
    ]).has(category)
      ? category as 'DOCUMENT_PREVIEW_UNAVAILABLE'
        | 'DOCUMENT_ACTION_RESULT_UNKNOWN'
        | 'DOCUMENT_EDIT_FAILED'
      : 'DOCUMENT_EDIT_FAILED'
    const allowedMessageKey = new Set([
      'document.previewUnavailable',
      'document.actionResultUnknown',
      'document.editFailed',
    ]).has(messageKey)
      ? messageKey as 'document.previewUnavailable'
        | 'document.actionResultUnknown'
        | 'document.editFailed'
      : 'document.editFailed'
    const allowedRetry = new Set(['same_turn', 'new_turn', 'never']).has(retryPolicy)
      ? retryPolicy as 'same_turn' | 'new_turn' | 'never'
      : 'never'
    const allowedAction = new Set([
      'retry', 'reinspect', 'finalize_without_tools', 'start_new_turn', 'stop',
    ]).has(nextAction)
      ? nextAction as 'retry'
        | 'reinspect'
        | 'finalize_without_tools'
        | 'start_new_turn'
        | 'stop'
      : 'stop'
    const lines: ActivityToolDetailLine[] = [
      { kind: 'document-category', category: allowedCategory },
      { kind: 'document-message', messageKey: allowedMessageKey },
    ]
    lines.push({ kind: 'document-retry', policy: allowedRetry })
    lines.push({ kind: 'document-next-action', action: allowedAction })
    // Document details never enter raw/full-result/copy paths. Only the
    // stable allowlisted projection above can reach the DOM.
    return { lines, rawContent: '' }
  }
  // Search/network calls disclose their primary target in the tool row. They do
  // not get a second expandable INPUT/RESULT surface: that would repeat the
  // query/path/URL and expose provider diagnostics or fetched content.
  if (
    (call.presentation?.argumentDisplay === 'primary' && !activityToolCodeKind(call, operationKey))
    || NON_EXPANDABLE_READ_OPERATIONS.has(operationKey)
  ) {
    const error = call.isError || call.status === 'error'
      ? safeError(call.result || call.resultPreview)
      : ''
    return {
      lines: error ? [{ kind: 'error', text: error }] : [],
      rawContent: '',
    }
  }
  const inputRecord = parseRecord(call.inputRaw || call.inputPreview)
  const resultRecord = parseRecord(call.result || call.resultPreview)
  if (operationKey === 'file.inspect') return readCodeDetails(call, inputRecord, resultRecord)
  const lines: ActivityToolDetailLine[] = []

  if (operationKey.startsWith('file.')) {
    const path = recordString(inputRecord, [
      'path',
      'file',
      'file_path',
      'filePath',
      'target',
    ])
    const displayPath = activityDisplayPath(path)
    pushUnique(lines, displayPath ? { kind: 'target', text: displayPath } : null)
  } else if (operationKey === 'artifact.create') {
    const name = recordString(inputRecord, ['name'])
    const path = recordString(inputRecord, ['path', 'file', 'file_path', 'filePath'])
    const displayTarget = safeTarget(name) || activityDisplayPath(path)
    pushUnique(lines, displayTarget ? { kind: 'target', text: displayTarget } : null)
  } else if (
    operationKey === 'web.search'
    || operationKey === 'web.discover'
    || operationKey === 'memory.search'
  ) {
    const query = recordString(inputRecord, ['query', 'q', 'search', 'text'])
    const safeQuery = safeInline(query)
    pushUnique(lines, safeQuery ? { kind: 'target', text: `“${safeQuery}”` } : null)
  } else if (operationKey === 'web.read') {
    const url = recordString(inputRecord, ['url', 'uri', 'href'])
    const displayUrl = safeUrl(url)
    pushUnique(lines, displayUrl ? { kind: 'target', text: displayUrl } : null)
  } else if (operationKey === 'command.run' || operationKey === 'code.python') {
    // Command arguments can contain credentials in forms that a browser-only
    // projection cannot classify exhaustively. Keep the raw command behind the
    // explicit detail viewer; the compact activity surface only reports safe
    // result metadata below.
  } else {
    const target = recordString(inputRecord, ['name', 'title', 'tool', 'skill'])
    const displayTarget = safeTarget(target)
    pushUnique(lines, displayTarget ? { kind: 'target', text: displayTarget } : null)
  }

  const result = String(call.result || call.resultPreview || '')
  const writtenMatch = (
    operationKey === 'file.write' || operationKey === 'file.edit'
  )
    ? /\bwritten\s+(\d+)\s+bytes?\b/i.exec(result)
    : null
  if (call.isError || call.status === 'error') {
    const errorSource = call.result || call.resultPreview
    const exitCode = activityExitCode(errorSource)
    if (exitCode !== null) {
      pushUnique(lines, { kind: 'exit-code', code: exitCode })
    } else {
      const error = safeError(errorSource)
      pushUnique(lines, error ? { kind: 'error', text: error } : null)
    }
  } else if (writtenMatch) {
    pushUnique(lines, { kind: 'bytes', bytes: Number(writtenMatch[1]) })
  } else if (
    operationKey === 'artifact.create'
    && String(resultRecord?.status || '').toLowerCase() === 'published'
  ) {
    pushUnique(lines, { kind: 'published' })
  } else if (operationKey.startsWith('tool.')) {
    // Output size is the only compact signal generic `tool.*` operations (MCP
    // tools, plan or messaging builtins) reliably have. Read-shaped tools have
    // no expandable detail surface and return before reaching this branch.
    const size = contentSize(result)
    if (size) pushUnique(lines, { kind: 'content-size', ...size })
  }

  const detail = operationKey === 'file.write' || operationKey === 'file.edit'
    ? fileMutationDetails(call, inputRecord, resultRecord, operationKey === 'file.write' ? 'write' : 'edit')
    : operationKey === 'command.run'
        || operationKey === 'code.python'
        || call.presentation?.argumentDisplay === 'all'
      ? { ...rawDetails(call), detailMode: 'parameters' as const }
      : rawDetails(call)

  return {
    lines: lines.slice(0, 3),
    ...detail,
    ...(operationKey === 'command.run' ? { codeView: commandCodeView(call, inputRecord, resultRecord) } : {}),
  }
}

export function hasActivityToolDetail(
  call: ChatToolCallRenderItem,
  operationKey: string,
): boolean {
  const projection = projectActivityToolDetail(call, operationKey)
  if (projection.codeView) return hasActivityToolCodeContent(projection.codeView)
    || canReadActivityFileSnapshot(call, operationKey)
    || canReadActivityExecutionLog(call, operationKey)
  return projection.lines.length > 0 || Boolean(projection.rawContent) || canReadActivityExecutionLog(call, operationKey)
}

/** This is a saved body, never a request to re-read the working file. */
export function canReadActivityFileSnapshot(call: ChatToolCallRenderItem, operationKey: string): boolean {
  if (operationKey !== 'file.write' && operationKey !== 'file.edit') return false
  const snapshot = normalizeToolFileSnapshot(call.fileSnapshot)
  return Boolean((snapshot?.sha256_ref || snapshot?.legacy_ref) && !snapshot.content_unavailable && !call.isRunning
    && call.status === 'success' && !call.isError && !projectActivityToolDetail(call, operationKey).codeView?.error)
}

/** A log handle is attached to complete results too; it is not evidence of missing output. */
export function canReadActivityExecutionLog(call: ChatToolCallRenderItem, operationKey: string): boolean {
  const supported = Boolean(call.executionLogHandle?.trim()
    && !isLegacyDocumentTool(call.name) && !isLegacyDocumentTool(operationKey)
    && (operationKey === 'command.run' || operationKey === 'code.python' || operationKey.startsWith('tool.')))
  if (!supported) return false

  const result = String(call.result || call.resultPreview || '')
  const callRecord = asRecord(call)
  const originalChars = callRecord?.resultOriginalChars ?? callRecord?.result_original_chars
  if (callRecord?.resultTruncated === true || callRecord?.result_truncated === true
    || (typeof originalChars === 'number' && originalChars > Array.from(result).length)) return true
  // An explicitly recorded empty result is complete. Old records without a
  // retained body can still recover one through the saved handle.
  if (!result.length) return originalChars !== 0
  if (isOmittedPayload(result)) return true

  const record = parseRecord(result)
  if (['truncated', 'result_truncated', 'output_truncated', 'stdout_truncated', 'stderr_truncated',
    'resultTruncated', 'outputTruncated', 'stdoutTruncated', 'stderrTruncated'].some(key => record?.[key] === true)) return true
  const output = stringField(record, ['output'])
  const streams = ['output', 'stdout', 'stderr'].map(key => stringField(record, [key]))
  if (streams.some(isOmittedPayload)) return true
  if (typeof record?.total_chars === 'number' && output !== undefined
    && record.total_chars > Array.from(output).length) return true
  // Without truncation evidence, keep the already readable historical result
  // and its copy action independent of an optional, possibly expired log.
  return false
}
