import { normalizeTerminalTarget, type TerminalAccess, type TerminalCreateOptions, type TerminalExit, type TerminalHandle, type TerminalOutput, type TerminalTarget } from '@/modules/terminal'
import type { TransportCallOptions, TransportEventHandler } from './transportTypes'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
  ready(options?: { signal?: AbortSignal }): Promise<void>
}

interface EventTransport {
  subscribe(event: string, handler: TransportEventHandler): { close(): void }
}

const METHODS = {
  create: 'terminal.create',
  write: 'terminal.write',
  resize: 'terminal.resize',
  close: 'terminal.close',
  consume: 'terminal.consume',
  output: 'terminal.output',
  exit: 'terminal.exit',
} as const

const requestOptions = (signal?: AbortSignal): TransportCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  recoveryClass: 'ephemeral',
  ...(signal ? { signal } : {}),
})

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function positiveInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null
}

function parseCreate(value: unknown): TerminalHandle {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  const sessionKey = text(source?.sessionKey ?? source?.session_key)
  const target = normalizeTerminalTarget(source?.target)
  const cwdPath = text(source?.cwdPath ?? source?.cwd_path)
  const cols = positiveInt(source?.cols)
  const rows = positiveInt(source?.rows)
  const shell = text(source?.shell)
  if (!terminalId || (!sessionKey && !target) || (source?.target !== undefined && !target) || !cols || !rows || !shell) throw new Error('terminal.create returned an invalid response')
  return { terminalId, ...(sessionKey ? { sessionKey } : {}), ...(target ? { target } : {}), ...(cwdPath ? { cwdPath } : {}), cols, rows, shell,
    ...(source?.outputFlowControl === 'consumed-v1' ? { outputFlowControl: 'consumed-v1' as const } : {}) }
}

function parseWrite(value: unknown) {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  const accepted = source?.accepted
  const bytes = source?.bytes
  if (!terminalId || typeof accepted !== 'boolean' || typeof bytes !== 'number' || !Number.isSafeInteger(bytes) || bytes < 0) {
    throw new Error('terminal.write returned an invalid response')
  }
  return { terminalId, accepted, bytes }
}

function parseResize(value: unknown) {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  const cols = positiveInt(source?.cols)
  const rows = positiveInt(source?.rows)
  if (!terminalId || !cols || !rows) throw new Error('terminal.resize returned an invalid response')
  return { terminalId, cols, rows }
}

function parseClose(value: unknown) {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  if (!source || !terminalId || typeof source.closed !== 'boolean') throw new Error('terminal.close returned an invalid response')
  return { terminalId, closed: source.closed }
}

function parseOutput(value: unknown): TerminalOutput | null {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  const sessionKey = text(source?.sessionKey ?? source?.session_key)
  const target = normalizeTerminalTarget(source?.target)
  if (source?.target !== undefined && !target) return null
  const seq = source?.seq
  const data = source?.data
  if (!terminalId || typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq < 0 || typeof data !== 'string') return null
  if (source?.encoding !== undefined && source.encoding !== 'utf8') return null
  return { terminalId, ...(sessionKey ? { sessionKey } : {}), ...(target ? { target } : {}), seq, data, encoding: 'utf8' }
}

function parseExit(value: unknown): TerminalExit | null {
  const source = record(value)
  const terminalId = text(source?.terminalId ?? source?.terminal_id)
  const sessionKey = text(source?.sessionKey ?? source?.session_key)
  const target = normalizeTerminalTarget(source?.target)
  if (source?.target !== undefined && !target) return null
  const seq = source?.seq
  const exitCode = source?.exitCode ?? source?.exit_code
  const signal = source?.signal
  const reason = source?.reason
  if (!terminalId || typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq < 0) return null
  if (exitCode !== null && exitCode !== undefined && (!Number.isSafeInteger(exitCode) || typeof exitCode !== 'number')) return null
  if (signal !== null && signal !== undefined && typeof signal !== 'string') return null
  if (reason !== null && reason !== undefined && typeof reason !== 'string') return null
  const normalizedExitCode = exitCode === null || exitCode === undefined ? null : exitCode as number
  return {
    terminalId,
    ...(sessionKey ? { sessionKey } : {}),
    ...(target ? { target } : {}),
    seq,
    exitCode: normalizedExitCode,
    signal: signal === undefined ? null : signal,
    reason: reason === undefined ? null : reason,
  }
}

export function createV4TerminalAccess(rpc: RpcTransport, events: EventTransport): TerminalAccess {
  return {
    async create(requestedTarget: string | TerminalTarget, options: TerminalCreateOptions = {}, signal?: AbortSignal) {
      const target = normalizeTerminalTarget(typeof requestedTarget === 'string' ? { kind: 'session', sessionKey: requestedTarget } : requestedTarget)
      if (!target) throw new Error('a valid terminal target is required')
      await rpc.ready({ signal })
      return parseCreate(await rpc.request(METHODS.create, {
        ...(typeof requestedTarget === 'string' && target.kind === 'session' ? { sessionKey: target.sessionKey } : { target }),
        ...(options.cols !== undefined ? { cols: options.cols } : {}),
        ...(options.rows !== undefined ? { rows: options.rows } : {}),
        ...(options.cwd ? { cwd: options.cwd } : {}),
        ...(options.shell ? { shell: options.shell } : {}),
        ...(options.outputFlowControl ? { outputFlowControl: options.outputFlowControl } : {}),
      }, requestOptions(signal)))
    },
    async write(terminalId: string, data: string, signal?: AbortSignal) {
      if (!terminalId || typeof data !== 'string' || new TextEncoder().encode(data).byteLength > 64 * 1024) throw new Error('invalid terminal.write input')
      return parseWrite(await rpc.request(METHODS.write, { terminalId, data }, requestOptions(signal)))
    },
    async resize(terminalId: string, cols: number, rows: number, signal?: AbortSignal) {
      if (!terminalId || !Number.isSafeInteger(cols) || cols < 1 || cols > 400 || !Number.isSafeInteger(rows) || rows < 1 || rows > 200) throw new Error('invalid terminal.resize input')
      return parseResize(await rpc.request(METHODS.resize, { terminalId, cols, rows }, requestOptions(signal)))
    },
    async close(terminalId: string, reason = 'client', signal?: AbortSignal) {
      if (!terminalId || typeof reason !== 'string' || !reason || reason.length > 128) {
        throw new Error('invalid terminal.close input')
      }
      return parseClose(await rpc.request(METHODS.close, { terminalId, reason: reason.slice(0, 128) }, requestOptions(signal)))
    },
    async consume(terminalId: string, seq: number, signal?: AbortSignal) {
      if (!terminalId || !Number.isSafeInteger(seq) || seq < 1) throw new Error('invalid terminal.consume input')
      const source = record(await rpc.request(METHODS.consume, { terminalId, seq }, requestOptions(signal)))
      const consumedSeq = positiveInt(source?.consumedSeq)
      if (source?.terminalId !== terminalId || consumedSeq === null || consumedSeq < seq) throw new Error('terminal.consume returned an invalid response')
      return { terminalId, consumedSeq }
    },
    onOutput(handler) {
      const subscription = events.subscribe(METHODS.output, (payload: unknown) => {
        const parsed = parseOutput(payload)
        if (parsed) handler(parsed)
      })
      return () => subscription.close()
    },
    onExit(handler) {
      const subscription = events.subscribe(METHODS.exit, (payload: unknown) => {
        const parsed = parseExit(payload)
        if (parsed) handler(parsed)
      })
      return () => subscription.close()
    },
  }
}
