import { describe, expect, it } from 'vitest'

import { toolResultCount } from '@/utils/chat/toolDisplay'

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
