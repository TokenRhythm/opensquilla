// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import type { ChatToolCall } from '@/types/chat'
import { splitVisualizeBody, visualizeReceipts } from './receipts'
const receipt = { previewStatus: 'inline-ready', presentation: 'inline', documentId: 'doc_123', revisionId: 'rev_123', artifactId: 'artifact_123', name: 'chart.html', mime: 'text/html', size: 100, sha256: 'a'.repeat(64), marker: 'visualize:doc_123:rev_123' }
const call = { toolId: 'call_1', name: 'open_workspace_preview', status: 'success', isRunning: false, isError: false, result: JSON.stringify(receipt) } as ChatToolCall
describe('Visualize authorized receipts and body placement', () => {
  it('requires a successful matching tool result, never a marker by itself', () => {
    expect(visualizeReceipts([{ ...call, isError: true }])).toEqual([])
    expect(visualizeReceipts([{ ...call, name: 'exec' }])).toEqual([])
    expect(visualizeReceipts([{ ...call, result: JSON.stringify({ ...receipt, marker: 'visualize:doc_other:rev_123' }) }])).toEqual([])
    expect(splitVisualizeBody(`<p><code>${receipt.marker}</code></p>`, [])).toEqual([{ key: 'text-0', html: `<p><code>${receipt.marker}</code></p>` }])
  })
  it('places a single canvas between text while preserving trailing content', () => {
    const parts = splitVisualizeBody(`<p>Before</p><p><code>${receipt.marker}</code></p><p>After</p><p><code>${receipt.marker}</code></p>`, visualizeReceipts([call]))
    expect(parts.map(part => part.receipt ? 'canvas' : part.html)).toEqual(['<p>Before</p>', 'canvas', '<p>After</p>'])
  })
  it('does not interpret fenced code or partial prose markers and uses one fallback', () => {
    const html = `<pre><code>${receipt.marker}</code></pre><p>Example <code>${receipt.marker}</code></p>`
    const parts = splitVisualizeBody(html, visualizeReceipts([call, call]))
    expect(parts[0]?.html).toBe(html)
    expect(parts.filter(part => part.receipt)).toHaveLength(1)
    expect(splitVisualizeBody(html, visualizeReceipts([call]), false).filter(part => part.receipt)).toHaveLength(0)
  })
  it('shows only the explicitly selected publication after a draft was republished', () => {
    const latest = { ...receipt, documentId: 'doc_latest', revisionId: 'rev_latest', marker: 'visualize:doc_latest:rev_latest' }
    const receipts = visualizeReceipts([call, { ...call, toolId: 'call_2', result: JSON.stringify(latest) }])
    const parts = splitVisualizeBody(`<p>Updated</p><p><code>${latest.marker}</code></p><p>Use this version.</p>`, receipts)
    expect(parts.filter(part => part.receipt).map(part => part.receipt?.marker)).toEqual([latest.marker])
    expect(parts.map(part => part.receipt ? 'canvas' : part.html)).toEqual(['<p>Updated</p>', 'canvas', '<p>Use this version.</p>'])
    expect(splitVisualizeBody(`<p><code>${receipt.marker}</code></p><p><code>${latest.marker}</code></p>`, receipts)
      .filter(part => part.receipt)).toHaveLength(2)
  })
  it('does not let an unauthorized marker suppress successful receipt fallback', () => {
    const parts = splitVisualizeBody('<p><code>visualize:doc_unknown:rev_unknown</code></p>', visualizeReceipts([call]))
    expect(parts.filter(part => part.receipt).map(part => part.receipt?.marker)).toEqual([receipt.marker])
  })
})
