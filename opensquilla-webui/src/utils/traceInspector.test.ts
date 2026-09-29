import { describe, expect, it } from 'vitest'
import type { TraceSpan } from '@/types/traceView'
import { traceInspectorModel } from './traceInspector'

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return { id: 'test-step', phase: 'tool_execution', kind: 'tool_response', title: 'Tool', status: 'success', ...overrides }
}

describe('traceInspectorModel', () => {
  it('interprets recorded boundaries consistently even when older phase labels are broad', () => {
    const approval = traceInspectorModel(span({ kind: 'tool_approval', phase: 'tool_execution' }), null, {
      approval_id: 'approval-demo', reason: 'permission_required',
    })
    expect(approval).toMatchObject({ category: 'approval', summaryKey: 'summaries.approval' })
    expect(approval.facts).toContainEqual({ key: 'approval_id', value: 'approval-demo' })
    expect(approval.facts).toContainEqual({ key: 'reason', value: 'permission_required' })
    expect(traceInspectorModel(span({ kind: 'context_compaction', phase: 'context' }), null, {}))
      .toMatchObject({ category: 'maintenance', summaryKey: 'summaries.maintenance' })
    expect(traceInspectorModel(span({ kind: 'provider_retry', phase: 'model_execution' }), null, {}))
      .toMatchObject({ category: 'retry', summaryKey: 'summaries.retry' })
  })

  it('keeps complete authored file content as code and separates its path', () => {
    const content = 'print("hello")\n'.repeat(1000)
    const model = traceInspectorModel(span({ toolName: 'write_file' }), {
      arguments: { path: '/workspace/main.py', content, create_parents: false },
    }, { result: 'Wrote file', is_error: false })
    expect(model.summaryKey).toBe('summaries.tool')
    expect(model.inputs.find(block => block.labelKey === 'blocks.content')).toMatchObject({
      format: 'code', language: 'python', value: content,
    })
    expect(model.inputs.find(block => block.labelKey === 'blocks.path')?.value).toBe('/workspace/main.py')
    expect(model.inputs.find(block => block.label === 'create_parents')?.value).toBe(false)
    expect(model.outputs.find(block => block.labelKey === 'blocks.result')?.value).toBe('Wrote file')
  })

  it('renders exact old/new edits as separate diff blocks without dropping edit metadata', () => {
    const model = traceInspectorModel(span({ toolName: 'edit_file' }), {
      arguments: {
        path: 'file.ts', edits: [
          { old_text: 'before', new_text: '', replace_all: true },
          { oldText: '', newText: 'after', custom: { retained: 1 } },
        ],
      },
    }, null)
    expect(model.inputs.filter(block => block.format === 'diff')).toMatchObject([
      { value: 'before', secondaryValue: '', language: 'typescript', labelKey: 'blocks.changes' },
      { value: '', secondaryValue: 'after', language: 'typescript', labelKey: 'blocks.changes' },
    ])
    expect(model.inputs.find(block => block.label === 'replace_all')?.value).toBe(true)
    expect(model.inputs.find(block => block.label === 'custom')?.value).toEqual({ retained: 1 })
    expect(model.outputs).toEqual([])
  })

  it('decodes nested tool results and separates terminal streams and exit status', () => {
    const model = traceInspectorModel(span({ toolName: 'shell' }), {
      arguments: { command: 'printf test', timeout: 30 },
    }, {
      result: JSON.stringify({ result_json: JSON.stringify({ stdout: 'test\n', stderr: 'warning', exit_code: 0, custom: 'kept' }) }),
      duration_ms: 12,
    })
    expect(model.inputs.find(block => block.labelKey === 'blocks.command')).toMatchObject({ value: 'printf test', language: 'bash' })
    expect(model.outputs.find(block => block.labelKey === 'blocks.stdout')).toMatchObject({ value: 'test\n', format: 'code' })
    expect(model.outputs.find(block => block.labelKey === 'blocks.stderr')?.value).toBe('warning')
    expect(model.outputs.find(block => block.labelKey === 'blocks.exitCode')?.value).toBe(0)
    expect(model.outputs.find(block => block.label === 'custom')?.value).toBe('kept')
    expect(model.outputs.find(block => block.label === 'duration_ms')?.value).toBe(12)
  })

  it('renders read-file lines as code while retaining line numbers and metadata', () => {
    const model = traceInspectorModel(span({ toolName: 'read_file' }), { arguments: { path: 'test.py' } }, {
      result: JSON.stringify({ lines: [{ number: 7, text: 'x = 1' }, { number: 8, text: 'print(x)' }], total_lines: 20 }),
    })
    expect(model.outputs.find(block => block.labelKey === 'blocks.content')).toMatchObject({
      value: '7\tx = 1\n8\tprint(x)', format: 'code', language: 'python',
    })
    expect(model.outputs.find(block => block.label === 'total_lines')?.value).toBe(20)
  })

  it('keeps each model message visible with config, schemas, reasoning, and answer separated', () => {
    const input = {
      messages: [
        { role: 'system', content: 'System instruction' },
        { role: 'user', content: [{ type: 'text', text: 'User request' }, { type: 'image_url', image_url: { url: 'https://example.invalid/image.png' } }] },
        { role: 'assistant', content: null, tool_calls: [{ id: 'call-a', function: { name: 'read', arguments: '{}' } }] },
      ],
      config: { temperature: 0, max_tokens: 2048 },
      tools: [{ name: 'read', input_schema: { type: 'object' } }],
      extension: { custom: true },
    }
    const original = JSON.stringify(input)
    const model = traceInspectorModel(span({ phase: 'model_execution', kind: 'llm_response', model: 'demo' }), input, {
      reasoning_content: 'Reasoning output', text: 'Answer',
      usage: { input_tokens: 10, output_tokens: 4 }, tool_calls: [],
    })
    expect(model.inputs.find(block => block.labelKey === 'roles.system')?.value).toBe('System instruction')
    expect(model.inputs.find(block => block.labelKey === 'roles.user')?.value).toBe('User request')
    expect(model.inputs.some(block => (block.value as { image_url?: unknown })?.image_url != null)).toBe(true)
    expect(model.inputs.find(block => block.labelKey === 'roles.assistant')?.value).toMatchObject({ tool_calls: input.messages[2]!.tool_calls })
    expect(model.inputs.find(block => block.label === 'config.temperature')?.value).toBe(0)
    expect(model.inputs.find(block => block.labelKey === 'blocks.tools')).toMatchObject({ value: input.tools, collapsed: true })
    expect(model.inputs.find(block => block.label === 'extension')?.value).toEqual({ custom: true })
    expect(model.outputs.find(block => block.labelKey === 'blocks.reasoning')?.value).toBe('Reasoning output')
    expect(model.outputs.find(block => block.labelKey === 'blocks.text')?.value).toBe('Answer')
    expect(model.outputs.find(block => block.labelKey === 'blocks.usage')?.value).toEqual({ input_tokens: 10, output_tokens: 4 })
    expect(model.facts).toContainEqual({ key: 'message_count', value: 3 })
    expect(JSON.stringify(input)).toBe(original)
  })

  it('handles search arrays and artifact metadata without cutting or hiding unknown fields', () => {
    const model = traceInspectorModel(span({ toolName: 'search' }), { arguments: { query: 'find item' } }, {
      result: { results: [{ path: 'a.txt', snippet: 'one', score: 0 }, { path: 'b.txt', snippet: 'two' }], artifact: { id: 'file-a', url: 'https://example.invalid/item' }, custom: [1, 2] },
    })
    const results = model.outputs.filter(block => block.labelKey === 'blocks.results')
    expect(results.map(block => block.label)).toEqual(['a.txt', 'b.txt'])
    expect(results.map(block => block.children?.find(child => child.labelKey === 'blocks.path')?.value)).toEqual(['a.txt', 'b.txt'])
    expect(results[0]?.children?.find(block => block.label === 'score')).toMatchObject({ value: 0, collapsed: true })
    expect(results[0]?.value).toEqual({ path: 'a.txt', snippet: 'one', score: 0 })
    expect(model.outputs.find(block => block.labelKey === 'blocks.artifact')?.value).toEqual({ id: 'file-a', url: 'https://example.invalid/item' })
    expect(model.outputs.find(block => block.label === 'custom')?.value).toEqual([1, 2])
    expect(model.facts).toContainEqual({ key: 'result_count', value: 2 })
  })

  it('unwraps full record payloads but preserves envelope metadata', () => {
    const model = traceInspectorModel(span({ toolName: 'read_file' }), {
      kind: 'tool_request', seq: 2, payload: { arguments: { path: 'file.py' } }, trace_id: 'trace-test',
    }, { kind: 'tool_response', seq: 3, payload: { result: 'x = 1\n' }, extension: 'metadata' })
    expect(model.outputs.find(block => block.labelKey === 'blocks.result')).toMatchObject({ value: 'x = 1\n', language: 'python' })
    expect(model.outputs.find(block => block.labelKey === 'blocks.metadata')?.value).toMatchObject({ extension: 'metadata' })
    expect(model.facts).toContainEqual({ key: 'path', value: 'file.py' })
  })

  it('shows live partial tool arguments and keeps truncation counters as data', () => {
    const model = traceInspectorModel(span({ phase: 'model_execution', kind: 'llm_progress', status: 'running' }), {}, {
      text: 'partial', partial: true, text_offset: 12000,
      tool_calls: [{ tool_use_id: 'call-a', name: 'write_file', arguments_text: '{"content":"unfinished', arguments_truncated: true }],
    })
    const call = model.outputs.find(block => block.label === 'write_file')
    expect(call?.children?.find(block => block.labelKey === 'blocks.arguments')).toMatchObject({ value: '{"content":"unfinished', format: 'code', language: 'json' })
    expect(model.outputs.find(block => block.label === 'text_offset')).toMatchObject({ value: 12000, collapsed: true })
    expect(call?.children?.find(block => block.label === 'arguments_truncated')).toMatchObject({ value: true, collapsed: true })
    expect(model.outputs.find(block => block.label === 'partial')?.collapsed).toBe(true)
    expect(model.facts).toContainEqual({ key: 'partial', value: true })
  })

  it('keeps model tool calls in separate parent cards with their complete original values', () => {
    const calls = [
      { tool_use_id: 'a', name: 'write_file', arguments: { path: 'a.py', content: 'a = 1' } },
      { tool_use_id: 'b', name: 'write_file', arguments: { path: 'b.ts', content: 'const b = 2' } },
      { id: 'c', function: { name: 'read_file', arguments: '{"path":"c.txt"}' } },
    ]
    const model = traceInspectorModel(span({ phase: 'model_execution' }), null, { tool_calls: calls })
    expect(model.outputs).toHaveLength(3)
    expect(model.outputs.map(block => block.label)).toEqual(['write_file', 'write_file', 'read_file'])
    expect(model.outputs.map(block => block.value)).toEqual(calls)
    expect(model.outputs.every(block => !block.labelKey && !block.collapsed)).toBe(true)
    expect(model.outputs[0]?.children?.find(block => block.labelKey === 'blocks.content')).toMatchObject({ value: 'a = 1', language: 'python' })
    expect(model.outputs[1]?.children?.find(block => block.labelKey === 'blocks.content')).toMatchObject({ value: 'const b = 2', language: 'typescript' })
    expect(model.outputs[2]?.children?.find(block => block.labelKey === 'blocks.path')?.value).toBe('c.txt')
    expect(new Set(model.outputs.map(block => block.id)).size).toBe(3)
  })

  it('keeps unknown text and HTML inert and leaves absent output empty', () => {
    const html = '<script>fetch("https://example.invalid")</script>'
    const model = traceInspectorModel(span({ toolName: 'read_file' }), { arguments: { path: 'page.html' } }, { result: html })
    expect(model.outputs[0]).toMatchObject({ value: html, format: 'code', language: 'html' })
    expect(traceInspectorModel(span(), null, undefined).outputs).toEqual([])
    expect(traceInspectorModel(span(), undefined, null).inputs).toEqual([])
    expect(traceInspectorModel(span(), {}, 'not JSON {').outputs[0]?.value).toBe('not JSON {')
  })
})
