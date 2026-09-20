import { describe, expect, it, vi } from 'vitest'
import { createV4SessionProcesses } from './sessionProcessesV4'

const rawProcess = { execution_id: 'p1', task_id: 'old-turn', command: 'python server.py', status: 'running', returncode: null, started_at: 10, ended_at: null }
const identity = { session_key: 'a', session_id: 'durable-a', session_epoch: 2 }
function harness(scopes = ['operator.admin']) {
  const request = vi.fn().mockResolvedValue({ ...identity, processes: [rawProcess] })
  const auth = { principal: { authState: 'authenticated', authenticated: false, scopes, capabilities: [] as string[] } }
  const supports = vi.fn(() => true)
  const port = createV4SessionProcesses({ request, supports }, { getAuth: () => auth })
  return { port, request, auth, supports }
}

describe('managed process Gateway adapter', () => {
  it('projects process and owner identities without confusing execution and session IDs', async () => {
    const h = harness()
    expect(h.port.available).toBe(true)
    expect(h.port.canStop).toBe(true)
    const result = await h.port.list('a')
    expect(h.request).toHaveBeenCalledWith('sessions.processes.list', { sessionKey: 'a' })
    expect(result.sessionId).toBe('durable-a')
    expect(result.processes[0]?.executionId).toBe('p1')
    expect(result.processes[0]?.returncode).toBeNull()
  })

  it('bounds logs and validates the requested execution owner', async () => {
    const h = harness()
    h.request.mockResolvedValue({ ...identity, execution_id: 'p1', status: 'running', output: 'ready', truncated: true })
    await expect(h.port.log('a', 'p1')).resolves.toMatchObject({ output: 'ready', truncated: true })
    expect(h.request).toHaveBeenCalledWith('sessions.processes.log', { sessionKey: 'a', executionId: 'p1', limit: 12000 })
    await expect(h.port.log('a', 'other')).rejects.toThrow('Invalid process output')
  })

  it('only permits stop for write/admin scope, including a local unauthenticated admin owner', async () => {
    const h = harness(['operator.read'])
    expect(h.port.available).toBe(true)
    expect(h.port.canStop).toBe(false)
    await expect(h.port.stop('a', 'p1')).rejects.toThrow('not permitted')
    expect(h.request).not.toHaveBeenCalled()
    h.auth.principal.scopes = ['operator.write']
    h.request.mockResolvedValue({ ...identity, process: { ...rawProcess, status: 'killed', ended_at: 20 } })
    await expect(h.port.stop('a', 'p1')).resolves.toMatchObject({ process: { status: 'killed' } })
    expect(h.request).toHaveBeenCalledWith('sessions.processes.stop', { sessionKey: 'a', executionId: 'p1' })
  })

  it('hides the feature for older gateways and guest authorities', () => {
    const h = harness()
    h.supports.mockReturnValue(false)
    expect(h.port.available).toBe(false)
    expect(h.port.canStop).toBe(false)
    h.supports.mockReturnValue(true)
    h.auth.principal.capabilities = ['guest.safe']
    expect(h.port.available).toBe(false)
    expect(h.port.canStop).toBe(false)
  })

  it('rejects malformed or cross-session snapshots', async () => {
    const h = harness()
    await expect(h.port.list('other')).rejects.toThrow('Invalid process snapshot')
    h.request.mockResolvedValue({ ...identity, processes: [{ ...rawProcess, status: 'success' }] })
    await expect(h.port.list('a')).rejects.toThrow('Invalid process snapshot')
  })
})
