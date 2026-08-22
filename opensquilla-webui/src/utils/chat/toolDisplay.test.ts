import { describe, expect, it } from 'vitest'

import {
  isDocumentAgentToolName,
  isDocumentWriterToolName,
  toolActionLabel,
  toolDisplayName,
  toolOperationKey,
  toolResultCount,
  toolSecondaryText,
} from '@/utils/chat/toolDisplay'

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

describe('page tool product presentation', () => {
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
