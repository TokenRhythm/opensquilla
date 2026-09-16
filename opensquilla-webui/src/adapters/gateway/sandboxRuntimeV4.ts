import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  SANDBOX_SETUP_STATUS_METHOD,
  type Result as SetupStatusResult,
} from '@/contracts/generated/v4/sandboxSetupStatus'
import { validateResult as validateSetupStatusResult } from '@/contracts/generated/v4/sandboxSetupStatusValidators.mjs'
import {
  SANDBOX_SETUP_ENSURE_METHOD,
  type Result as SetupEnsureResult,
} from '@/contracts/generated/v4/sandboxSetupEnsure'
import { validateResult as validateSetupEnsureResult } from '@/contracts/generated/v4/sandboxSetupEnsureValidators.mjs'
import {
  SANDBOX_CAPABILITY_STATUS_METHOD,
  type Result as CapabilityStatusResult,
} from '@/contracts/generated/v4/sandboxCapabilityStatus'
import { validateResult as validateCapabilityStatusResult } from '@/contracts/generated/v4/sandboxCapabilityStatusValidators.mjs'
import {
  SANDBOX_POLICY_GET_METHOD,
  type Policy as PolicyGetResult,
} from '@/contracts/generated/v4/sandboxPolicyGet'
import { validatePolicy as validatePolicyGetResult } from '@/contracts/generated/v4/sandboxPolicyGetValidators.mjs'
import {
  SANDBOX_POLICY_DEFAULTS_METHOD,
  type Result as PolicyDefaultsResult,
} from '@/contracts/generated/v4/sandboxPolicyDefaults'
import { validateResult as validatePolicyDefaultsResult } from '@/contracts/generated/v4/sandboxPolicyDefaultsValidators.mjs'
import {
  SANDBOX_POLICY_UPDATE_METHOD,
  type Policy as PolicyUpdateResult,
} from '@/contracts/generated/v4/sandboxPolicyUpdate'
import { validatePolicy as validatePolicyUpdateResult } from '@/contracts/generated/v4/sandboxPolicyUpdateValidators.mjs'
import {
  SANDBOX_RUN_MODE_PREFERENCE_GET_METHOD,
  type Result as RunModePreferenceGetResult,
} from '@/contracts/generated/v4/sandboxRunModePreferenceGet'
import { validateResult as validateRunModePreferenceGetResult } from '@/contracts/generated/v4/sandboxRunModePreferenceGetValidators.mjs'
import {
  SANDBOX_RUN_MODE_PREFERENCE_SET_METHOD,
  type Result as RunModePreferenceSetResult,
} from '@/contracts/generated/v4/sandboxRunModePreferenceSet'
import { validateResult as validateRunModePreferenceSetResult } from '@/contracts/generated/v4/sandboxRunModePreferenceSetValidators.mjs'
import {
  SANDBOX_RUN_MODE_PREFERENCE_CHANGED_EVENT,
  type Payload as RunModePreferenceChangedPayload,
} from '@/contracts/generated/v4/sandboxRunModePreferenceChanged'
import { validatePayload as validateRunModePreferenceChangedPayload } from '@/contracts/generated/v4/sandboxRunModePreferenceChangedValidators.mjs'
import {
  SANDBOX_RUNTIME_STATUS_METHOD,
  type Result as RuntimeStatusResult,
} from '@/contracts/generated/v4/sandboxRuntimeStatus'
import { validateResult as validateRuntimeStatusResult } from '@/contracts/generated/v4/sandboxRuntimeStatusValidators.mjs'
import {
  SANDBOX_RUNTIME_INSTALL_METHOD,
  type Result as RuntimeInstallResult,
} from '@/contracts/generated/v4/sandboxRuntimeInstall'
import { validateResult as validateRuntimeInstallResult } from '@/contracts/generated/v4/sandboxRuntimeInstallValidators.mjs'
import {
  SANDBOX_RUNTIME_CANCEL_METHOD,
  type Result as RuntimeCancelResult,
} from '@/contracts/generated/v4/sandboxRuntimeCancel'
import { validateResult as validateRuntimeCancelResult } from '@/contracts/generated/v4/sandboxRuntimeCancelValidators.mjs'
import {
  SANDBOX_RUNTIME_REMOVE_METHOD,
  type Result as RuntimeRemoveResult,
} from '@/contracts/generated/v4/sandboxRuntimeRemove'
import { validateResult as validateRuntimeRemoveResult } from '@/contracts/generated/v4/sandboxRuntimeRemoveValidators.mjs'
import {
  SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD,
  type Result as RuntimeDiscardDownloadResult,
} from '@/contracts/generated/v4/sandboxRuntimeDiscardDownload'
import { validateResult as validateRuntimeDiscardDownloadResult } from '@/contracts/generated/v4/sandboxRuntimeDiscardDownloadValidators.mjs'
import {
  SANDBOX_RESUME_METHOD,
  type Result as ResumeResult,
} from '@/contracts/generated/v4/sandboxResume'
import { validateResult as validateResumeResult } from '@/contracts/generated/v4/sandboxResumeValidators.mjs'
import {
  SandboxError,
  type SandboxReadinessState,
  type SandboxRequestOptions,
  type SandboxRunModePreference,
  type SandboxRuntime,
  type SandboxRuntimeActionReceipt,
  type SandboxSetupResult,
} from '@/modules/sandboxRuntime'
import {
  normalizeSandboxRunMode,
  type SandboxCapabilityReport,
  type SandboxPolicy,
  type SandboxPolicyDefaults,
  type SandboxRunMode,
  type SandboxRuntimeComponentId,
  type SandboxRuntimePackStatus,
  type SandboxRuntimeOperation,
  type SandboxSetupStatusPayload,
} from '@/types/sandbox'

interface SandboxRpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  ready(options?: RpcCallOptions): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

interface SandboxEventTransport {
  subscribe(
    event: string,
    handler: (payload: unknown) => void,
  ): { close(): void }
}

interface SandboxCallLifecycle {
  onRequestStart(): void
  onSent(): void
}

const DEFAULT_TIMEOUT_MS = 15_000
const SETUP_TIMEOUT_MS = 45_000
const SETUP_RECONCILE_POLL_MS = 1_000
const SETUP_RECONCILE_MAX_WAIT_MS = 120_000

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function requestOptions(
  options?: SandboxRequestOptions,
  onSent?: () => void,
): RpcCallOptions {
  return {
    timeoutMs: options?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    timeoutAction: 'reject',
    abortAction: 'reject',
    ...(options?.signal ? { signal: options.signal } : {}),
    ...(onSent ? { onSent } : {}),
  }
}

function waitForSetupPoll(signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(new SandboxError('aborted', 'Sandbox setup reconciliation was aborted'))
  }
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      reject(new SandboxError('aborted', 'Sandbox setup reconciliation was aborted'))
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, SETUP_RECONCILE_POLL_MS)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function rpcCode(error: unknown): string {
  const raw = objectValue(error)
  const data = objectValue(raw?.data)
  return text(raw?.code, data?.code).toUpperCase()
}

function rpcDetails(error: unknown): unknown {
  const raw = objectValue(error)
  const data = objectValue(raw?.data)
  return raw?.details ?? data?.details
}

function isMethodUnsupported(error: unknown): boolean {
  const code = rpcCode(error)
  return code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED'
}

function isTransportFailure(error: unknown): boolean {
  return rpcCode(error) === 'RPC_TRANSPORT_ERROR'
}

function isSetupResponseLoss(error: unknown): boolean {
  return isTransportFailure(error) || rpcCode(error) === 'RPC_TIMEOUT'
}

function setupRequestWasDefinitelyUnsent(
  error: unknown,
  requestStarted: boolean,
  requestSent: boolean,
): boolean {
  if (requestSent) return false
  if (!requestStarted) return true
  const raw = objectValue(error)
  const data = objectValue(raw?.data)
  const accepted = raw?.accepted ?? data?.accepted
  return isTransportFailure(error) && accepted === false
}

function isExplicitSetupRejection(error: unknown): boolean {
  if (error instanceof SandboxError) return false
  const code = rpcCode(error)
  return Boolean(code)
    && code !== 'RPC_TRANSPORT_ERROR'
    && code !== 'RPC_TIMEOUT'
    && code !== 'RPC_ABORTED'
    && code !== 'INTERNAL_ERROR'
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || rpcCode(error) === 'RPC_ABORTED'
    || (error instanceof Error && error.name === 'AbortError')
}

function canonicalSetupStatus(value: unknown): Record<string, unknown> | null {
  const raw = objectValue(value)
  if (!raw) return null
  const state = raw.state
  const requiresAdmin = raw.requiresAdmin ?? raw.requires_admin
  if (
    typeof state !== 'string'
    || !['not_setup', 'setting_up', 'ready', 'failed', 'unavailable'].includes(state)
  ) return null
  if (
    typeof raw.platform !== 'string'
    || typeof raw.message !== 'string'
    || typeof requiresAdmin !== 'boolean'
  ) return null
  return {
    state,
    platform: raw.platform,
    message: raw.message,
    requiresAdmin,
    ...(typeof raw.detail === 'string' ? { detail: raw.detail } : {}),
  }
}

function projectSetupStatus(
  value: unknown,
  valid: (candidate: unknown) => boolean,
  method: string,
): SandboxSetupStatusPayload {
  const canonical = canonicalSetupStatus(value)
  if (!canonical || !valid(canonical)) {
    throw new SandboxError('invalid', `${method} returned an invalid setup status`)
  }
  return canonical as unknown as SandboxSetupStatusPayload
}

function canonicalCapability(value: unknown): Record<string, unknown> | null {
  const raw = objectValue(value)
  if (!raw) return null
  const setupSupported = raw.setupSupported ?? raw.setup_supported
  const restartRequired = raw.restartRequired ?? raw.restart_required
  const probeVersion = raw.probeVersion ?? raw.probe_version
  if (
    typeof raw.available !== 'boolean'
    || typeof raw.backend !== 'string'
    || typeof raw.platform !== 'string'
    || typeof raw.code !== 'string'
    || typeof raw.reason !== 'string'
    || typeof setupSupported !== 'boolean'
    || typeof restartRequired !== 'boolean'
    || !Number.isInteger(probeVersion)
    || Number(probeVersion) < 0
    || !Array.isArray(raw.capabilities)
    || raw.capabilities.some(item => typeof item !== 'string')
  ) return null
  return {
    available: raw.available,
    backend: raw.backend,
    platform: raw.platform,
    code: raw.code,
    reason: raw.reason,
    setupSupported,
    restartRequired,
    probeVersion,
    capabilities: [...raw.capabilities],
  }
}

function projectCapability(value: unknown): SandboxCapabilityReport {
  const canonical = canonicalCapability(value)
  if (!canonical || !validateCapabilityStatusResult(canonical)) {
    throw new SandboxError(
      'invalid',
      `${SANDBOX_CAPABILITY_STATUS_METHOD} returned an invalid capability report`,
    )
  }
  return canonical as unknown as SandboxCapabilityReport
}

function projectPolicy(
  value: unknown,
  valid: (candidate: unknown) => boolean,
  method: string,
): SandboxPolicy {
  if (!valid(value)) {
    throw new SandboxError('invalid', `${method} returned an invalid sandbox policy`)
  }
  return clone(value as SandboxPolicy)
}

function projectDefaults(value: unknown): Partial<SandboxPolicyDefaults> {
  if (!validatePolicyDefaultsResult(value)) {
    throw new SandboxError(
      'invalid',
      `${SANDBOX_POLICY_DEFAULTS_METHOD} returned invalid sandbox defaults`,
    )
  }
  return clone(value as Partial<SandboxPolicyDefaults>)
}

function canonicalPreference(value: unknown): Record<string, unknown> | null {
  const raw = objectValue(value)
  if (!raw) return null
  const candidate = raw.runMode ?? raw.run_mode ?? raw.mode
  if (!['safe', 'full', 'standard', 'trusted', 'managed', 'bypass'].includes(String(candidate))) {
    return null
  }
  return {
    runMode: normalizeSandboxRunMode(candidate),
    ...(typeof raw.source === 'string' ? { source: raw.source } : {}),
  }
}

function projectPreference(
  value: unknown,
  valid: (candidate: unknown) => boolean,
  method: string,
): SandboxRunModePreference {
  const canonical = canonicalPreference(value)
  if (!canonical || !valid(canonical)) {
    throw new SandboxError('invalid', `${method} returned an invalid run-mode preference`)
  }
  return canonical as unknown as SandboxRunModePreference
}

function projectRuntimeStatus(value: unknown, method: string): SandboxRuntimePackStatus {
  if (!validateRuntimeStatusResult(value)) {
    throw new SandboxError('invalid', `${method} returned an invalid runtime status`)
  }
  const raw = objectValue(value)
  const status = objectValue(raw?.status) ?? objectValue(raw?.runtimeStatus) ?? raw
  if (!status) throw new SandboxError('invalid', `${method} returned an invalid runtime status`)
  return clone(status as unknown as SandboxRuntimePackStatus)
}

function projectOperationReceipt(
  value: unknown,
  componentId: SandboxRuntimeComponentId,
  valid: (candidate: unknown) => boolean,
  method: string,
): SandboxRuntimeActionReceipt {
  const raw = objectValue(value)
  const operation = objectValue(raw?.operation)
  if (!operation || !valid(value)) {
    throw new SandboxError('invalid', `${method} returned an invalid runtime operation`)
  }
  const projected = clone(operation as unknown as SandboxRuntimeOperation)
  if (projected.componentId !== componentId) {
    throw new SandboxError('invalid', `${method} returned an invalid runtime operation`)
  }
  return { kind: 'operation', operation: projected }
}

function projectStatusReceipt(value: unknown, method: string): SandboxRuntimeActionReceipt {
  const raw = objectValue(value)
  const status = objectValue(raw?.status)
  if (!status || !validateRuntimeDiscardDownloadResult(value)) {
    throw new SandboxError('invalid', `${method} returned an invalid runtime status`)
  }
  return { kind: 'status', status: clone(status as unknown as SandboxRuntimePackStatus) }
}

function policyFromConflict(details: unknown): SandboxPolicy | undefined {
  const raw = objectValue(details)
  const candidate = raw?.currentPolicy ?? raw?.current_policy
  return validatePolicyGetResult(candidate) ? clone(candidate as SandboxPolicy) : undefined
}

function mapError(error: unknown, signal?: AbortSignal): SandboxError {
  if (error instanceof SandboxError) return error
  const raw = objectValue(error)
  const code = rpcCode(error)
  const details = rpcDetails(error)
  const message = text(raw?.message) || (error instanceof Error ? error.message : 'Sandbox request failed')
  const retryable = raw?.retryable === true
  if (isAbort(error, signal)) return new SandboxError('aborted', message, { cause: error })
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') {
    return new SandboxError('unsupported', message, { details, cause: error })
  }
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN' || code === 'OWNER_REQUIRED') {
    return new SandboxError('forbidden', message, { details, cause: error })
  }
  if (code === 'POLICY_VERSION_CONFLICT') {
    return new SandboxError('conflict', message, {
      details,
      currentPolicy: policyFromConflict(details),
      retryable: true,
      cause: error,
    })
  }
  if (code === 'CONFLICT' || code === 'RUNTIME_JOB_CONFLICT') {
    return new SandboxError('conflict', message, { details, retryable: true, cause: error })
  }
  if (code === 'SANDBOX_CAPABILITY_UNAVAILABLE') {
    return new SandboxError('setup_required', message, { details, cause: error })
  }
  if (code === 'STORAGE_BUSY') {
    return new SandboxError('busy', message, { details, retryable: true, cause: error })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS' || code === 'BAD_REQUEST') {
    return new SandboxError('invalid', message, { details, cause: error })
  }
  if (code === 'RPC_TRANSPORT_ERROR' || code === 'RPC_TIMEOUT' || code === 'UNAVAILABLE') {
    return new SandboxError('unavailable', message, { details, retryable: true, cause: error })
  }
  return new SandboxError('failed', message, { details, retryable, cause: error })
}

function setupOutcome(status: SandboxSetupStatusPayload | null): SandboxSetupResult['outcome'] {
  if (!status) return 'failed'
  if (status.state === 'setting_up') return 'in_progress'
  if (status.state === 'ready') return 'verification_failed'
  return status.detail?.toLowerCase().includes('cancel') ? 'cancelled' : 'failed'
}

export function createV4SandboxRuntime(
  rpc: SandboxRpcTransport,
  events: SandboxEventTransport,
): SandboxRuntime {
  let setupInFlight: Promise<SandboxSetupResult> | null = null
  let setupMutationAttempted = false

  const call = async <T>(
    method: string,
    params?: Record<string, unknown>,
    options?: SandboxRequestOptions,
    skipReady = false,
    lifecycle?: SandboxCallLifecycle,
  ): Promise<T> => {
    if (!skipReady) {
      await rpc.ready({
        timeoutMs: options?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        timeoutAction: 'reject',
        abortAction: 'reject',
        ...(options?.signal ? { signal: options.signal } : {}),
      })
    }
    lifecycle?.onRequestStart()
    return await rpc.request<T>(method, params, requestOptions(options, lifecycle?.onSent))
  }

  const readSetupStatus = async (
    options?: SandboxRequestOptions,
    skipReady = false,
  ): Promise<SandboxSetupStatusPayload | null> => {
    if (!rpc.supports(SANDBOX_SETUP_STATUS_METHOD)) return null
    try {
      const value = await call<SetupStatusResult>(
        SANDBOX_SETUP_STATUS_METHOD,
        undefined,
        options,
        skipReady,
      )
      return projectSetupStatus(
        value,
        validateSetupStatusResult,
        SANDBOX_SETUP_STATUS_METHOD,
      )
    } catch (error) {
      if (isMethodUnsupported(error)) {
        rpc.markUnsupported(SANDBOX_SETUP_STATUS_METHOD)
        return null
      }
      throw mapError(error, options?.signal)
    }
  }

  const readCapability = async (
    refresh: boolean,
    options?: SandboxRequestOptions,
    skipReady = false,
  ): Promise<SandboxCapabilityReport | null> => {
    if (!rpc.supports(SANDBOX_CAPABILITY_STATUS_METHOD)) return null
    try {
      const value = await call<CapabilityStatusResult>(
        SANDBOX_CAPABILITY_STATUS_METHOD,
        refresh ? { refresh: true } : undefined,
        options,
        skipReady,
      )
      return projectCapability(value)
    } catch (error) {
      if (isMethodUnsupported(error)) {
        rpc.markUnsupported(SANDBOX_CAPABILITY_STATUS_METHOD)
        return null
      }
      throw mapError(error, options?.signal)
    }
  }

  const readReadiness = async (
    options?: SandboxRequestOptions & { refreshCapability?: boolean },
    skipReady = false,
  ): Promise<SandboxReadinessState> => {
    const status = await readSetupStatus(options, skipReady)
    const capability = status === null || status.state === 'ready'
      ? await readCapability(options?.refreshCapability === true, options, skipReady)
      : null
    return { status, capability }
  }

  const finishSetup = async (
    status: SandboxSetupStatusPayload | null,
    options?: SandboxRequestOptions,
    skipReady = false,
    ambiguous = false,
  ): Promise<SandboxSetupResult> => {
    if (!status || status.state !== 'ready') {
      return {
        ready: false,
        status,
        capability: null,
        outcome: ambiguous && status === null ? 'in_progress' : setupOutcome(status),
      }
    }
    try {
      const capability = await readCapability(true, options, skipReady)
      return capability?.available === true
        ? { ready: true, status, capability, outcome: 'ready' }
        : { ready: false, status, capability, outcome: 'verification_failed' }
    } catch (error) {
      if (isAbort(error, options?.signal)) throw mapError(error, options?.signal)
      return { ready: false, status, capability: null, outcome: 'verification_failed' }
    }
  }

  const waitForSetupCompletion = async (
    initialStatus: SandboxSetupStatusPayload | null,
    options?: SandboxRequestOptions,
  ): Promise<SandboxSetupStatusPayload | null> => {
    let status = initialStatus
    const deadline = Date.now() + SETUP_RECONCILE_MAX_WAIT_MS
    let delayBeforeRead = status !== null
    while (status === null || status.state === 'setting_up') {
      if (Date.now() >= deadline) return status
      if (delayBeforeRead) await waitForSetupPoll(options?.signal)
      try {
        const next = await readSetupStatus(options)
        if (next === null) return status
        status = next
        if (status.state !== 'setting_up') return status
      } catch (error) {
        if (isAbort(error, options?.signal)) throw mapError(error, options?.signal)
        if (!(error instanceof SandboxError && error.code === 'unavailable')) throw error
      }
      delayBeforeRead = true
    }
    return status
  }

  const preference = async (
    options?: SandboxRequestOptions,
  ): Promise<SandboxRunModePreference> => {
    try {
      const value = await call<RunModePreferenceGetResult>(
        SANDBOX_RUN_MODE_PREFERENCE_GET_METHOD,
        undefined,
        options,
      )
      return projectPreference(
        value,
        validateRunModePreferenceGetResult,
        SANDBOX_RUN_MODE_PREFERENCE_GET_METHOD,
      )
    } catch (error) {
      throw mapError(error, options?.signal)
    }
  }

  const runtimeOperation = async (
    method: string,
    params: Record<string, unknown>,
    componentId: SandboxRuntimeComponentId,
    valid: (candidate: unknown) => boolean,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRuntimeActionReceipt> => {
    if (!rpc.supports(method)) {
      throw new SandboxError('unsupported', `${method} is unsupported`)
    }
    try {
      const value = await call<
        RuntimeInstallResult | RuntimeCancelResult | RuntimeRemoveResult
      >(method, params, options)
      return projectOperationReceipt(value, componentId, valid, method)
    } catch (error) {
      if (isMethodUnsupported(error)) rpc.markUnsupported(method)
      throw mapError(error, options?.signal)
    }
  }

  return {
    async readiness(options) {
      return await readReadiness(options)
    },

    async ensureReady(options) {
      if (setupInFlight) return await setupInFlight
      const operation = (async (): Promise<SandboxSetupResult> => {
        let status: SandboxSetupStatusPayload | null
        if (setupMutationAttempted) {
          status = await waitForSetupCompletion(null, options)
          const result = await finishSetup(status, options, false, true)
          if (status && status.state !== 'ready' && status.state !== 'setting_up') {
            setupMutationAttempted = false
          }
          return result
        }
        setupMutationAttempted = true
        let retryDefinitelyUnsent = true
        while (true) {
          let requestStarted = false
          let requestSent = false
          try {
            const value = await call<SetupEnsureResult>(
              SANDBOX_SETUP_ENSURE_METHOD,
              undefined,
              {
                ...options,
                timeoutMs: options?.timeoutMs ?? SETUP_TIMEOUT_MS,
              },
              false,
              {
                onRequestStart: () => { requestStarted = true },
                onSent: () => { requestSent = true },
              },
            )
            status = projectSetupStatus(
              value,
              validateSetupEnsureResult,
              SANDBOX_SETUP_ENSURE_METHOD,
            )
            break
          } catch (error) {
            const definitelyUnsent = setupRequestWasDefinitelyUnsent(
              error,
              requestStarted,
              requestSent,
            )
            if (definitelyUnsent) {
              if (isSetupResponseLoss(error) && retryDefinitelyUnsent) {
                retryDefinitelyUnsent = false
                continue
              }
              setupMutationAttempted = false
              throw mapError(error, options?.signal)
            }
            if (isAbort(error, options?.signal)) {
              throw mapError(error, options?.signal)
            }
            if (isExplicitSetupRejection(error)) {
              setupMutationAttempted = false
              throw mapError(error, options?.signal)
            }
            try {
              // Setup may have completed after the original socket disappeared
              // (notably across Windows UAC). Never repeat the mutation: reconnect,
              // then reconcile only authoritative read projections until the
              // server reports a terminal state.
              status = await waitForSetupCompletion(null, options)
              const result = await finishSetup(status, options, false, true)
              if (status && status.state !== 'ready' && status.state !== 'setting_up') {
                setupMutationAttempted = false
              }
              return result
            } catch (reconcileError) {
              if (isAbort(reconcileError, options?.signal)) {
                throw mapError(reconcileError, options?.signal)
              }
              return { ready: false, status: null, capability: null, outcome: 'in_progress' }
            }
          }
        }
        status = await waitForSetupCompletion(status, options)
        const result = await finishSetup(status, options)
        if (status && status.state !== 'ready' && status.state !== 'setting_up') {
          setupMutationAttempted = false
        }
        return result
      })()
      setupInFlight = operation
      try {
        return await operation
      } finally {
        if (setupInFlight === operation) setupInFlight = null
      }
    },

    async loadSettings(options) {
      try {
        const [policyValue, defaultsValue, preferenceValue] = await Promise.all([
          call<PolicyGetResult>(SANDBOX_POLICY_GET_METHOD, undefined, options),
          call<PolicyDefaultsResult>(SANDBOX_POLICY_DEFAULTS_METHOD, undefined, options),
          call<RunModePreferenceGetResult>(
            SANDBOX_RUN_MODE_PREFERENCE_GET_METHOD,
            undefined,
            options,
          ),
        ])
        return {
          policy: projectPolicy(
            policyValue,
            validatePolicyGetResult,
            SANDBOX_POLICY_GET_METHOD,
          ),
          defaults: projectDefaults(defaultsValue),
          preference: projectPreference(
            preferenceValue,
            validateRunModePreferenceGetResult,
            SANDBOX_RUN_MODE_PREFERENCE_GET_METHOD,
          ),
        }
      } catch (error) {
        throw mapError(error, options?.signal)
      }
    },

    async updatePolicy(basePolicyVersion, policy, options) {
      try {
        const value = await call<PolicyUpdateResult>(SANDBOX_POLICY_UPDATE_METHOD, {
          basePolicyVersion,
          policy,
        }, options)
        return projectPolicy(value, validatePolicyUpdateResult, SANDBOX_POLICY_UPDATE_METHOD)
      } catch (error) {
        throw mapError(error, options?.signal)
      }
    },

    preference,

    async selectMode(mode: SandboxRunMode, options?: SandboxRequestOptions) {
      try {
        const value = await call<RunModePreferenceSetResult>(
          SANDBOX_RUN_MODE_PREFERENCE_SET_METHOD,
          { runMode: mode },
          options,
        )
        return projectPreference(
          value,
          validateRunModePreferenceSetResult,
          SANDBOX_RUN_MODE_PREFERENCE_SET_METHOD,
        )
      } catch (error) {
        throw mapError(error, options?.signal)
      }
    },

    onPreferenceChanged(handler) {
      // Registration is local and safe before the Gateway hello advertises its
      // event list. Gating here would permanently lose updates for views that
      // mount while the connection is still negotiating.
      const subscription = events.subscribe(
        SANDBOX_RUN_MODE_PREFERENCE_CHANGED_EVENT,
        payload => {
          const canonical = canonicalPreference(payload)
          if (!canonical || !validateRunModePreferenceChangedPayload(canonical)) return
          const projected = projectPreference(
            canonical as RunModePreferenceChangedPayload,
            validateRunModePreferenceChangedPayload,
            SANDBOX_RUN_MODE_PREFERENCE_CHANGED_EVENT,
          )
          try {
            handler(projected)
          } catch (error) {
            console.error('[SandboxRuntime] preference listener failed', error)
          }
        },
      )
      return () => subscription.close()
    },

    async runtimeStatus(options) {
      if (!rpc.supports(SANDBOX_RUNTIME_STATUS_METHOD)) return null
      try {
        const value = await call<RuntimeStatusResult>(
          SANDBOX_RUNTIME_STATUS_METHOD,
          undefined,
          options,
        )
        return projectRuntimeStatus(value, SANDBOX_RUNTIME_STATUS_METHOD)
      } catch (error) {
        if (isMethodUnsupported(error)) {
          rpc.markUnsupported(SANDBOX_RUNTIME_STATUS_METHOD)
          return null
        }
        throw mapError(error, options?.signal)
      }
    },

    installRuntime(componentId, options) {
      return runtimeOperation(
        SANDBOX_RUNTIME_INSTALL_METHOD,
        { componentId },
        componentId,
        validateRuntimeInstallResult,
        options,
      )
    },

    cancelRuntime(componentId, operationId, options) {
      return runtimeOperation(
        SANDBOX_RUNTIME_CANCEL_METHOD,
        { componentId, operationId },
        componentId,
        validateRuntimeCancelResult,
        options,
      )
    },

    removeRuntime(componentId, options) {
      return runtimeOperation(
        SANDBOX_RUNTIME_REMOVE_METHOD,
        { componentId },
        componentId,
        validateRuntimeRemoveResult,
        options,
      )
    },

    async discardRuntimeDownload(componentId, options) {
      if (!rpc.supports(SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD)) {
        throw new SandboxError(
          'unsupported',
          `${SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD} is unsupported`,
        )
      }
      try {
        const value = await call<RuntimeDiscardDownloadResult>(
          SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD,
          { componentId },
          options,
        )
        return projectStatusReceipt(value, SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD)
      } catch (error) {
        if (isMethodUnsupported(error)) {
          rpc.markUnsupported(SANDBOX_RUNTIME_DISCARD_DOWNLOAD_METHOD)
        }
        throw mapError(error, options?.signal)
      }
    },

    async resumeSession(sessionKey, options) {
      try {
        const value = await call<ResumeResult>(
          SANDBOX_RESUME_METHOD,
          { sessionKey },
          options,
        )
        if (!validateResumeResult(value)) {
          throw new SandboxError(
            'invalid',
            `${SANDBOX_RESUME_METHOD} returned an invalid recovery result`,
          )
        }
        return {
          sessionKey: value.sessionKey,
          resumed: value.resumed,
          autonomousPaused: value.autonomousPaused,
        }
      } catch (error) {
        throw mapError(error, options?.signal)
      }
    },
  }
}
