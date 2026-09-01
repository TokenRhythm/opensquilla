import type { InjectionKey } from 'vue'
import type { CronJob, CronRun } from '@/types/cron'

export interface CronRunOutcome {
  readonly runId?: string
  readonly reply?: string
  readonly error?: string
}

export interface CronRunFinished {
  readonly jobId?: string
  readonly jobName?: string
  readonly payloadKind?: string
  readonly runId?: string
  readonly sessionKey?: string
  readonly summary?: string
  readonly success?: boolean
}

export interface CronSubscription {
  close(): void
}

export interface CronScheduler {
  listJobs(): Promise<readonly CronJob[]>
  saveJob(input: Record<string, unknown>, options: { readonly existing: boolean }): Promise<void>
  setEnabled(jobId: string, enabled: boolean): Promise<void>
  runNow(jobId: string): Promise<CronRunOutcome>
  remove(jobId: string): Promise<void>
  listRuns(jobId: string, limit?: number): Promise<readonly CronRun[]>
  ready(options?: { readonly timeoutMs?: number }): Promise<void>
  resumeEvents(): Promise<void>
  subscribe(listener: (event: CronRunFinished) => void): CronSubscription
}

export const CRON_SCHEDULER_KEY: InjectionKey<CronScheduler> = Symbol('CronScheduler')
