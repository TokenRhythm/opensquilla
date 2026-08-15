// PetPermissions — approval cards from the gateway's exec.approval.* events.
//
// OpenSquilla's approval queue pushes `exec.approval.requested|updated|resolved`
// to every connection holding operator.approvals. The pet surfaces these as
// "waiting" cards and resolves them via `exec.approval.resolve {id, approved}`.

import { PendingApproval } from './types.js'
import { GatewayRpc } from './rpc.js'

export interface PermissionDeps {
  rpc: () => GatewayRpc | null
  onChange: () => void
}

export function createPermissions(deps: PermissionDeps) {
  const entries = new Map<string, PendingApproval>()

  function ingest(kind: string, payload: Record<string, unknown>): void {
    const id = typeof payload.approval_id === 'string' ? payload.approval_id : ''
    if (!id) return
    if (kind === 'resolved') {
      entries.delete(id)
      deps.onChange()
      return
    }
    const existing = entries.get(id)
    if (kind === 'requested' && existing) return
    const entry: PendingApproval = {
      id,
      namespace: typeof payload.namespace === 'string' ? payload.namespace : 'exec',
      sessionKey: typeof payload.session_key === 'string' ? payload.session_key : '',
      toolName: typeof payload.tool_name === 'string' ? payload.tool_name : 'Unknown',
      command: typeof payload.command === 'string' ? payload.command : '',
      approvalKind: typeof payload.approval_kind === 'string' ? payload.approval_kind : '',
      agent: typeof payload.agent === 'string' ? payload.agent : '',
      args: payload.args && typeof payload.args === 'object' ? (payload.args as Record<string, unknown>) : null,
      warning: typeof payload.warning === 'string' ? payload.warning : '',
      createdAt: Number(payload.created_at) || Date.now(),
      deadline: typeof payload.deadline === 'number' ? payload.deadline : null,
    }
    if (kind === 'updated' && existing) {
      Object.assign(existing, { command: entry.command, args: entry.args, warning: entry.warning, deadline: entry.deadline })
    } else {
      entries.set(id, entry)
    }
    deps.onChange()
  }

  function decide(id: string, approved: boolean): void {
    const rpc = deps.rpc()
    if (!rpc) return
    rpc.call('exec.approval.resolve', { id, approved }).catch(() => {
      // resolution failed — leave the card; user can retry or the gateway retries
    })
  }

  /** Remove approvals tied to a session when that session ends. */
  function sweepForSessionEvent(sessionKey: string): void {
    let changed = false
    for (const [id, entry] of entries) {
      if (entry.sessionKey === sessionKey) { entries.delete(id); changed = true }
    }
    if (changed) deps.onChange()
  }

  function getPending(): PendingApproval[] {
    return [...entries.values()]
  }

  return { ingest, decide, sweepForSessionEvent, getPending }
}

export type PetPermissions = ReturnType<typeof createPermissions>
