import type { InjectionKey } from 'vue'

export interface TerminalCreateOptions {
  cols?: number
  rows?: number
  cwd?: string
  shell?: string
}

export interface TerminalHandle {
  terminalId: string
  sessionKey: string
  cols: number
  rows: number
  shell: string
}

export interface TerminalOutput {
  terminalId: string
  sessionKey?: string
  seq: number
  data: string
  encoding: 'utf8'
}

export interface TerminalExit {
  terminalId: string
  sessionKey?: string
  seq: number
  exitCode: number | null
  signal: string | null
  reason: string | null
}

export interface TerminalAccess {
  create(sessionKey: string, options?: TerminalCreateOptions, signal?: AbortSignal): Promise<TerminalHandle>
  write(terminalId: string, data: string, signal?: AbortSignal): Promise<{ terminalId: string; accepted: boolean; bytes: number }>
  resize(terminalId: string, cols: number, rows: number, signal?: AbortSignal): Promise<{ terminalId: string; cols: number; rows: number }>
  close(terminalId: string, reason?: string, signal?: AbortSignal): Promise<{ terminalId: string; closed: boolean }>
  onOutput(handler: (event: TerminalOutput) => void): () => void
  onExit(handler: (event: TerminalExit) => void): () => void
}

export const TERMINAL_ACCESS_KEY: InjectionKey<TerminalAccess> = Symbol('TerminalAccess')
