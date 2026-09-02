import type { RpcCallOptions } from '@/lib/rpc'
import type {
  CronRunFinished,
  CronScheduler,
  CronSubscription,
} from '@/modules/cronScheduler'
import type { CronJob, CronRun } from '@/types/cron'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { timeoutMs?: number; signal?: AbortSignal }): Promise<void>
}

interface EventTransport {
  subscribe(event: string, handler: (payload: unknown) => void): { close(): void }
}

const rows = <T>(value: unknown, key: string): readonly T[] => {
  if (Array.isArray(value)) return value as T[]
  if (!value || typeof value !== 'object') return []
  const nested = (value as Record<string, unknown>)[key]
  return Array.isArray(nested) ? nested as T[] : []
}

export function createV4CronScheduler(
  rpc: RpcTransport,
  events: EventTransport,
): CronScheduler {
  const listeners = new Set<(event: CronRunFinished) => void>()
  let wireSubscription: { close(): void } | null = null

  const startLease = (): void => {
    if (wireSubscription) return
    wireSubscription = events.subscribe('cron.run.finished', payload => {
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return
      const event = payload as CronRunFinished
      for (const listener of [...listeners]) listener(event)
    })
    void rpc.request('cron.subscribe').catch(() => undefined)
  }

  const stopLease = (): void => {
    if (listeners.size > 0 || !wireSubscription) return
    wireSubscription.close()
    wireSubscription = null
    void rpc.request('cron.unsubscribe').catch(() => undefined)
  }

  return {
    async listJobs() {
      await rpc.ready()
      return rows<CronJob>(await rpc.request('cron.list'), 'jobs')
    },
    async saveJob(input, options) {
      await rpc.request(options.existing ? 'cron.update' : 'cron.create', { ...input })
    },
    async setEnabled(jobId, enabled) {
      await rpc.request('cron.update', { id: jobId, enabled })
    },
    async runNow(jobId) {
      return rpc.request('cron.run', { id: jobId })
    },
    async remove(jobId) {
      await rpc.request('cron.remove', { id: jobId })
    },
    async listRuns(jobId, limit = 10) {
      return rows<CronRun>(await rpc.request('cron.runs', { id: jobId, limit }), 'runs')
    },
    ready(options) {
      return rpc.ready({ timeoutMs: options?.timeoutMs })
    },
    async resumeEvents() {
      if (listeners.size > 0) await rpc.request('cron.subscribe')
    },
    subscribe(listener): CronSubscription {
      listeners.add(listener)
      startLease()
      let closed = false
      return {
        close() {
          if (closed) return
          closed = true
          listeners.delete(listener)
          stopLease()
        },
      }
    },
  }
}
