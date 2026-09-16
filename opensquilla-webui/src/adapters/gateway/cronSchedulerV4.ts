import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { CRON_LIST_METHOD, type Result as CronListResult } from '@/contracts/generated/v4/cronList'
import { validateResult as validateCronListResult } from '@/contracts/generated/v4/cronListValidators.mjs'
import { CRON_CREATE_METHOD, type Result as CronCreateResult } from '@/contracts/generated/v4/cronCreate'
import { validateResult as validateCronCreateResult } from '@/contracts/generated/v4/cronCreateValidators.mjs'
import { CRON_UPDATE_METHOD, type Result as CronUpdateResult } from '@/contracts/generated/v4/cronUpdate'
import { validateResult as validateCronUpdateResult } from '@/contracts/generated/v4/cronUpdateValidators.mjs'
import { CRON_RUN_METHOD, type Result as CronRunResult } from '@/contracts/generated/v4/cronRun'
import { validateResult as validateCronRunResult } from '@/contracts/generated/v4/cronRunValidators.mjs'
import { CRON_REMOVE_METHOD, type Result as CronRemoveResult } from '@/contracts/generated/v4/cronRemove'
import { validateResult as validateCronRemoveResult } from '@/contracts/generated/v4/cronRemoveValidators.mjs'
import { CRON_RUNS_METHOD, type Result as CronRunsResult } from '@/contracts/generated/v4/cronRuns'
import { validateResult as validateCronRunsResult } from '@/contracts/generated/v4/cronRunsValidators.mjs'
import { CRON_SUBSCRIBE_METHOD, type Result as CronSubscribeResult } from '@/contracts/generated/v4/cronSubscribe'
import { validateResult as validateCronSubscribeResult } from '@/contracts/generated/v4/cronSubscribeValidators.mjs'
import { CRON_UNSUBSCRIBE_METHOD, type Result as CronUnsubscribeResult } from '@/contracts/generated/v4/cronUnsubscribe'
import { validateResult as validateCronUnsubscribeResult } from '@/contracts/generated/v4/cronUnsubscribeValidators.mjs'
import {
  CRON_RUN_FINISHED_EVENT,
  type Payload as CronRunFinishedPayload,
} from '@/contracts/generated/v4/cronRunFinishedEvent'
import { validatePayload as validateCronRunFinishedPayload } from '@/contracts/generated/v4/cronRunFinishedEventValidators.mjs'
import type {
  CronRunFinished,
  CronScheduler,
  CronSubscription,
} from '@/modules/cronScheduler'
import type { CronJob, CronRun } from '@/types/cron'

interface RpcTransport {
  readonly generation: number
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: RpcCallOptions): Promise<void>
}

interface EventTransport {
  subscribe(event: string, handler: (payload: unknown) => void): { close(): void }
}

const PRIVATE_STATE_EVENT = '_state'
const LEASE_OPTIONS: RpcCallOptions = {
  timeoutMs: 10_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
}

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

export function createV4CronScheduler(
  rpc: RpcTransport,
  events: EventTransport,
): CronScheduler {
  const listeners = new Set<(event: CronRunFinished) => void>()
  let wireSubscription: { close(): void } | null = null
  let stateSubscription: { close(): void } | null = null
  let boundGeneration: number | null = null
  let bindWork: Promise<void> | null = null

  const bindLease = async (): Promise<void> => {
    if (listeners.size === 0 || boundGeneration === rpc.generation) return
    if (bindWork) return bindWork
    let work!: Promise<void>
    work = (async () => {
      await rpc.ready(LEASE_OPTIONS)
      if (listeners.size === 0) return
      const generation = rpc.generation
      const result = await rpc.request<CronSubscribeResult>(
        CRON_SUBSCRIBE_METHOD,
        {},
        { ...LEASE_OPTIONS, expectedGeneration: generation },
      )
      if (!validateCronSubscribeResult(result) || !result.ok) {
        throw invalid(CRON_SUBSCRIBE_METHOD)
      }
      if (rpc.generation === generation) {
        boundGeneration = generation
        // The last local owner can close while the subscribe request is in
        // flight. Release the now-orphaned server lease as soon as the bind
        // is acknowledged instead of waiting for another lifecycle event.
        if (listeners.size === 0) releaseLease()
      }
    })().finally(() => {
      if (bindWork === work) bindWork = null
    })
    bindWork = work
    return work
  }

  const releaseLease = (): void => {
    const generation = boundGeneration
    boundGeneration = null
    if (generation === null || rpc.generation !== generation) return
    void rpc.request<CronUnsubscribeResult>(
      CRON_UNSUBSCRIBE_METHOD,
      {},
      { ...LEASE_OPTIONS, expectedGeneration: generation },
    ).then(result => {
      if (!validateCronUnsubscribeResult(result)) throw invalid(CRON_UNSUBSCRIBE_METHOD)
    }).catch(() => undefined)
  }

  const startLease = (): void => {
    if (!wireSubscription) {
      wireSubscription = events.subscribe(CRON_RUN_FINISHED_EVENT, payload => {
        if (!validateCronRunFinishedPayload(payload)) return
        const event = payload as CronRunFinishedPayload
        for (const listener of [...listeners]) listener(event as CronRunFinished)
      })
    }
    if (!stateSubscription) {
      stateSubscription = events.subscribe(PRIVATE_STATE_EVENT, state => {
        if (state === 'connected') void bindLease().catch(() => undefined)
        else boundGeneration = null
      })
    }
    void bindLease().catch(() => undefined)
  }

  const stopLease = (): void => {
    if (listeners.size > 0) return
    releaseLease()
    wireSubscription?.close()
    stateSubscription?.close()
    wireSubscription = null
    stateSubscription = null
  }

  return {
    async listJobs() {
      await rpc.ready()
      const result = await rpc.request<CronListResult>(CRON_LIST_METHOD)
      if (!validateCronListResult(result)) throw invalid(CRON_LIST_METHOD)
      return result as CronJob[]
    },
    async saveJob(input, options) {
      const method = options.existing ? CRON_UPDATE_METHOD : CRON_CREATE_METHOD
      const result = options.existing
        ? await rpc.request<CronUpdateResult>(method, { ...input })
        : await rpc.request<CronCreateResult>(method, { ...input })
      const valid = options.existing
        ? validateCronUpdateResult(result)
        : validateCronCreateResult(result)
      if (!valid) throw invalid(method)
    },
    async setEnabled(jobId, enabled) {
      const result = await rpc.request<CronUpdateResult>(
        CRON_UPDATE_METHOD,
        { id: jobId, enabled },
      )
      if (!validateCronUpdateResult(result)) throw invalid(CRON_UPDATE_METHOD)
    },
    async runNow(jobId) {
      const result = await rpc.request<CronRunResult>(CRON_RUN_METHOD, { id: jobId })
      if (!validateCronRunResult(result)) throw invalid(CRON_RUN_METHOD)
      return result
    },
    async remove(jobId) {
      const result = await rpc.request<CronRemoveResult>(CRON_REMOVE_METHOD, { id: jobId })
      if (!validateCronRemoveResult(result)) throw invalid(CRON_REMOVE_METHOD)
    },
    async listRuns(jobId, limit = 10) {
      const result = await rpc.request<CronRunsResult>(
        CRON_RUNS_METHOD,
        { id: jobId, limit },
      )
      if (!validateCronRunsResult(result)) throw invalid(CRON_RUNS_METHOD)
      return result as CronRun[]
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
