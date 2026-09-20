import type { InjectionKey } from 'vue'

export interface SessionProcess {
  executionId: string
  taskId: string | null
  command: string
  status: 'running' | 'done' | 'killed' | 'timed_out'
  returncode: number | null
  startedAt: number
  endedAt: number | null
}

export interface SessionProcessIdentity {
  sessionKey: string
  sessionId: string
  sessionEpoch: number
}

export interface SessionProcessLog extends SessionProcessIdentity {
  executionId: string
  status: SessionProcess['status']
  output: string
  truncated: boolean
}

export interface SessionProcesses {
  readonly available: boolean
  readonly canStop: boolean
  list(sessionKey: string): Promise<SessionProcessIdentity & { processes: SessionProcess[] }>
  log(sessionKey: string, executionId: string): Promise<SessionProcessLog>
  stop(sessionKey: string, executionId: string): Promise<SessionProcessIdentity & { process: SessionProcess }>
}

export const SESSION_PROCESSES_KEY: InjectionKey<SessionProcesses> = Symbol('SessionProcesses')
