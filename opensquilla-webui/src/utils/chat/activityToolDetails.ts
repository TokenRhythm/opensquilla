import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'

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

export interface ActivityToolDetailProjection {
  lines: ActivityToolDetailLine[]
  rawContent: string
  rawSection?: ToolResultContext['section']
  detailMode?: 'changes' | 'parameters' | 'result'
}

export type ActivityToolTarget =
  | { kind: 'path'; text: string }
  | { kind: 'url'; text: string; url: string }

const INLINE_TEXT_LIMIT = 140
const SENSITIVE_KEY = [
  '(?:[a-z0-9]+[_-])*',
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
  return String(value || '')
    .replace(
      /([a-z][a-z0-9+.-]*:\/\/)([^/\s:@]+):([^@/\s]+)@/gi,
      '$1[redacted]@',
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
    .replace(
      new RegExp(
        `(^|[^a-z0-9])(${SENSITIVE_KEY}[ \\t]*[:=][ \\t]*)(?!(?:bearer|basic)\\b)(?!\\[redacted\\])(?:"[^"]*"|'[^']*'|[^\\s,;"'\\}\\]]+)`,
        'gim',
      ),
      '$1$2[redacted]',
    )
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
  'file.inspect',
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

function safeError(value: string): string {
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
    || source
  const firstLine = preferred.split(/\r?\n/, 1)[0] || ''
  const withoutAbsolutePaths = firstLine.replace(
    /(?:[A-Za-z]:[\\/]|\/)(?:[^<>:"|?*\s]+[\\/])+([^<>:"|?*\s]+)/g,
    '…/$1',
  )
  return safeInline(withoutAbsolutePaths)
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

function replacementDiff(before: string, after: string, index?: number): string {
  const suffix = index === undefined ? '' : ` ${index + 1}`
  const removed = before
    ? before.split(/\r\n|\r|\n/).map(line => `-${line}`).join('\n')
    : '-(empty)'
  const added = after
    ? after.split(/\r\n|\r|\n/).map(line => `+${line}`).join('\n')
    : '+(empty)'
  return `--- before${suffix}\n+++ after${suffix}\n@@\n${removed}\n${added}`
}

function fileMutationDetails(
  call: ChatToolCallRenderItem,
  inputRecord: Record<string, unknown> | null,
  resultRecord: Record<string, unknown> | null,
): Pick<ActivityToolDetailProjection, 'rawContent' | 'rawSection' | 'detailMode'> {
  const result = String(call.result || call.resultPreview || '').trim()
  if (call.isError || call.status === 'error') {
    return {
      rawContent: redactActivityDetail(result),
      rawSection: 'error',
      detailMode: 'result',
    }
  }

  const resultStatus = recordString(resultRecord, ['status', 'state']).toLowerCase()
  if (NON_APPLIED_MUTATION_STATUSES.has(resultStatus)) {
    return {
      rawContent: redactActivityDetail(result),
      rawSection: 'result',
      detailMode: 'result',
    }
  }

  // Revision-based source tools already return the authoritative file diff.
  // Prefer it over reconstructing a view from their invocation contract.
  const resultDiff = stringField(resultRecord, ['diff_summary', 'diffSummary'])
  if (resultDiff !== undefined) {
    return {
      rawContent: redactActivityDetail(resultDiff),
      rawSection: 'result',
      detailMode: 'changes',
    }
  }

  // apply_patch's patch argument is itself the most accurate available change
  // representation. A path-only patch cannot be expanded without reading a
  // second file in the browser, so it falls back to the execution result.
  const patch = stringField(inputRecord, ['patch'])
  if (patch !== undefined && patch.trim()) {
    return {
      rawContent: redactActivityDetail(patch),
      rawSection: 'input',
      detailMode: 'changes',
    }
  }

  const replacements: Array<{ before: string; after: string }> = []
  const oldText = stringField(inputRecord, ['old_text', 'oldText', 'old_string', 'oldString'])
  const newText = stringField(inputRecord, ['new_text', 'newText', 'new_string', 'newString'])
  if (oldText !== undefined && newText !== undefined) {
    replacements.push({ before: oldText, after: newText })
  }
  for (const value of parsedArray(inputRecord?.edits)) {
    const edit = asRecord(value)
    const before = stringField(edit, ['old_text', 'oldText', 'old_string', 'oldString'])
    const after = stringField(edit, ['new_text', 'newText', 'new_string', 'newString'])
    if (before !== undefined && after !== undefined) {
      replacements.push({ before, after })
    }
  }
  if (replacements.length) {
    return {
      rawContent: redactActivityDetail(replacements
        .map((replacement, index) => replacementDiff(
          replacement.before,
          replacement.after,
          replacements.length > 1 ? index : undefined,
        ))
        .join('\n\n')),
      rawSection: 'input',
      detailMode: 'changes',
    }
  }

  // Full-file writers expose the resulting content but not the previous file
  // bytes. Say that explicitly instead of fabricating a before/after diff.
  const content = stringField(inputRecord, ['content'])
  if (content !== undefined) {
    const added = content
      ? content.split(/\r\n|\r|\n/).map(line => `+${line}`).join('\n')
      : '+(empty file)'
    return {
      rawContent: redactActivityDetail(
        `--- previous content unavailable\n+++ resulting content\n@@\n${added}`,
      ),
      rawSection: 'input',
      detailMode: 'changes',
    }
  }

  return {
    rawContent: redactActivityDetail(result),
    rawSection: result ? 'result' : undefined,
    detailMode: result ? 'result' : undefined,
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
export function projectActivityToolTargets(
  call: ChatToolCallRenderItem,
  operationKey: string,
): ActivityToolTarget[] {
  if (operationKey === 'document.read' || operationKey === 'document.update') return []
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
  // Page editing tools carry source excerpts, revision hashes, cursors and
  // one-time grants. Those belong in diagnostics, not in the ordinary chat
  // activity disclosure.
  if (operationKey === 'document.read' || operationKey === 'document.update') {
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
  // Search/read calls disclose their primary target in the tool row. They do
  // not get a second expandable INPUT/RESULT surface: that would repeat the
  // query/path/URL and expose provider diagnostics or fetched content.
  if (
    call.presentation?.argumentDisplay === 'primary'
    || NON_EXPANDABLE_READ_OPERATIONS.has(operationKey)
  ) {
    return { lines: [], rawContent: '' }
  }
  const inputRecord = parseRecord(call.inputRaw || call.inputPreview)
  const resultRecord = parseRecord(call.result || call.resultPreview)
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
    ? fileMutationDetails(call, inputRecord, resultRecord)
    : operationKey === 'command.run'
        || operationKey === 'code.python'
        || call.presentation?.argumentDisplay === 'all'
      ? { ...rawDetails(call), detailMode: 'parameters' as const }
      : rawDetails(call)

  return {
    lines: lines.slice(0, 3),
    ...detail,
  }
}

export function hasActivityToolDetail(
  call: ChatToolCallRenderItem,
  operationKey: string,
): boolean {
  const projection = projectActivityToolDetail(call, operationKey)
  return projection.lines.length > 0 || Boolean(projection.rawContent)
}
