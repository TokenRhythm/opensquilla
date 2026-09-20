import type { ChatRenderedMessage } from '@/types/chat'
import { normalizeWorkspaceFileReferenceV1, type WorkspaceFileReferenceV1 } from '@/types/references'

const SOURCE_TOOLS = new Set(['read_source', 'edit_source', 'grep_search'])

/** Only trusted builtin receipts can create actions; prose and tool input never do. */
export function workspaceReferencesFromMessage(message: ChatRenderedMessage): WorkspaceFileReferenceV1[] {
  const calls = [
    ...(message.toolCalls ?? []),
    ...(message.timelineItems ?? []).flatMap(item => item.type === 'tool-group' ? item.group.calls : []),
  ]
  const seen = new Set<string>()
  return calls.flatMap(call => {
    if (!SOURCE_TOOLS.has(call.name) || call.isRunning || call.isError || call.status !== 'success') return []
    let result: unknown = call.result
    if (typeof result === 'string') {
      try { result = JSON.parse(result) } catch { return [] }
    }
    if (!result || typeof result !== 'object' || Array.isArray(result)) return []
    const row = result as Record<string, unknown>
    const legacy = call.name === 'read_source' && row.reference == null && row.status === 'success'
      && typeof row.path === 'string' && typeof row.revision === 'string'
      && /^file_[0-9a-f]{16}$/.test(row.revision) && Array.isArray(row.range) && row.range.length === 2
      ? {
          version: 1, kind: 'workspace_file', id: row.path, label: row.path, scope: {},
          locator: { relativePath: row.path, startLine: row.range[0], endLine: row.range[1] },
          state: { available: true, revision: row.revision }, capabilities: { open: true, copy: true },
        }
      : null
    const values = [row.reference ?? legacy, ...(Array.isArray(row.references) ? row.references : [])]
    return values.flatMap(value => {
      const reference = normalizeWorkspaceFileReferenceV1(value)
      if (!reference || reference.capabilities.open !== true) return []
      const identity = JSON.stringify([reference.scope, reference.id, reference.locator, reference.state?.revision])
      if (seen.has(identity)) return []
      seen.add(identity)
      return [reference]
    })
  }).slice(0, 30)
}
