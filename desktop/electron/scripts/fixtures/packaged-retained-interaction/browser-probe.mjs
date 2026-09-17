// Observes the existing renderer socket. Never proxies, drops, acknowledges,
// or injects an RPC frame. This function is serializable for page.evaluate().
export function installRetainedRpcProbe() {
  if (globalThis.__retainedAuditRpc) return
  const probe = { requests: [], events: [] }
  Object.defineProperty(globalThis, '__retainedAuditRpc', { value: probe })
  const observed = new WeakSet()
  const originalSend = WebSocket.prototype.send
  function recordEvent(value, name, inheritedSession, depth = 0) {
    if (!value || typeof value !== 'object' || depth > 4) return
    if (Array.isArray(value)) {
      for (const item of value) recordEvent(item, name, inheritedSession, depth + 1)
      return
    }
    const sessionKey = value.session_key || value.sessionKey || value.key || inheritedSession
    const taskId = value.task_id || value.taskId || value.turn_id
    const reason = value.reason || value.terminal_reason || value.outcome?.kind || value.turn_outcome?.kind
    const cancelled = value.cancelled === true
      || [value.status, value.task_status, value.run_status, value.outcome?.kind, value.turn_outcome?.kind].includes('cancelled')
    if (typeof taskId === 'string' && (cancelled || ['aborted', 'cancelled'].includes(reason))) {
      probe.events.push({ name, sessionKey, taskId, reason, cancelled })
    }
    for (const key of ['last_task', 'changed_task', 'task', 'tasks', 'task_snapshot', 'snapshot', 'session', 'sessions']) {
      recordEvent(value[key], name, sessionKey, depth + 1)
    }
  }
  WebSocket.prototype.send = function retainedAuditObservedSend(data) {
    if (!observed.has(this)) {
      observed.add(this)
      this.addEventListener('message', event => {
        try {
          const frame = JSON.parse(event.data)
          if (frame.type === 'event') recordEvent(frame.payload, frame.event, undefined)
        } catch { /* Observation cannot change the production transport. */ }
      })
    }
    try {
      const frame = typeof data === 'string' ? JSON.parse(data) : null
      if (frame?.type === 'req' && ['chat.send', 'chat.abort'].includes(frame.method)) {
        const params = frame.params || {}
        probe.requests.push({ method: frame.method, params: {
          sessionKey: params.sessionKey, taskId: params.taskId,
          source: params.source, scope: params.scope,
          // Only these explicitly synthetic messages are retained as evidence.
          message: typeof params.message === 'string' && /^(Retained profile|Read the audit sentinel|Hold this response)/.test(params.message)
            ? params.message : undefined,
        } })
      }
    } catch { /* Observation cannot change the production transport. */ }
    return originalSend.call(this, data)
  }
}
