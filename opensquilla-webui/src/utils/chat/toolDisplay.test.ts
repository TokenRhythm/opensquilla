import { describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'

import {
  toolDisplayInputText,
  toolActionLabel,
  toolCallGroups,
  toolIconName,
  toolOperationKey,
  toolGroupStatusText,
  toolResultCount,
  toolSecondaryText,
  toolStatusText,
} from '@/utils/chat/toolDisplay'
import type { ChatToolCall, ToolPresentation } from '@/types/chat'

describe('registered browser, preview and skill tool presentation', () => {
  it('localizes the known actions with the matching type icon', async () => {
    await loadLocaleMessages('zh-Hans')
    const previousLocale = i18n.global.locale.value
    try {
      i18n.global.locale.value = 'zh-Hans'
      for (const [name, label, icon] of [
        ['mcp__desktop-browser__browser_open', '打开浏览器', 'monitor'],
        ['open_workspace_preview', '打开预览', 'monitor'],
        ['skill_view', '读取技能', 'skills'],
      ]) {
        expect(toolActionLabel(name)).toBe(label)
        expect(toolIconName(name)).toBe(icon)
      }
    } finally {
      i18n.global.locale.value = previousLocale
    }
  })

  it('does not label unrelated MCP tools or similar names as these actions', () => {
    for (const name of [
      'mcp__other-browser__browser_open', 'mcp__desktop-browser__browser_open_extra',
      'mcp__custom__open_workspace_preview', 'custom_skill_view',
    ]) {
      expect(toolActionLabel(name)).toBe('Tool call')
      expect(toolIconName(name)).toBe('gear')
    }
  })

  it('keeps raw identities, error state, disclosure and separate groups', () => {
    const names = ['mcp__desktop-browser__browser_open', 'open_workspace_preview', 'skill_view']
    const calls = names.map((name, index): ChatToolCall => ({
      toolId: `known-${index}`, name, displayName: name,
      inputRaw: '{"path":"game/index.html"}', inputPreview: '',
      isRunning: false, status: 'error', isError: true, isOpen: false,
      result: 'Original error detail', resultPreview: 'Original error detail',
    }))
    const groups = toolCallGroups(calls, 'owner')
    expect(groups.map(group => group.label)).toEqual(['Open browser', 'Open preview', 'Read skill'])
    expect(groups.map(group => group.iconName)).toEqual(['monitor', 'monitor', 'skills'])
    expect(groups.map(group => group.operationKey)).toEqual(names.map(toolOperationKey))
    groups.forEach((group, index) => {
      expect(group.isError).toBe(true)
      expect(group.status).toBe('error')
      expect(group.calls).toHaveLength(1)
      expect(group.calls[0]).toMatchObject(calls[index])
    })
  })
})

describe('unknown tool presentation', () => {
  it('keeps the exact tool identifier separate from the localized generic action', async () => {
    const name = 'mcp__acme-v2__diagram_probe'
    const call: ChatToolCall = {
      toolId: 'probe-1', name, displayName: name, inputRaw: '{"action":"private_action"}', inputPreview: '',
      isRunning: false, status: 'success', isError: false, result: 'done', resultPreview: 'done', isOpen: false,
    }
    const previousLocale = i18n.global.locale.value
    try {
      for (const [locale, label] of [
        ['en', 'Tool call'], ['zh-Hans', '调用工具'], ['ja', 'ツール呼び出し'],
        ['fr', 'Appel d’outil'], ['de', 'Werkzeugaufruf'], ['es', 'Llamada a herramienta'],
      ] as const) {
        await loadLocaleMessages(locale)
        i18n.global.locale.value = locale
        const group = toolCallGroups([call], 'owner')[0]
        expect(group.label).toBe(label)
        expect(group.secondary).toBe(name)
        expect(group.calls[0]).toMatchObject(call)
        expect(group.operationKey).toBe(toolOperationKey(name))
      }
    } finally {
      i18n.global.locale.value = previousLocale
    }
  })
})

describe('stored tool result retrieval presentation', () => {
  const call = (overrides: Partial<ChatToolCall> = {}): ChatToolCall => ({
    toolId: 'retrieve-1', name: 'retrieve_tool_result', displayName: 'retrieve_tool_result',
    inputRaw: '{"handle":"result-opaque-id","mode":"query","query":"report.html"}',
    inputPreview: '{"handle":"result-opaque-id","mode":"query","query":"report.html"}',
    isRunning: false, status: 'success', isError: false,
    result: '[tool_result_retrieval]\nhandle: result-opaque-id\ntool_name: web_fetch\n---\npage body',
    resultPreview: 'page body', isOpen: false, ...overrides,
  })

  it('localizes exact and namespaced tool names before generic name heuristics', async () => {
    await loadLocaleMessages('zh-Hans')
    const previousLocale = i18n.global.locale.value
    try {
      i18n.global.locale.value = 'zh-Hans'
      for (const name of [
        'retrieve_tool_result', 'functions.retrieve_tool_result', 'tools/retrieve_tool_result',
        'tools:retrieve_tool_result', 'mcp__web_search__retrieve_tool_result',
      ]) {
        expect(toolActionLabel(name)).toBe('读取工具结果')
        expect(toolIconName(name)).toBe('logs')
        expect(toolOperationKey(name)).toBe('tool.retrieve.tool.result')
      }
      expect(toolActionLabel('custom_retrieve_tool_result')).not.toBe('读取工具结果')
      expect(toolActionLabel('retrieve_tool_result_stats')).not.toBe('读取工具结果')
      expect(toolActionLabel('retrieve-tool-result')).not.toBe('读取工具结果')
    } finally {
      i18n.global.locale.value = previousLocale
    }
  })

  it('does not mistake an opaque handle, query or returned body for a source identity', () => {
    expect(toolSecondaryText(call())).toBe('')
    expect(toolSecondaryText(call({ name: 'functions.retrieve_tool_result' }))).toBe('')
    expect(toolSecondaryText(call({
      inputRaw: '', inputPreview: '', resultPreview: 'https://example.test/report.html',
    }))).toBe('')
    expect(toolSecondaryText(call({
      presentation: { category: 'generic', primaryArguments: ['handle'], argumentDisplay: 'primary', lifecycleDisplay: 'boundary' },
    }))).toBe('')
  })

  it('keeps retrieval status and raw details while grouping by the recognized operation', () => {
    const failed = call({ status: 'error', isError: true })
    const group = toolCallGroups([failed], 'owner')[0]
    expect(group.label).toBe('Read tool result')
    expect(group.iconName).toBe('logs')
    expect(group.secondary).toBe('')
    expect(group.isError).toBe(true)
    expect(group.calls[0].inputRaw).toBe(failed.inputRaw)
    expect(group.calls[0].result).toBe(failed.result)
  })
})

describe('managed process tool presentation', () => {
  const call = (overrides: Partial<ChatToolCall> = {}): ChatToolCall => ({
    toolId: 'poll-1', name: 'process', displayName: 'process',
    inputRaw: '{"action":"poll","execution_id":"exec-1"}', inputPreview: '',
    isRunning: false, status: 'success', isError: false,
    result: '', resultPreview: '', isOpen: false, ...overrides,
  })

  it('localizes the process action in Chinese without changing command status', async () => {
    await loadLocaleMessages('zh-Hans')
    const previousLocale = i18n.global.locale.value
    try {
      i18n.global.locale.value = 'zh-Hans'
      expect(toolActionLabel('process', { action: 'poll' })).toBe('检查后台命令')
      expect(toolActionLabel('process', '{"action":"wait"}')).toBe('等待后台命令')
      expect(toolActionLabel('process', { action: 'log' })).toBe('读取后台输出')
      expect(toolActionLabel('process', { action: 'kill' })).toBe('停止后台命令')
      expect(toolActionLabel('process', { action: 'unknown' })).toBe('管理后台命令')
      expect(toolActionLabel('custom_process_reader', { action: 'poll' })).not.toBe('检查后台命令')
      expect(toolIconName('process')).toBe('terminal')
      expect(toolStatusText(call({ status: 'error', isError: true }))).toBe('失败')
    } finally {
      i18n.global.locale.value = previousLocale
    }
  })

  it('prefers the actual managed command and falls back to the execution handle', () => {
    expect(toolSecondaryText(call({ result: JSON.stringify({
      status: 'ok', action: 'poll', session: { command: 'python -m http.server 8080' },
    }) }))).toBe('python -m http.server 8080')
    expect(toolSecondaryText(call())).toBe('exec-1')
    expect(toolSecondaryText(call({ inputRaw: '{"action":"wait","execution_ids":["a","b"]}' }))).toBe('a · b')
    expect(toolSecondaryText(call({ inputRaw: '{"action":"list"}', result: '{"sessions":[]}' }))).toBe('')
  })

  it('keeps hidden arguments and arbitrary result text out of the compact target', () => {
    expect(toolSecondaryText(call({ inputRaw: '{"action":"poll"}', result: 'private output' }))).toBe('')
    expect(toolSecondaryText(call({
      result: '{"session":{"command":"private command"}}',
      presentation: { category: 'generic', primaryArguments: [], argumentDisplay: 'primary', lifecycleDisplay: 'boundary' },
    }))).toBe('')
  })

  it('uses a specific label for one action and a neutral label for mixed process actions', () => {
    expect(toolCallGroups([call()], 'owner')[0].label).toBe('Check background command')
    expect(toolCallGroups([call(), call({ inputRaw: '{"action":"kill"}' })], 'owner')[0].label).toBe('Manage background command')
  })
})

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
