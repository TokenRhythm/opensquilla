import type { InjectionKey } from 'vue'
import type { CronJob, CronRun, DeliveryConfig } from '@/types/cron'

export type CronSchedule =
  | { readonly kind: 'cron'; readonly expr: string; readonly tz?: string }
  | { readonly kind: 'every'; readonly every_seconds: number }
  | { readonly kind: 'at'; readonly at: string }

export interface CronJobMutation {
  id?: string
  name: string
  enabled: boolean
  schedule?: CronSchedule
  payloadKind: string
  agentId: string
  sessionTarget: string
  text: string
  workspaceId: string
  templateId: string
  tz?: string
  wakeMode?: string
  delivery?: DeliveryConfig
  sessionKey?: string
  targetSessionKey?: string
  originSessionKey?: string
}

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
  saveJob(input: CronJobMutation, options: { readonly existing: boolean }): Promise<void>
  setEnabled(jobId: string, enabled: boolean): Promise<void>
  runNow(jobId: string): Promise<CronRunOutcome>
  remove(jobId: string): Promise<void>
  listRuns(jobId: string, limit?: number): Promise<readonly CronRun[]>
  subscribe(listener: (event: CronRunFinished) => void): CronSubscription
}

export const CRON_SCHEDULER_KEY: InjectionKey<CronScheduler> = Symbol('CronScheduler')
