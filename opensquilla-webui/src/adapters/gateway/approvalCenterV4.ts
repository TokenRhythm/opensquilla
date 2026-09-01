import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { HttpTransportError } from './privateHttpTransport'
import type {
  ApprovalCenter,
  ApprovalAvailability,
  ApprovalErrorKind,
  ApprovalEvent,
  ApprovalItem,
  ApprovalNamespace,
  ApprovalRequestOptions,
  ApprovalResolveResult,
  ApprovalStatus,
} from '@/modules/approvalCenter'
import { ApprovalCenterError, approvalChoiceForDecision } from '@/modules/approvalCenter'
import {
  validateApprovalResolveParams,
  validateApprovalResolveResult,
} from '@/contracts/generated/v4/approvalResolveValidators.mjs'
import {
  createApprovalCenterV4Contract,
  ApprovalCenterContractError,
  projectApprovalDisplayArgs,
  projectApprovalHttpSnapshot,
  type ApprovalCenterContractEventTransport,
  type ApprovalCenterContractTransport,
} from './approvalCenterV4Contract'

interface ApprovalRpcTransport extends ApprovalCenterContractTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
}

interface ApprovalEventTransport extends ApprovalCenterContractEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
}

interface ApprovalHttpTransport {
  // Deliberately structural: the Adapter only uses same-origin GET/POST.
  // Keep private HTTP transport types out of the exported declaration.
  requestJson<T = unknown>(
    endpoint: string,
    options?:
      | { method?: 'GET'; signal?: AbortSignal; timeoutMs?: number }
      | { method: 'POST'; json: unknown; signal?: AbortSignal; timeoutMs?: number },
  ): Promise<T>
}

export interface ApprovalCenterV4Options {
  http: ApprovalHttpTransport
  onViolation?: (error: unknown) => void
}

const APPROVAL_EXTEND_SECONDS = 300

function optionsFor(request?: ApprovalRequestOptions): RpcCallOptions | undefined {
  if (!request) return undefined
  return {
    signal: request.signal,
    timeoutMs: request.timeoutMs,
    timeoutAction: 'reject',
    abortAction: 'reject',
  }
}

function approvalErrorKind(error: unknown): ApprovalErrorKind | null {
  if (error instanceof HttpTransportError) {
    if (error.status === 401 || error.status === 403) return 'forbidden'
    if (error.status === 404) return 'not-found'
    if (error.status === 409) return 'conflict'
    if (error.kind === 'aborted' || error.kind === 'timeout' || error.kind === 'network') return 'unavailable'
    if (error.kind === 'decode' || error.kind === 'encode' || error.kind === 'invalid-endpoint') return 'invalid'
    return 'unavailable'
  }
  const candidate = error as { code?: unknown; message?: unknown } | null
  const code = typeof candidate?.code === 'string' ? candidate.code.toUpperCase() : ''
  if (code === 'NOT_FOUND') return 'not-found'
  if (code === 'UNSUPPORTED' || code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED_PARAM') return 'unsupported'
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN' || code === 'PERMISSION_DENIED') return 'forbidden'
  if (code === 'CONFLICT') return 'conflict'
  if (code === 'INVALID_PARAMS' || code === 'INVALID_REQUEST') return 'invalid'
  if (
    code === 'UNAVAILABLE'
    || code === 'STORAGE_BUSY'
    || code === 'TIMEOUT'
    || code === 'RPC_TIMEOUT'
    || code === 'RPC_ABORTED'
    || code === 'RPC_TRANSPORT_ERROR'
  ) return 'unavailable'
  return null
}

function mapApprovalError(error: unknown): unknown {
  if (error instanceof ApprovalCenterError) return error
  if (error instanceof ApprovalCenterContractError) {
    return new ApprovalCenterError('invalid', error.message, error)
  }
  const kind = approvalErrorKind(error)
  if (!kind) return error
  const message = error instanceof Error ? error.message : String(error)
  return new ApprovalCenterError(kind, message, error)
}

function httpOptions(request?: ApprovalRequestOptions) {
  return request ? { signal: request.signal, timeoutMs: request.timeoutMs } : undefined
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

const DISPLAY_KINDS = new Set(['delete', 'modify', 'create', 'run_command', 'run_code', 'network_access', 'path_access', 'plugin_permission', 'sensitive_operation'])
const BACKUP_STATES = new Set(['not_applicable', 'enabled', 'disabled', 'unavailable_requires_confirmation'])

function displayKind(value: unknown, kind: string, command: string): string {
  const explicit = text(value)
  if (DISPLAY_KINDS.has(explicit)) return explicit
  if (kind === 'sandbox_network') return 'network_access'
  if (kind === 'sandbox_path') return 'path_access'
  return command ? 'run_command' : 'sensitive_operation'
}

function backupState(value: unknown): string {
  const explicit = text(value)
  return BACKUP_STATES.has(explicit) ? explicit : 'not_applicable'
}

function target(kind: string, explicit: unknown, args: Readonly<Record<string, unknown>> | null): string {
  const value = text(explicit)
  if (value) return value
  if (!args) return ''
  if (kind === 'network_access') return text(args.host || args.bundle_id)
  if (kind === 'path_access') return text(args.path)
  return ''
}

function itemFrom(value: Record<string, unknown>): ApprovalItem {
  let command = text(value.command)
  if (!command && Array.isArray(value.argv) && value.argv.length) command = value.argv.map(String).join(' ')
  const rawArgs = value.args && typeof value.args === 'object' && !Array.isArray(value.args)
    ? value.args as Readonly<Record<string, unknown>>
    : null
  const kind = text(value.approvalKind || value.approval_kind)
  const args = projectApprovalDisplayArgs(kind, rawArgs)
  if (!command && rawArgs && typeof rawArgs.command === 'string') command = rawArgs.command
  const shownKind = displayKind(value.displayKind || value.display_kind, kind, command)
  return {
    id: text(value.id || value.approvalId || value.approval_id),
    namespace: value.namespace === 'plugin' ? 'plugin' : 'exec',
    toolName: text(value.toolName || value.tool_name || value.pluginId || value.actionKind || value.action_kind),
    command,
    approvalKind: kind,
    args,
    warning: text(value.warning),
    displayKind: shownKind,
    displayTarget: target(shownKind, value.displayTarget || value.display_target, args),
    destructive: value.destructive === true,
    irreversible: value.irreversible === true,
    backupState: backupState(value.backupState || value.backup_state),
    agent: text(value.agent),
    sessionKey: text(value.sessionKey || value.session_key),
    deadline: Number(value.deadline) || 0,
  }
}

function statusFrom(value: Awaited<ReturnType<ReturnType<typeof createApprovalCenterV4Contract>['status']>>): ApprovalStatus {
  return {
    id: value.id,
    namespace: value.namespace,
    ...(value.found === undefined ? {} : { found: value.found }),
    pending: value.pending,
    resolutionInProgress: value.resolutionInProgress,
    resolved: value.resolved,
    approved: value.approved,
    resolution: value.resolution,
    consumed: value.consumed,
    deadline: value.deadline,
  }
}

function eventKind(event: string): ApprovalEvent['kind'] {
  if (event.endsWith('.requested')) return 'requested'
  if (event.endsWith('.updated')) return 'updated'
  return 'resolved'
}

function availabilityFromTransportState(value: unknown): ApprovalAvailability | null {
  if (value === 'connected') return 'available'
  if (value === 'connecting') return 'recovering'
  if (value === 'disconnected') return 'unavailable'
  return null
}

function assertApprovalIdentity(namespace: unknown, id: unknown): asserts namespace is ApprovalNamespace {
  if (namespace !== 'exec' && namespace !== 'plugin') {
    throw new ApprovalCenterError('invalid', 'Approval namespace is invalid.')
  }
  if (!text(id)) {
    throw new ApprovalCenterError('invalid', 'Approval id is required.')
  }
}

export function createApprovalCenterV4(
  rpc: ApprovalRpcTransport,
  events: ApprovalEventTransport,
  options: ApprovalCenterV4Options,
): ApprovalCenter {
  const listeners = new Set<(event: ApprovalEvent) => void>()
  const availabilityListeners = new Set<(state: ApprovalAvailability) => void>()
  const contract = createApprovalCenterV4Contract(rpc, events, {
    onViolation: options.onViolation,
  })
  const stateSubscription = events.subscribe('_state', payload => {
    const transportState = typeof payload === 'string'
      ? payload
      : payload && typeof payload === 'object'
        ? (payload as Record<string, unknown>).state
        : undefined
    const availability = availabilityFromTransportState(transportState)
    if (availability) {
      for (const listener of availabilityListeners) listener(availability)
    }
  })
  const eventSubscription = contract.subscribe(event => {
    const approval = itemFrom({
      id: event.approvalId,
      namespace: event.namespace,
      sessionKey: event.sessionKey,
      toolName: event.toolName,
      command: event.command,
      approvalKind: event.approvalKind,
      args: event.args,
      warning: event.warning,
      displayKind: event.displayKind,
      displayTarget: event.displayTarget,
      destructive: event.destructive,
      irreversible: event.irreversible,
      backupState: event.backupState,
      agent: event.agent,
      deadline: event.deadline,
    })
    const payload = (event as unknown as { event: string })
    const projected: ApprovalEvent = {
      kind: eventKind(payload.event),
      approvalId: event.approvalId,
      namespace: event.namespace,
      approval,
      sessionKey: event.sessionKey,
      approved: event.approved,
      resolution: event.resolution,
      emittedAt: event.emittedAt ?? event.createdAt,
      ...(event.streamSeq && event.streamSeq > 0 ? { activityOrder: event.streamSeq } : {}),
      needsHydration: !event.hasArgs || !event.hasWarning,
    }
    for (const listener of listeners) {
      try { listener(projected) } catch { /* best effort */ }
    }
  })

  return {
    async setElevatedMode(sessionKey, mode, request) {
      try {
        await options.http.requestJson('/api/elevated-mode', {
          method: 'POST',
          json: { sessionKey, mode },
          ...httpOptions(request),
        })
      } catch (error) {
        throw mapApprovalError(error)
      }
    },
    async snapshot(request) {
      try {
        const value = await options.http.requestJson<unknown>('/api/approvals', {
          ...httpOptions(request),
          method: 'GET',
        })
        const snapshot = projectApprovalHttpSnapshot(value)
        return {
          mode: snapshot.mode,
          pending: snapshot.pending.map(item => itemFrom(item as unknown as Record<string, unknown>)),
        }
      } catch (error) {
        throw mapApprovalError(error)
      }
    },
    async status(namespace, id, request) {
      assertApprovalIdentity(namespace, id)
      try {
        return statusFrom(await contract.status(namespace, id, optionsFor(request)))
      } catch (error) {
        throw mapApprovalError(error)
      }
    },
    async resolve(request, requestOptions): Promise<ApprovalResolveResult> {
      assertApprovalIdentity(request.namespace, request.id)
      if (request.decision !== 'allow-once'
        && request.decision !== 'allow-always'
        && request.decision !== 'deny') {
        throw new ApprovalCenterError('invalid', 'Approval decision is invalid.')
      }
      const body = {
        id: text(request.id),
        namespace: request.namespace,
        approved: request.decision !== 'deny',
        choice: approvalChoiceForDecision(request.decision),
      }
      if (!validateApprovalResolveParams(body)) {
        throw new ApprovalCenterContractError(
          'approval.resolve',
          'HTTP request violated its generated v4 Contract',
          validateApprovalResolveParams.errors ?? [],
        )
      }
      try {
        const value = await options.http.requestJson<unknown>('/api/approvals/resolve', {
          ...httpOptions(requestOptions),
          method: 'POST',
          json: body,
        })
        if (!validateApprovalResolveResult(value)) {
          throw new ApprovalCenterContractError(
            'approval.resolve',
            'HTTP result violated its generated v4 Contract',
            validateApprovalResolveResult.errors ?? [],
          )
        }
        const result = value as Record<string, unknown>
        return {
          ...(typeof result.approved === 'boolean' ? { approved: result.approved } : {}),
          ...(typeof result.resolved === 'boolean' ? { resolved: result.resolved } : {}),
          ...(typeof result.pending === 'boolean' ? { pending: result.pending } : {}),
          ...(typeof result.resolution === 'string' ? { resolution: result.resolution } : {}),
          ...(typeof result.resolutionInProgress === 'boolean' ? { resolutionInProgress: result.resolutionInProgress } : {}),
        }
      } catch (error) {
        throw mapApprovalError(error)
      }
    },
    async extend(namespace, id, seconds = APPROVAL_EXTEND_SECONDS, request) {
      assertApprovalIdentity(namespace, id)
      if (!Number.isFinite(seconds) || seconds <= 0) {
        throw new ApprovalCenterError('invalid', 'Approval extension seconds are invalid.')
      }
      try {
        return statusFrom(await contract.extend(namespace, id, seconds, optionsFor(request)))
      } catch (error) {
        throw mapApprovalError(error)
      }
    },
    subscribe(listener) {
      listeners.add(listener)
      return { close: () => listeners.delete(listener) }
    },
    subscribeAvailability(listener) {
      availabilityListeners.add(listener)
      return { close: () => availabilityListeners.delete(listener) }
    },
    dispose() {
      listeners.clear()
      availabilityListeners.clear()
      eventSubscription.close()
      stateSubscription.close()
      contract.dispose()
    },
  }
}
