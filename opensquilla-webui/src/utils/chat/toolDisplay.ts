import i18n from '@/i18n'
import { isLegacyDocumentTool } from './legacyDocumentTool'
import type {
  ChatToolCall,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolPresentation,
} from '@/types/chat'
import type { IconName } from '@/utils/icons'

function truncateToolText(text: string, max = 200): string {
  if (!text || text.length <= max) return text || ''
  return text.slice(0, max) + '…'
}

function parseToolInput(input: unknown): Record<string, unknown> | null {
  if (typeof input !== 'string') {
    return input && typeof input === 'object' ? input as Record<string, unknown> : null
  }
  try {
    const parsed = JSON.parse(input)
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

export function isEmptyToolPreview(text: string): boolean {
  const value = String(text || '').trim()
  return !value || value === '""' || value === "''" || value === '{}' || value === '[]'
}

export function truncateToolPreview(text: string, max = 200): string {
  return truncateToolText(text, max)
}

export function normalizeToolName(raw: unknown): string {
  const record = asRecord(raw)
  const fn = asRecord(record?.function)
  const value = record?.name ?? record?.tool_name ?? record?.toolName ?? fn?.name
  const name = typeof value === 'string' ? value.trim() : ''
  return name && name !== 'tool' ? name : ''
}

export function isInternalToolName(name: string): boolean {
  return name === 'router_control'
}

export function activityToolName(name: string): string {
  const normalized = String(name || '')
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .replace(/-/g, '_')
  const namespaced = normalized.split(/__|[.:/]/).filter(Boolean)
  return namespaced[namespaced.length - 1] || ''
}

// Successful progress calls may follow an answer without moving it back into
// Activity. Retain the retired checkpoint name for historical PlanRun turns.
const ANSWER_TRANSPARENT_CONTROL_TOOLS = new Set(['plan_run_checkpoint', 'update_plan'])

export function isAnswerTransparentControlTool(name: string): boolean {
  return ANSWER_TRANSPARENT_CONTROL_TOOLS.has(activityToolName(name))
}

export function normalizeToolInputText(raw: unknown): string {
  const record = asRecord(raw)
  const value = record?.input ?? record?.arguments ?? ''
  if (value == null) return ''
  if (typeof value === 'string') {
    const text = value.trim()
    return isEmptyToolPreview(text) ? '' : text
  }
  if (Array.isArray(value) && value.length === 0) return ''
  if (typeof value === 'object' && Object.keys(value).length === 0) return ''
  const text = JSON.stringify(value, null, 2)
  return isEmptyToolPreview(text) ? '' : text
}

const TOOL_PRESENTATION_CATEGORIES = new Set<ToolPresentation['category']>([
  'search',
  'file_read',
  'network_read',
  'command',
  'subagent',
  'mutation',
  'generic',
])
const RESTRICTED_TOOL_PRESENTATION: ToolPresentation = {
  category: 'generic',
  primaryArguments: [],
  argumentDisplay: 'primary',
  lifecycleDisplay: 'boundary',
}
const TOOL_PRESENTATION_STATUS_KEYS = {
  search: {
    running: 'chat.tool.searchRunning',
    done: 'chat.tool.searchDone',
    failed: 'chat.tool.searchFailed',
  },
  file_read: {
    running: 'chat.tool.fileReadRunning',
    done: 'chat.tool.fileReadDone',
    failed: 'chat.tool.fileReadFailed',
  },
  network_read: {
    running: 'chat.tool.networkReadRunning',
    done: 'chat.tool.networkReadDone',
    failed: 'chat.tool.networkReadFailed',
  },
  command: {
    running: 'chat.tool.commandRunning',
    done: 'chat.tool.commandDone',
    failed: 'chat.tool.commandFailed',
  },
  subagent: {
    running: 'chat.tool.subagentRunning',
    done: 'chat.tool.subagentDone',
    failed: 'chat.tool.subagentFailed',
  },
  mutation: {
    running: 'chat.tool.mutationRunning',
    done: 'chat.tool.mutationDone',
    failed: 'chat.tool.mutationFailed',
  },
  generic: {
    running: 'chat.tool.genericRunning',
    done: 'chat.tool.genericDone',
    failed: 'chat.tool.genericFailed',
  },
} as const
const TOOL_MUTATION_STATUS_KEYS = {
  'file.write': {
    running: 'chat.tool.fileWriteRunning',
    done: 'chat.tool.fileWriteDone',
    failed: 'chat.tool.fileWriteFailed',
  },
  'file.edit': {
    running: 'chat.tool.fileEditRunning',
    done: 'chat.tool.fileEditDone',
    failed: 'chat.tool.fileEditFailed',
  },
} as const

export function normalizeToolPresentation(raw: unknown): ToolPresentation | undefined {
  const record = asRecord(raw)
  if (!record || !Object.prototype.hasOwnProperty.call(record, 'tool_presentation')) return undefined
  const value = asRecord(record.tool_presentation)
  if (!value) return RESTRICTED_TOOL_PRESENTATION
  const category = value.category
  const primaryArguments = value.primaryArguments
  const argumentDisplay = value.argumentDisplay
  const lifecycleDisplay = value.lifecycleDisplay
  if (
    typeof category !== 'string'
    || !TOOL_PRESENTATION_CATEGORIES.has(category as ToolPresentation['category'])
    || !Array.isArray(primaryArguments)
    || !primaryArguments.every(key => typeof key === 'string')
    || !['primary', 'all'].includes(String(argumentDisplay))
    || !['boundary', 'default'].includes(String(lifecycleDisplay))
  ) return RESTRICTED_TOOL_PRESENTATION
  return {
    category: category as ToolPresentation['category'],
    primaryArguments: [...primaryArguments],
    argumentDisplay: argumentDisplay as ToolPresentation['argumentDisplay'],
    lifecycleDisplay: lifecycleDisplay as ToolPresentation['lifecycleDisplay'],
  }
}

export function toolDisplayInputText(
  raw: unknown,
  presentation = normalizeToolPresentation(raw),
): string {
  const input = normalizeToolInputText(raw)
  if (!input || presentation?.argumentDisplay !== 'primary') return input
  const parsed = parseToolInput(input)
  if (!parsed) return ''
  const projected = Object.fromEntries(
    presentation.primaryArguments
      .filter(key => Object.prototype.hasOwnProperty.call(parsed, key))
      .map(key => [key, parsed[key]]),
  )
  return Object.keys(projected).length ? JSON.stringify(projected, null, 2) : ''
}

export function toolDisplayName(name: string, input: unknown): string {
  if (name === 'publish_artifact') {
    const inputObj = parseToolInput(input)
    const target = inputObj?.name || inputObj?.path
    if (typeof target === 'string' && target) {
      return `${name} - ${target.split(/[\\/]+/).filter(Boolean).pop() || target}`
    }
  }
  return name
}

// These names come from registered tools. Keep their presentation separate from
// the raw identity and grouping key; unrelated MCP tools keep their identifier.
const REGISTERED_TOOL_ACTIONS = new Map<string, { label: string; icon: IconName }>([
  ['mcp__desktop-browser__browser_open', { label: 'openBrowser', icon: 'monitor' }],
  ['open_workspace_preview', { label: 'openPreview', icon: 'monitor' }],
  ['skill_view', { label: 'readSkill', icon: 'skills' }],
])

export function toolIconName(name: string): IconName {
  const n = String(name || '').toLowerCase()
  const registeredAction = REGISTERED_TOOL_ACTIONS.get(n)
  if (registeredAction) return registeredAction.icon
  if (isProcessTool(n)) return 'terminal'
  if (isToolResultRetrieval(n)) return 'logs'
  if (n.includes('search') || n.includes('google') || n.includes('bing')) return 'search'
  if (n.includes('fetch') || n.includes('http') || n.includes('curl') || n.includes('wget')) return 'monitor'
  if (n.includes('python') || n === 'py' || n.includes('exec') || n.includes('bash') || n.includes('shell')) return 'config'
  if (n.includes('write') || n.includes('edit') || n.includes('patch')) return 'edit'
  if (n.includes('read') || n.includes('file') || n.includes('cat') || n.includes('list') || n === 'ls' || n.includes('glob') || n.includes('find')) return 'logs'
  if (n.includes('artifact') || n.includes('download')) return 'download'
  if (n.includes('memory')) return 'clock'
  return 'gear'
}

function hasToolNameSuffix(name: string, tool: string): boolean {
  return name === tool
    || name.endsWith(`.${tool}`)
    || name.endsWith(`/${tool}`)
    || name.endsWith(`:${tool}`)
    || name.endsWith(`__${tool}`)
}

function isToolResultRetrieval(name: string): boolean {
  return hasToolNameSuffix(String(name || '').trim().toLowerCase(), 'retrieve_tool_result')
}

export function isProcessTool(name: string): boolean {
  const normalized = String(name || '').toLowerCase()
  return ['process', 'background_process'].some(tool => hasToolNameSuffix(normalized, tool))
}

const PROCESS_ACTION_LABELS: Record<string, string> = {
  poll: 'checkBackgroundCommand',
  wait: 'waitBackgroundCommand',
  log: 'readBackgroundOutput',
  list: 'listBackgroundCommands',
  kill: 'stopBackgroundCommand',
  remove: 'removeBackgroundCommand',
  write: 'sendBackgroundInput',
  submit: 'submitBackgroundInput',
  eof: 'closeBackgroundInput',
  resize: 'resizeBackgroundTerminal',
}

/** Identify the managed command without repeating the action or its raw JSON. */
export function processToolTarget(toolCall: ChatToolCall): string {
  const input = parseToolInput(toolCall.inputRaw || toolCall.inputPreview)
  const result = parseToolInput(toolCall.result || toolCall.resultPreview)
  const session = asRecord(result?.session)
  const primary = toolCall.presentation?.argumentDisplay === 'primary'
    ? toolCall.presentation.primaryArguments : null
  const value = (record: Record<string, unknown> | null, key: string): string => {
    const field = record?.[key]
    return typeof field === 'string' ? field.trim() : ''
  }
  // A server-declared primary-only projection must remain primary-only.
  const command = !primary
    ? value(session, 'command') || value(result, 'command') || value(input, 'command')
    : primary.includes('command') ? value(input, 'command') : ''
  if (command) return truncateToolText(command.replace(/\s+/g, ' '), 110)
  for (const key of ['execution_id', 'session_id', 'sessionId']) {
    if (primary && !primary.includes(key)) continue
    const target = value(input, key) || (!primary ? value(result, key) || value(session, key) : '')
    if (target) return truncateToolText(target, 86)
  }
  if ((!primary || primary.includes('execution_ids')) && Array.isArray(input?.execution_ids)) {
    return truncateToolText(input.execution_ids.filter(id => typeof id === 'string').join(' · '), 86)
  }
  return ''
}

export function toolOperationKey(name: string): string {
  const n = String(name || '').toLowerCase()
  if (isToolResultRetrieval(n)) return 'tool.retrieve.tool.result'
  if (n.includes('web_discover')) return 'web.discover'
  if (n.includes('web_search') || n === 'search' || n.includes('google') || n.includes('bing')) return 'web.search'
  if (n.includes('web_fetch') || n.includes('http') || n.includes('fetch') || n.includes('curl') || n.includes('wget')) return 'web.read'
  if (n.includes('python') || n === 'py') return 'code.python'
  if (n.includes('bash') || n.includes('shell') || n.includes('exec')) return 'command.run'
  if (['create_file', 'create_source', 'write_scratch'].some(tool => hasToolNameSuffix(n, tool))) return 'file.write'
  if (['edit_file', 'edit_source', 'apply_patch'].some(tool => hasToolNameSuffix(n, tool))) return 'file.edit'
  if (n.includes('write')) return 'file.write'
  if (n.includes('edit') || n.includes('patch')) return 'file.edit'
  if (n.includes('read') || n.includes('cat') || n.includes('list') || n === 'ls' || n.includes('glob') || n.includes('find') || n.includes('file')) return 'file.inspect'
  if (n.includes('publish_artifact') || n.includes('artifact')) return 'artifact.create'
  if (n.includes('memory')) return 'memory.search'
  return `tool.${n.replace(/[^a-z0-9]+/g, '.') || 'unknown'}`
}

/** Unknown tools use a neutral translated action; their identifier stays verbatim. */
export function isGenericTool(name: string): boolean {
  const normalized = String(name || '').toLowerCase()
  return !REGISTERED_TOOL_ACTIONS.has(normalized)
    && !isProcessTool(name)
    && !isToolResultRetrieval(name)
    && !isLegacyDocumentTool(name)
    && toolOperationKey(name).startsWith('tool.')
}

export function toolActionLabel(name: string, input?: unknown): string {
  const key = toolOperationKey(name)
  const t = i18n.global.t
  const registeredAction = REGISTERED_TOOL_ACTIONS.get(String(name || '').toLowerCase())
  if (registeredAction) return t(`chat.tool.${registeredAction.label}`)
  if (isToolResultRetrieval(name)) return t('chat.tool.retrieveToolResult')
  if (isProcessTool(name)) {
    const action = parseToolInput(input)?.action
    const label = typeof action === 'string' ? PROCESS_ACTION_LABELS[action] : undefined
    return t(`chat.tool.${label || 'manageBackgroundCommand'}`)
  }
  if (key === 'web.discover') return t('chat.tool.discoverLinks')
  if (key === 'web.search') return t('chat.tool.searchWeb')
  if (key === 'web.read') return t('chat.tool.readWebPage')
  if (key === 'code.python') return t('chat.tool.runPython')
  if (key === 'command.run') return t('chat.tool.runCommand')
  if (key === 'file.inspect') return t('chat.tool.inspectFiles')
  if (key === 'file.write') return t('chat.tool.writeFile')
  if (key === 'file.edit') return t('chat.tool.editFile')
  if (key === 'artifact.create') return t('chat.tool.createFile')
  if (key === 'memory.search') return t('chat.tool.searchMemory')
  return t('chat.tool.genericCall')
}

export function toolSecondaryText(toolCall: ChatToolCall): string {
  if (isLegacyDocumentTool(toolCall.name)) return ''
  if (isGenericTool(toolCall.name)) return toolCall.name || ''
  if (isProcessTool(toolCall.name)) return processToolTarget(toolCall)
  // Retrieval handles and slice/query parameters do not identify the original
  // file or web page. Keep them in the invocation detail, not the row summary.
  if (isToolResultRetrieval(toolCall.name)) return ''
  const operation = toolOperationKey(toolCall.name)
  if (operation === 'web.search' || operation === 'web.discover') return ''
  if (toolCall.presentation?.argumentDisplay === 'primary') {
    const input = parseToolInput(toolCall.inputRaw || toolCall.inputPreview)
    const primary = toolCall.presentation.primaryArguments.flatMap((key) => {
      const value = input?.[key]
      if (Array.isArray(value)) return value.map(item => String(item)).filter(Boolean)
      return value == null ? [] : [String(value)]
    })
    return primary.length ? truncateToolText(primary.join(' · '), 86) : ''
  }
  const source = String(toolCall.inputPreview || toolCall.resultPreview || '').replace(/\s+/g, ' ').trim()
  if (isEmptyToolPreview(source)) return ''
  return truncateToolText(source.replace(/^"|"$/g, ''), 86)
}

export function summarizeToolGroup(calls: ChatToolCall[]): string {
  const running = calls.filter(toolCall => toolCall.isRunning).length
  const done = calls.filter(toolCall => toolCall.status === 'success').length
  const failed = calls.filter(toolCall => toolCall.status === 'error').length
  const sample = calls.map(toolCall => toolSecondaryText(toolCall)).find(Boolean)
  const t = i18n.global.t
  const parts = []
  if (running) parts.push(t('chat.tool.countRunning', { count: running }))
  if (done) parts.push(t('chat.tool.countDone', { count: done }))
  if (failed) parts.push(t('chat.tool.countFailed', { count: failed }))
  if (sample) parts.push(sample)
  return parts.join(' · ')
}

export function toolCallGroups(calls: ChatToolCall[] | undefined, ownerKey: string): ChatToolCallGroup[] {
  if (!calls?.length) return []
  const groups: ChatToolCallGroup[] = []

  calls.forEach((call, index) => {
    const operationKey = toolOperationKey(call.name)
    const renderKey = `${ownerKey}:tool:${call.toolId || call.name || index}:${index}`
    const last = groups[groups.length - 1]
    if (!last || last.operationKey !== operationKey || (call.groupId && last.groupId !== call.groupId)) {
      groups.push({
        groupId: call.groupId || `${ownerKey}:tool-group:${operationKey}:${groups.length}`,
        operationKey,
        label: toolActionLabel(call.name),
        iconName: toolIconName(call.name),
        calls: [],
        secondary: '',
        isRunning: false,
        isError: false,
        status: '',
      })
    }

    groups[groups.length - 1].calls.push({ ...call, renderKey } as ChatToolCallRenderItem)
  })

  groups.forEach(group => {
    if (group.calls.length === 1 && isProcessTool(group.calls[0].name)) {
      const call = group.calls[0]
      group.label = toolActionLabel(call.name, call.inputRaw || call.inputPreview)
    }
    group.isRunning = group.calls.some(tc => tc.isRunning)
    group.isError = group.calls.some(tc => tc.isError || tc.status === 'error')
    group.status = group.isError ? 'error' : (group.calls.every(tc => tc.status === 'success') ? 'success' : '')
    group.secondary = group.calls.length === 1
      ? toolSecondaryText(group.calls[0])
      : summarizeToolGroup(group.calls)
  })

  return groups
}

const PLAIN_TEXT_RESULT_TOOL_TOKENS = new Set([
  'discover',
  'search',
])
const RAW_TEXT_SEARCH_TOOLS = new Set([
  'glob_search',
  'grep_search',
])

function supportsPlainTextResultCount(toolName: string): boolean {
  const tokens = String(toolName || '')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
  if (RAW_TEXT_SEARCH_TOOLS.has(tokens.join('_'))) return false
  return tokens.some(token => PLAIN_TEXT_RESULT_TOOL_TOKENS.has(token))
}

function isLikelyYear(value: string): boolean {
  if (!/^\d{4}$/.test(value)) return false
  const year = Number(value)
  return year >= 1900 && year <= 2199
}

function plainTextResultCount(text: string): number | null {
  const lines = text
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
  if (!lines.length) return null
  const line = /^\[[^\]]+\]$/.test(lines[0]) && lines[1] ? lines[1] : lines[0]

  const explicitPatterns = [
    /^(?:(?:web\s+)?search\s+)?(?:found|returned|showing)\s*:?\s*(\d{1,4})\s+results?(?:\s+(?:for|matching)\b.*)?[.!]?$/i,
    /^showing\s+\d+\s*[-–]\s*\d+\s+of\s+(\d{1,4})\s+results?[.!]?$/i,
    /^(?:搜索\s*)?(?:共\s*)?(?:找到|返回|显示)\s*[:：]?\s*(\d{1,4})\s*(?:条|个)?\s*结果[。！.!]?$/,
  ]
  for (const pattern of explicitPatterns) {
    const match = pattern.exec(line)
    if (match) return Number(match[1])
  }

  const bareMatch = /^(?:about\s+)?(\d{1,4})\s*(?:results?|(?:条|个)?\s*结果)(?:\s+(?:found|returned|shown))?[.!。！:]?$/i.exec(line)
  if (!bareMatch || isLikelyYear(bareMatch[1])) return null
  return Number(bareMatch[1])
}

export function toolResultCount(raw: string, toolName: string): number | null {
  const text = String(raw || '').trim()
  if (!text) return null
  let summaryText = text
  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) return parsed.length
    for (const key of ['results', 'items', 'data', 'matches']) {
      if (Array.isArray(parsed?.[key])) return parsed[key].length
    }
    // Structured tool payloads often contain arbitrary page or command output.
    // Never infer a count from those nested strings: a phrase such as
    // "2026 results" may be a year plus a heading, not a result summary.
    if (parsed && typeof parsed === 'object') return null
    if (typeof parsed === 'string') summaryText = parsed.trim()
    else return null
  } catch {}
  // Preserve text summaries only for search operations, and only when the
  // first summary line has an explicit count shape. Content-producing tools
  // and arbitrary result bodies must not reinterpret years as metadata.
  if (!supportsPlainTextResultCount(toolName)) return null
  return plainTextResultCount(summaryText)
}

export function toolResultIsError(payload: unknown): boolean {
  const record = asRecord(payload)
  const status = asRecord(record?.execution_status ?? record?.executionStatus)
  if (typeof status?.status === 'string') {
    return ['error', 'timeout', 'cancelled'].includes(status.status)
  }
  return !!(record?.is_error || record?.isError || record?.error)
}

export function toolStatusText(toolCall: ChatToolCall): string {
  const t = i18n.global.t
  const presentationStatus = toolPresentationStatusText(
    toolCall,
    toolCall.isRunning ? 'running' : toolCall.status === 'error' ? 'failed' : toolCall.status === 'success' ? 'done' : null,
  )
  if (presentationStatus) return presentationStatus
  if (toolCall.isRunning) return t('chat.tool.running')
  if (toolCall.status === 'error') return t('chat.tool.failed')
  const count = toolResultCount(toolCall.result, toolCall.name)
  if (count !== null) return t('chat.tool.results', { count })
  if (toolCall.status === 'success') return t('chat.tool.done')
  return t('chat.tool.pending')
}

export function toolGroupStatusText(group: ChatToolCallGroup): string {
  const t = i18n.global.t
  const presentationCall = group.calls.find(call => call.presentation)
  const presentationStatus = presentationCall && toolPresentationStatusText(
    presentationCall,
    group.isRunning ? 'running' : group.isError ? 'failed' : group.status === 'success' ? 'done' : null,
  )
  if (presentationStatus) return presentationStatus
  if (group.isRunning) return t('chat.tool.running')
  if (group.isError) return t('chat.tool.failed')
  const counts = group.calls
    .map(toolCall => toolResultCount(toolCall.result, toolCall.name))
    .filter((count): count is number => count !== null)
  if (counts.length && group.calls.length === 1) return t('chat.tool.results', { count: counts[0] })
  if (counts.length) return t('chat.tool.results', { count: counts.reduce((sum, count) => sum + count, 0) })
  if (group.status === 'success') return t('chat.tool.done')
  return t('chat.tool.pending')
}

function toolPresentationStatusText(
  toolCall: ChatToolCall,
  state: 'running' | 'done' | 'failed' | null,
): string {
  if (!state || !toolCall.presentation) return ''
  if (toolCall.presentation.category === 'mutation') {
    const mutationKeys = TOOL_MUTATION_STATUS_KEYS[
      toolOperationKey(toolCall.name) as keyof typeof TOOL_MUTATION_STATUS_KEYS
    ]
    if (mutationKeys) return i18n.global.t(mutationKeys[state])
  }
  const keys = TOOL_PRESENTATION_STATUS_KEYS[
    toolCall.presentation.category as keyof typeof TOOL_PRESENTATION_STATUS_KEYS
  ]
  return keys ? i18n.global.t(keys[state]) : ''
}
