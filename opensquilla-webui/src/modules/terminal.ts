import type { InjectionKey } from 'vue'

export type TerminalTarget =
  | { kind: 'default-agent'; agentId: string }
  | { kind: 'project'; workspaceId: string }
  | { kind: 'session'; sessionKey: string }

export function normalizeTerminalTarget(value: unknown): TerminalTarget | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const target = value as Record<string, unknown>
  const field = target.kind === 'default-agent' ? 'agentId' : target.kind === 'project' ? 'workspaceId' : target.kind === 'session' ? 'sessionKey' : null
  const id = field && typeof target[field] === 'string' ? (target[field] as string).trim() : ''
  if (!field || !id) return null
  if (target.kind === 'default-agent') return { kind: 'default-agent', agentId: id }
  if (target.kind === 'project') return { kind: 'project', workspaceId: id }
  return { kind: 'session', sessionKey: id }
}

export function terminalTargetKey(target: TerminalTarget): string {
  if (target.kind === 'default-agent') return `default-agent:${target.agentId}`
  if (target.kind === 'project') return `project:${target.workspaceId}`
  return `session:${target.sessionKey}`
}

export interface TerminalCreateOptions {
  cols?: number
  rows?: number
  cwd?: string
  shell?: string
  outputFlowControl?: 'consumed-v1'
}

export interface TerminalHandle {
  terminalId: string
  sessionKey?: string
  target?: TerminalTarget
  cwdPath?: string
  cols: number
  rows: number
  shell: string
  outputFlowControl?: 'consumed-v1'
}

export interface TerminalOutput {
  terminalId: string
  sessionKey?: string
  target?: TerminalTarget
  seq: number
  data: string
  encoding: 'utf8'
}

export interface TerminalExit {
  terminalId: string
  sessionKey?: string
  target?: TerminalTarget
  seq: number
  exitCode: number | null
  signal: string | null
  reason: string | null
}

export interface TerminalAccess {
  create(target: string | TerminalTarget, options?: TerminalCreateOptions, signal?: AbortSignal): Promise<TerminalHandle>
  write(terminalId: string, data: string, signal?: AbortSignal): Promise<{ terminalId: string; accepted: boolean; bytes: number }>
  resize(terminalId: string, cols: number, rows: number, signal?: AbortSignal): Promise<{ terminalId: string; cols: number; rows: number }>
  close(terminalId: string, reason?: string, signal?: AbortSignal): Promise<{ terminalId: string; closed: boolean }>
  /** Acknowledge xterm parser consumption, never mere event receipt. */
  consume?(terminalId: string, seq: number, signal?: AbortSignal): Promise<{ terminalId: string; consumedSeq: number }>
  onOutput(handler: (event: TerminalOutput) => void): () => void
  onExit(handler: (event: TerminalExit) => void): () => void
}

export const TERMINAL_ACCESS_KEY: InjectionKey<TerminalAccess> = Symbol('TerminalAccess')
