import type { ChatToolCallRenderItem, ToolResultContext } from '@/types/chat'

export type ActivityToolDetailLine =
  | { kind: 'target' | 'code' | 'error'; text: string }
  | { kind: 'bytes'; bytes: number }
  | { kind: 'content-size'; lines: number; characters: number }
  | { kind: 'exit-code'; code: number }
  | { kind: 'published' }

export interface ActivityToolDetailProjection {
  lines: ActivityToolDetailLine[]
  rawContent: string
  rawSection?: ToolResultContext['section']
}

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
        `((?:--?|\\/)${SENSITIVE_KEY})(?:[ \\t]+|=)(?:"[^"]*"|'[^']*'|[^\\s,;]+)`,
        'gi',
      ),
      '$1 [redacted]',
    )
    .replace(
      new RegExp(`("(?:${SENSITIVE_KEY})"\\s*:\\s*)"(?:[^"\\\\]|\\\\.)*"`, 'gi'),
      '$1"[redacted]"',
    )
    .replace(
      new RegExp(
        `(^|[^a-z0-9])(${SENSITIVE_KEY}[ \\t]*[:=][ \\t]*)(?!(?:bearer|basic)\\b)(?:"[^"]*"|'[^']*'|[^\\s,;]+)`,
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

function safeInline(value: string): string {
  return truncateInline(redactActivityDetail(value))
}

const CONTENT_SIZE_OPERATIONS = new Set(['file.inspect', 'web.read'])

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
    rawContent: sections.join('\n\n'),
    rawSection,
  }
}

function pushUnique(
  lines: ActivityToolDetailLine[],
  line: ActivityToolDetailLine | null,
) {
  if (!line) return
  const key = 'text' in line
    ? `${line.kind}:${line.text}`
    : JSON.stringify(line)
  const duplicate = lines.some(item => (
    'text' in item
      ? `${item.kind}:${item.text}`
      : JSON.stringify(item)
  ) === key)
  if (!duplicate) lines.push(line)
}

export function projectActivityToolDetail(
  call: ChatToolCallRenderItem,
  operationKey: string,
): ActivityToolDetailProjection {
  // Page editing tools carry source excerpts, revision hashes, cursors and
  // one-time grants. Those belong in diagnostics, not in the ordinary chat
  // activity disclosure.
  if (operationKey === 'document.read' || operationKey === 'document.update') {
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
  } else if (
    CONTENT_SIZE_OPERATIONS.has(operationKey)
    || operationKey.startsWith('tool.')
  ) {
    // Output size is meaningful audit signal for read-shaped operations, and
    // it is the only signal generic `tool.*` operations (MCP tools, plan or
    // messaging builtins) reliably have — their inputs rarely carry a
    // name-like key. Search, command, and write ops stay quiet on purpose.
    const size = contentSize(result)
    if (size) pushUnique(lines, { kind: 'content-size', ...size })
  }

  return {
    lines: lines.slice(0, 3),
    ...rawDetails(call),
  }
}
