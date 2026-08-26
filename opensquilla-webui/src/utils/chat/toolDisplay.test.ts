import { describe, expect, it } from 'vitest'

import {
  isDocumentAgentToolName,
  isDocumentWriterToolName,
  toolDisplayInputText,
  toolActionLabel,
  toolDisplayName,
  toolGroupStatusText,
  toolOperationKey,
  toolResultCount,
  toolSecondaryText,
  toolStatusText,
} from '@/utils/chat/toolDisplay'
import type { ChatToolCall, ToolPresentation } from '@/types/chat'

describe('toolDisplayInputText', () => {
  it('keeps only server-declared primary arguments', () => {
    expect(toolDisplayInputText({
      input: {
        url: 'https://example.test/report',
        headers: { Authorization: 'secret' },
        body: 'private request body',
      },
      tool_presentation: {
        category: 'network_read',
        primaryArguments: ['url'],
        argumentDisplay: 'primary',
        lifecycleDisplay: 'boundary',
      },
    })).toBe(JSON.stringify({ url: 'https://example.test/report' }, null, 2))
  })

  it('keeps full arguments for all-argument rules', () => {
    const input = { path: 'src/app.py', content: 'print(1)' }
    expect(toolDisplayInputText({
      input,
      tool_presentation: {
        category: 'mutation',
        primaryArguments: ['path'],
        argumentDisplay: 'all',
        lifecycleDisplay: 'default',
      },
    })).toBe(JSON.stringify(input, null, 2))
  })

  it('does not expose malformed partial JSON for a primary-only rule', () => {
    expect(toolDisplayInputText({
      input: '{"url":"https://example.test',
      tool_presentation: {
        category: 'network_read',
        primaryArguments: ['url'],
        argumentDisplay: 'primary',
        lifecycleDisplay: 'boundary',
      },
    })).toBe('')
  })

  it('preserves legacy behavior when rule metadata is absent', () => {
    expect(toolDisplayInputText({ input: '{"value":1}' })).toBe('{"value":1}')
  })

  it('fails closed when rule metadata exists but is malformed', () => {
    expect(toolDisplayInputText({
      input: { url: 'https://example.test', token: 'secret' },
      tool_presentation: { argumentDisplay: 'all' },
    })).toBe('')
  })
})

describe('toolResultCount', () => {
  it('counts structured result collections', () => {
    expect(toolResultCount(JSON.stringify([{ id: 1 }, { id: 2 }]), 'web_search')).toBe(2)
    expect(toolResultCount(
      JSON.stringify({ results: [{ id: 1 }, { id: 2 }, { id: 3 }] }),
      'web_search',
    )).toBe(3)
  })

  it('preserves legacy plain-text summaries for result-producing tools', () => {
    expect(toolResultCount('Search returned 3 results.', 'web_search')).toBe(3)
    expect(toolResultCount('Found 4 results for "squid".\n1. One\n2. Two', 'webSearch')).toBe(4)
    expect(toolResultCount('共找到 5 条结果。', 'mcp__catalog__search')).toBe(5)
    expect(toolResultCount(JSON.stringify('6 results'), 'session_search')).toBe(6)
    expect(toolResultCount('Found 7 results.', 'MCPURLSearch')).toBe(7)
  })

  it('does not treat a year in structured web content as a result count', () => {
    const webFetchResult = JSON.stringify({
      url: 'https://example.test/ai-news-today',
      title: 'AI News Today',
      text: 'The 2026 results will be published in the annual report.',
    })

    expect(toolResultCount(webFetchResult, 'web_fetch')).toBeNull()
  })

  it('does not infer counts from plain text returned by content tools', () => {
    expect(toolResultCount('2026 results', 'web_fetch')).toBeNull()
    expect(toolResultCount(JSON.stringify('2026 results'), 'web_fetch')).toBeNull()
    expect(toolResultCount('The 2026 results will be published.', 'shell')).toBeNull()
    expect(toolResultCount('Found 3 results for "squid".', 'research_article')).toBeNull()
  })

  it('does not scan search result bodies or treat a bare year as a count', () => {
    expect(toolResultCount('[grep_search]\nreturned: 2\n---\n2026 results', 'grep_search')).toBeNull()
    expect(toolResultCount('3 results.txt\nanother-file.txt', 'glob_search')).toBeNull()
    expect(toolResultCount('2026 results\nanother-file.txt', 'glob_search')).toBeNull()
    expect(toolResultCount('Found 2026 results.txt', 'web_search')).toBeNull()
    expect(toolResultCount('2026 results', 'web_search')).toBeNull()
    expect(toolResultCount('Found 2026 results.', 'web_search')).toBe(2026)
  })

  it('uses array structure before count-like result text', () => {
    const results = [
      { title: '2026 results' },
      { title: 'Another result' },
    ]

    expect(toolResultCount(JSON.stringify({ results }), 'web_search')).toBe(2)
  })
})

describe('category-specific tool lifecycle presentation', () => {
  const presentation = (
    category: ToolPresentation['category'],
    primaryArguments: string[],
  ): ToolPresentation => ({
    category,
    primaryArguments,
    argumentDisplay: 'primary',
    lifecycleDisplay: 'boundary',
  })

  const toolCall = (overrides: Partial<ChatToolCall>): ChatToolCall => ({
    toolId: 'tool-1',
    name: 'read_file',
    displayName: 'read_file',
    inputRaw: '',
    inputPreview: '',
    isRunning: false,
    status: '',
    isError: false,
    result: '',
    resultPreview: '',
    isOpen: false,
    ...overrides,
  })

  it.each([
    ['search', 'Searching', 'Search complete', 'Search failed'],
    ['file_read', 'Reading files', 'File reading complete', 'File reading failed'],
    ['network_read', 'Accessing network', 'Network access complete', 'Network access failed'],
    ['command', 'Executing command', 'Command execution complete', 'Command execution failed'],
    ['subagent', 'Delegating task', 'Task delegation complete', 'Task delegation failed'],
    ['mutation', 'Applying changes', 'Changes applied', 'Failed to apply changes'],
    ['generic', 'Calling tool', 'Tool call complete', 'Tool call failed'],
  ] as const)('shows dedicated %s start and end text', (category, running, done, failed) => {
    const rule = presentation(category, category === 'search' ? ['query'] : category === 'file_read' ? ['path'] : ['url'])

    expect(toolStatusText(toolCall({ presentation: rule, isRunning: true }))).toBe(running)
    expect(toolStatusText(toolCall({ presentation: rule, status: 'success' }))).toBe(done)
    expect(toolStatusText(toolCall({ presentation: rule, status: 'error', isError: true }))).toBe(failed)
  })

  it('lists only primary paths and URLs as readable secondary text', () => {
    const inputRaw = JSON.stringify({
      url: 'https://example.test/report',
      headers: { Authorization: 'secret' },
    })
    expect(toolSecondaryText(toolCall({
      name: 'http_request',
      inputRaw,
      inputPreview: inputRaw,
      presentation: presentation('network_read', ['url']),
    }))).toBe('https://example.test/report')

    expect(toolSecondaryText(toolCall({
      name: 'web_search',
      inputRaw: JSON.stringify({ query: 'AI news today', mode: 'news' }),
      presentation: presentation('search', ['query']),
    }))).toBe('')

    expect(toolSecondaryText(toolCall({
      name: 'read_file',
      inputRaw: JSON.stringify({ path: 'src/App.vue', offset: 200 }),
      presentation: presentation('file_read', ['path']),
    }))).toBe('src/App.vue')
  })

  it('does not fall back to result content when a hidden tool has no address', () => {
    expect(toolSecondaryText(toolCall({
      name: 'custom_reader',
      resultPreview: 'private file contents',
      presentation: presentation('file_read', ['path']),
    }))).toBe('')
  })

  it('uses the same dedicated text on the collapsed tool group', () => {
    const call = toolCall({
      isRunning: true,
      presentation: presentation('file_read', ['path']),
    })

    expect(toolGroupStatusText({
      groupId: 'group-1',
      operationKey: 'file.inspect',
      label: 'Inspect files',
      iconName: 'logs',
      calls: [{ ...call, renderKey: 'read-1' }],
      secondary: '',
      isRunning: true,
      isError: false,
      status: '',
    })).toBe('Reading files')
  })

  it('uses dedicated lifecycle text for mutation tools with full argument display', () => {
    expect(toolStatusText(toolCall({
      isRunning: true,
      presentation: {
        category: 'mutation',
        primaryArguments: ['path'],
        argumentDisplay: 'all',
        lifecycleDisplay: 'default',
      },
    }))).toBe('Applying changes')
  })

  it.each([
    ['write_file', 'Writing file', 'File written', 'File write failed'],
    ['edit_source', 'Editing file', 'File edited', 'File edit failed'],
  ])('uses file-specific lifecycle text for %s', (name, running, done, failed) => {
    const rule: ToolPresentation = {
      category: 'mutation',
      primaryArguments: ['path'],
      argumentDisplay: 'all',
      lifecycleDisplay: 'default',
    }

    expect(toolStatusText(toolCall({ name, presentation: rule, isRunning: true }))).toBe(running)
    expect(toolStatusText(toolCall({ name, presentation: rule, status: 'success' }))).toBe(done)
    expect(toolStatusText(toolCall({ name, presentation: rule, status: 'error', isError: true }))).toBe(failed)
  })
})

describe('page tool product presentation', () => {
  it.each([
    ['create_source', 'file.write'],
    ['write_scratch', 'file.write'],
    ['edit_source', 'file.edit'],
    ['apply_patch', 'file.edit'],
  ])('maps source mutation %s to %s details', (name, operation) => {
    expect(toolOperationKey(name)).toBe(operation)
  })

  it.each([
    'document_inspect',
    'document_read',
    'document_locate',
    'document_apply',
    'document_patch',
    'document_browser_inspect',
    'document_browser_act',
    'document_browser_screenshot',
    'document_browser_reload',
    'document_finish',
    'mcp__document_browser_act',
  ])('recognizes %s as document-agent activity', (name) => {
    expect(isDocumentAgentToolName(name)).toBe(true)
  })

  it.each([
    'document_apply',
    'document_patch',
    'gateway.document_apply',
    'gateway/document_patch',
    'gateway:document_apply',
    'gateway__document_patch',
  ])('recognizes %s as a document writer', (name) => {
    expect(isDocumentWriterToolName(name)).toBe(true)
  })

  it('does not classify ordinary file writers as document writers', () => {
    expect(isDocumentWriterToolName('apply_patch')).toBe(false)
    expect(isDocumentWriterToolName('edit_file')).toBe(false)
  })

  it.each([
    ['document_read', 'document.read', 'Read page'],
    ['document_locate', 'document.read', 'Read page'],
    ['gateway.document_inspect', 'document.read', 'Read page'],
    ['document_browser_inspect', 'document.read', 'Read page'],
    ['document_browser_screenshot', 'document.read', 'Read page'],
    ['document_browser_reload', 'document.read', 'Read page'],
    ['document_browser_act', 'document.update', 'Update page'],
    ['document_finish', 'document.update', 'Update page'],
    ['document_apply', 'document.update', 'Update page'],
    ['document_patch', 'document.update', 'Update page'],
  ])('maps %s to a product action', (name, operation, label) => {
    expect(toolOperationKey(name)).toBe(operation)
    expect(toolDisplayName(name, '{}')).toBe(label)
    expect(toolActionLabel(name)).toBe(label)
  })

  it('never exposes page-tool protocol payloads as secondary text', () => {
    const raw = JSON.stringify({
      expectedSha256: 'a'.repeat(64),
      cursor: 'private-cursor',
      grant: 'one-time-grant',
    })
    expect(toolSecondaryText({
      toolId: 'tool-1',
      name: 'document_apply',
      displayName: 'document_apply',
      inputRaw: raw,
      inputPreview: raw,
      result: raw,
      resultPreview: raw,
      isRunning: false,
      status: 'success',
      isError: false,
      isOpen: false,
    })).toBe('')
  })
})
