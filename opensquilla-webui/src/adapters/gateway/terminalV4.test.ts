import { describe, expect, it, vi } from 'vitest'
import { createV4TerminalAccess } from './terminalV4'

function fixture() {
  const rpc = {
    ready: vi.fn(async () => undefined),
    request: vi.fn(async <T = unknown>(method: string, _params?: Record<string, unknown>, _options?: unknown): Promise<T> => {
      if (method === 'terminal.create') return { terminalId: 'pty-1', sessionKey: 'session-a', cols: 80, rows: 24, shell: 'bash' } as T
      if (method === 'terminal.write') return { terminalId: 'pty-1', accepted: true, bytes: 3 } as T
      if (method === 'terminal.resize') return { terminalId: 'pty-1', cols: 100, rows: 30 } as T
      return { terminalId: 'pty-1', closed: true } as T
    }),
  }
  const handlers = new Map<string, (payload: unknown) => void>()
  const events = { subscribe: vi.fn((name: string, handler: (payload: unknown) => void) => {
    handlers.set(name, handler)
    return { close: () => handlers.delete(name) }
  }) }
  return {
    rpc,
    events,
    access: createV4TerminalAccess(rpc as unknown as Parameters<typeof createV4TerminalAccess>[0], events),
    handlers,
  }
}

describe('terminal Gateway adapter', () => {
  it('uses the shared RPC transport for create/write/resize/close', async () => {
    const h = fixture()
    const signal = new AbortController().signal
    await expect(h.access.create('session-a', { cols: 80, rows: 24 }, signal)).resolves.toMatchObject({ terminalId: 'pty-1' })
    await h.access.write('pty-1', 'pwd', signal)
    await h.access.resize('pty-1', 100, 30, signal)
    await h.access.close('pty-1', 'test', signal)
    expect(h.rpc.ready).toHaveBeenCalledWith({ signal })
    expect(h.rpc.request.mock.calls.map(call => call[0])).toEqual([
      'terminal.create', 'terminal.write', 'terminal.resize', 'terminal.close',
    ])
    expect(h.rpc.request.mock.calls[0]?.[1]).toMatchObject({ sessionKey: 'session-a', cols: 80, rows: 24 })
    expect(h.rpc.request.mock.calls[3]?.[1]).toMatchObject({ terminalId: 'pty-1', reason: 'test' })
  })

  it('projects output and exit events while dropping malformed frames', () => {
    const h = fixture()
    const output = vi.fn()
    const exit = vi.fn()
    const stopOutput = h.access.onOutput(output)
    const stopExit = h.access.onExit(exit)
    h.handlers.get('terminal.output')?.({ terminalId: 'pty-1', seq: 1, data: 'ok', encoding: 'utf8' })
    h.handlers.get('terminal.output')?.({ terminalId: 'pty-1', seq: 2, data: 4, encoding: 'utf8' })
    h.handlers.get('terminal.exit')?.({ terminalId: 'pty-1', seq: 3, exitCode: 0, signal: null, reason: null })
    expect(output).toHaveBeenCalledWith({ terminalId: 'pty-1', seq: 1, data: 'ok', encoding: 'utf8' })
    expect(output).toHaveBeenCalledTimes(1)
    expect(exit).toHaveBeenCalledWith({ terminalId: 'pty-1', seq: 3, exitCode: 0, signal: null, reason: null })
    stopOutput(); stopExit()
    expect(h.events.subscribe).toHaveBeenCalledTimes(2)
  })

  it('rejects unsafe writes and terminal dimensions before sending', async () => {
    const h = fixture()
    await expect(h.access.write('pty-1', 'x'.repeat(64 * 1024 + 1))).rejects.toThrow()
    await expect(h.access.resize('pty-1', 0, 24)).rejects.toThrow()
    await expect(h.access.close('pty-1', '')).rejects.toThrow()
    expect(h.rpc.request).not.toHaveBeenCalled()
  })
})
