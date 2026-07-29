import { performance } from 'node:perf_hooks'
import { normalize, resolve } from 'node:path'

import {
  desktopGatewayStartIdentityConflict,
  desktopProcessStartIdentity,
  loadDesktopGatewayOwnershipRecord,
  verifyDesktopGatewayOwnership,
  type DesktopGatewayOwnershipRecord,
  type DesktopGatewayOwnershipRecordLoad,
} from './desktop-gateway-ownership.js'

const DEFAULT_IDENTITY_READY_TIMEOUT_MS = 45_000
const DEFAULT_POLL_INTERVAL_MS = 250
const DEFAULT_CHALLENGE_TIMEOUT_MS = 750
const DEFAULT_MAX_RECORD_BUDGETS_PER_DIRECTORY = 8

interface OwnershipVerificationState {
  deadlineMs: number
  inFlight: Promise<boolean> | null
}

interface OwnershipRecoveryFlight {
  recordKey: string
  promise: Promise<void>
}

export interface DesktopGatewayOwnershipVerificationOptions {
  identityReadyTimeoutMs?: number
  pollIntervalMs?: number
  challengeTimeoutMs?: number
  maxRecordBudgetsPerDirectory?: number
  now?: () => number
  wait?: (timeoutMs: number) => Promise<void>
  verify?: (record: DesktopGatewayOwnershipRecord, timeoutMs: number) => Promise<boolean>
  load?: (ownershipDir: string) => DesktopGatewayOwnershipRecordLoad
  processMayStillBeAlive?: (pid: number) => boolean
  processStartIdentity?: (pid: number) => string | null
  startIdentityConflicts?: (recorded: string, live: string | null) => boolean
  onPidRecycled?: (record: DesktopGatewayOwnershipRecord) => void
}

function processIdMayStillBeAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    // EPERM still proves that a process occupies the PID. Only ESRCH is a
    // reliable negative across supported Node platforms.
    return (error as NodeJS.ErrnoException).code !== 'ESRCH'
  }
}

function ownershipRecordKey(
  ownershipDir: string,
  record: DesktopGatewayOwnershipRecord,
): string {
  // Include every persisted field. A replacement record — even one reusing a
  // PID, port, or nonce — is a distinct instance and receives its own budget.
  return JSON.stringify([
    ownershipDirectoryKey(ownershipDir),
    record.schema_version,
    record.protocol,
    record.profile_fingerprint,
    record.pid,
    record.start_identity,
    record.port,
    record.version,
    record.instance_nonce,
  ])
}

function ownershipDirectoryKey(ownershipDir: string): string {
  const resolved = normalize(resolve(ownershipDir))
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved
}

/**
 * Coordinates readiness verification for Desktop Gateway ownership records.
 *
 * Every exact record instance receives one process-local readiness budget.
 * Concurrent callers share one poll, while later sequential callers still
 * perform a fresh identity challenge and liveness check without resetting the
 * deadline. Verification results never grant or cache shutdown authority.
 */
export class DesktopGatewayOwnershipVerificationCoordinator {
  private readonly states = new Map<string, OwnershipVerificationState>()
  private readonly budgetKeysByDirectory = new Map<string, Set<string>>()
  private readonly recoveryFlights = new Map<string, OwnershipRecoveryFlight>()
  private readonly identityReadyTimeoutMs: number
  private readonly pollIntervalMs: number
  private readonly challengeTimeoutMs: number
  private readonly maxRecordBudgetsPerDirectory: number
  private readonly now: () => number
  private readonly wait: (timeoutMs: number) => Promise<void>
  private readonly verify: (
    record: DesktopGatewayOwnershipRecord,
    timeoutMs: number,
  ) => Promise<boolean>
  private readonly load: (ownershipDir: string) => DesktopGatewayOwnershipRecordLoad
  private readonly processMayStillBeAlive: (pid: number) => boolean
  private readonly processStartIdentity: (pid: number) => string | null
  private readonly startIdentityConflicts: (recorded: string, live: string | null) => boolean
  private readonly onPidRecycled: (record: DesktopGatewayOwnershipRecord) => void

  constructor(options: DesktopGatewayOwnershipVerificationOptions = {}) {
    this.identityReadyTimeoutMs = Math.max(
      0,
      options.identityReadyTimeoutMs ?? DEFAULT_IDENTITY_READY_TIMEOUT_MS,
    )
    this.pollIntervalMs = Math.max(1, options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS)
    this.challengeTimeoutMs = Math.max(
      1,
      options.challengeTimeoutMs ?? DEFAULT_CHALLENGE_TIMEOUT_MS,
    )
    this.maxRecordBudgetsPerDirectory = Math.max(
      1,
      options.maxRecordBudgetsPerDirectory ?? DEFAULT_MAX_RECORD_BUDGETS_PER_DIRECTORY,
    )
    this.now = options.now ?? (() => performance.now())
    this.wait = options.wait ?? (
      (timeoutMs) => new Promise((resolveWait) => setTimeout(resolveWait, timeoutMs))
    )
    this.verify = options.verify ?? (
      (record, timeoutMs) => verifyDesktopGatewayOwnership(record, { timeoutMs })
    )
    this.load = options.load ?? loadDesktopGatewayOwnershipRecord
    this.processMayStillBeAlive = options.processMayStillBeAlive ?? processIdMayStillBeAlive
    this.processStartIdentity = options.processStartIdentity ?? desktopProcessStartIdentity
    this.startIdentityConflicts = (
      options.startIdentityConflicts ?? desktopGatewayStartIdentityConflict
    )
    this.onPidRecycled = options.onPidRecycled ?? (() => {})
  }

  async verifyWhenReady(
    ownershipDir: string,
    record: DesktopGatewayOwnershipRecord,
  ): Promise<boolean> {
    const [key, state] = this.stateFor(ownershipDir, record)
    if (state.inFlight !== null) return await state.inFlight

    const inFlight = this.pollUntilReady(ownershipDir, record, key, state.deadlineMs)
    state.inFlight = inFlight
    try {
      return await inFlight
    } finally {
      if (state.inFlight === inFlight) state.inFlight = null
    }
  }

  async runRecovery(
    ownershipDir: string,
    record: DesktopGatewayOwnershipRecord,
    operation: (currentRecord: DesktopGatewayOwnershipRecord) => Promise<void>,
  ): Promise<void> {
    const directoryKey = ownershipDirectoryKey(ownershipDir)
    const targetRecordKey = ownershipRecordKey(ownershipDir, record)
    while (true) {
      const existing = this.recoveryFlights.get(directoryKey)
      if (existing !== undefined) {
        if (existing.recordKey === targetRecordKey) {
          await existing.promise
          return
        }
        try {
          await existing.promise
        } catch {
          // A different exact record owns the error. The caller's target is
          // revalidated below after the directory becomes idle.
        }
        continue
      }

      const loaded = this.load(ownershipDir)
      if (
        loaded.status !== 'valid'
        || ownershipRecordKey(ownershipDir, loaded.record) !== targetRecordKey
      ) return

      const promise = operation(loaded.record)
      const flight = { recordKey: targetRecordKey, promise }
      this.recoveryFlights.set(directoryKey, flight)
      try {
        await promise
        return
      } finally {
        if (this.recoveryFlights.get(directoryKey) === flight) {
          this.recoveryFlights.delete(directoryKey)
        }
      }
    }
  }

  private stateFor(
    ownershipDir: string,
    record: DesktopGatewayOwnershipRecord,
  ): [string, OwnershipVerificationState] {
    const key = ownershipRecordKey(ownershipDir, record)
    let state = this.states.get(key)
    if (state === undefined) {
      const directoryKey = ownershipDirectoryKey(ownershipDir)
      let budgetKeys = this.budgetKeysByDirectory.get(directoryKey)
      if (budgetKeys === undefined) {
        budgetKeys = new Set()
        this.budgetKeysByDirectory.set(directoryKey, budgetKeys)
      }
      if (budgetKeys.size >= this.maxRecordBudgetsPerDirectory) {
        // Abnormal record churn must not create unbounded memory or a chain of
        // fresh 45-second waits. Keep the mandatory fresh challenge/liveness
        // checks, but give additional unknown records an already-expired budget.
        return [
          key,
          {
            deadlineMs: this.now(),
            inFlight: null,
          },
        ]
      }
      state = {
        deadlineMs: this.now() + this.identityReadyTimeoutMs,
        inFlight: null,
      }
      this.states.set(key, state)
      budgetKeys.add(key)
    }
    return [key, state]
  }

  private async pollUntilReady(
    ownershipDir: string,
    record: DesktopGatewayOwnershipRecord,
    expectedKey: string,
    deadlineMs: number,
  ): Promise<boolean> {
    let startIdentityChecked = false
    while (true) {
      // Always challenge first, including after the shared budget expires. This
      // lets a later startup phase recover an orphan that became ready after
      // the original poll, without granting authority from a cached result.
      if (await this.verify(record, this.challengeTimeoutMs)) return true

      const current = this.load(ownershipDir)
      if (
        current.status !== 'valid'
        || ownershipRecordKey(ownershipDir, current.record) !== expectedKey
        || !this.processMayStillBeAlive(record.pid)
      ) return false

      if (!startIdentityChecked) {
        startIdentityChecked = true
        const liveStartIdentity = this.processStartIdentity(record.pid)
        if (this.startIdentityConflicts(record.start_identity, liveStartIdentity)) {
          this.onPidRecycled(record)
          return false
        }
      }

      const remainingMs = deadlineMs - this.now()
      if (remainingMs <= 0) return false
      await this.wait(Math.min(this.pollIntervalMs, remainingMs))
    }
  }
}
