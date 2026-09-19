import { describe, expect, it } from 'vitest'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { normalizeSessionReferenceV1, normalizeWorkspaceFileReferenceV1, sessionGatewayUrl } from '@/types/references'
import { workspaceReferencesFromMessage } from './workspaceReferences'

const reference = {
  version: 1, kind: 'workspace_file', id: 'src/math.py', label: 'src/math.py:2-4',
  scope: {}, locator: { relativePath: 'src/math.py', startLine: 2, endLine: 4 },
  state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true, copy: true },
}
const call = (name = 'read_source', result: unknown = { reference }) => ({
  name, result: JSON.stringify(result), status: 'success', isRunning: false, isError: false,
}) as ChatToolCall
const message = (overrides: Partial<ChatRenderedMessage>) => ({ ...overrides }) as ChatRenderedMessage

describe('workspace reference extraction', () => {
  it('renders only successful builtin structured receipts and deduplicates restored calls', () => {
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call(), call()] }))).toHaveLength(1)
    for (const tool of [call('exec_command'), call('mcp_untrusted'), { ...call(), isError: true }, { ...call(), isRunning: true }]) {
      expect(workspaceReferencesFromMessage(message({ toolCalls: [tool] }))).toEqual([])
    }
    expect(workspaceReferencesFromMessage(message({ text: JSON.stringify(reference) }))).toEqual([])
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call('read_source', 'src/math.py:2')] }))).toEqual([])
  })
  it.each(['/etc/config', '../secret', 'dir/../secret', 'C:\\data\\file', '\\\\host\\share', 'file:stream', 'a\u0000b', ''])('rejects unsafe path %s', path => {
    expect(normalizeWorkspaceFileReferenceV1({ ...reference, locator: { relativePath: path } })).toBeNull()
  })
  it.each([{ startLine: -1 }, { startLine: 2.5 }, { startLine: 5, endLine: 2 }, { endLine: '3' }])('rejects invalid ranges %s', range => {
    expect(normalizeWorkspaceFileReferenceV1({ ...reference, locator: { ...reference.locator, ...range } })).toBeNull()
  })
  it.each(['src\\source.py', ' source.py', 'source.py '])('does not rewrite legacy file identity %s', path => {
    const receipt = { status: 'success', path, revision: 'file_1234567890abcdef', range: [1, 2] }
    expect(normalizeWorkspaceFileReferenceV1({ ...reference, id: path, locator: { relativePath: path } })).toBeNull()
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call('read_source', receipt)] }))).toEqual([])
  })
  it('does not add an invalid nullable runStatus to file RPC requests', () => {
    expect(normalizeWorkspaceFileReferenceV1(reference)?.state).toEqual(reference.state)
  })
  it('adapts only legacy successful source receipts with a safe path and revision', () => {
    const old = { status: 'success', path: 'src/math.py', revision: 'file_1234567890abcdef', range: [2, 4] }
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call('read_source', old)] }))).toHaveLength(1)
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call('read_source', { ...old, path: '../secret' })] }))).toEqual([])
    expect(workspaceReferencesFromMessage(message({ toolCalls: [call('read_source', { ...old, revision: '' })] }))).toEqual([])
  })
  it('does not turn an empty or conflicting session identity into a different task', () => {
    expect(normalizeSessionReferenceV1({ version: 1, kind: 'session', scope: {} })).toBeNull()
    expect(normalizeSessionReferenceV1({ version: 1, kind: 'session', id: 'one', scope: { sessionKey: 'two' } })).toBeNull()
  })
  it('preserves HTTPS and drops credentials and signed query parameters in Gateway links', () => {
    const url = new URL(sessionGatewayUrl('task', 'https://user:secret@gateway.example/ws?token=secret#secret', '/nested/control'))
    expect(url.protocol).toBe('https:')
    expect(url.pathname).toBe('/nested/control/chat')
    expect(url.username).toBe('')
    expect(url.password).toBe('')
    expect(url.search).toBe('?session=task')
    expect(url.hash).toBe('')
  })
})
