/** OpenSquilla Web UI — WebSocket RPC client (TypeScript port). */

const ANSWER_GENERATION_RESET_CAPABILITY = 'session.answer_generation_reset.v1';
const TURN_COMMITTED_CAPABILITY = 'session.turn_committed.v1';
const PROBE_CAPABILITY = 'transport.probe.v1';
export const WEB_RPC_PROTOCOL_VERSION = 3 as const;

export interface HelloOkFrame {
  type: 'hello-ok';
  protocol: typeof WEB_RPC_PROTOCOL_VERSION;
  server: {
    version: string;
    conn_id: string;
    [key: string]: unknown;
  };
  features: {
    methods: string[];
    events: string[];
    [key: string]: unknown;
  };
  snapshot: Record<string, unknown>;
  policy: Record<string, unknown>;
  auth: Record<string, unknown> | null;
  [key: string]: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

const HELLO_POLICY_INTEGER_FIELDS = [
  'max_payload',
  'max_buffered_bytes',
  'tick_interval_ms',
  'agent_stream_heartbeat_interval_ms',
  'agent_stream_idle_timeout_ms',
  'webui_stream_idle_grace_ms',
  'client_ws_keepalive_timeout_ms',
] as const;

function isHelloPolicy(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value)) return false;
  for (const field of HELLO_POLICY_INTEGER_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(value, field)) continue;
    const fieldValue = value[field];
    if (
      typeof fieldValue !== 'number'
      || !Number.isSafeInteger(fieldValue)
      || fieldValue < 0
      || (field === 'tick_interval_ms' && fieldValue === 0)
    ) {
      return false;
    }
  }
  if (
    Object.prototype.hasOwnProperty.call(value, 'concurrent_history_reads')
    && typeof value.concurrent_history_reads !== 'boolean'
  ) return false;
  if (
    Object.prototype.hasOwnProperty.call(value, 'concurrent_optional_read_methods')
    && !isStringArray(value.concurrent_optional_read_methods)
  ) return false;
  if (
    Object.prototype.hasOwnProperty.call(value, 'cancellable_request_methods')
    && !isStringArray(value.cancellable_request_methods)
  ) return false;
  if (
    Object.prototype.hasOwnProperty.call(value, 'provider_probe_modes')
    && !isStringArray(value.provider_probe_modes)
  ) return false;
  return true;
}

/** Validate the authenticated Gateway handshake without rejecting additive fields. */
export function isHelloOkFrame(value: unknown): value is HelloOkFrame {
  if (!isRecord(value)) return false;
  if (value.type !== 'hello-ok') return false;
  if (
    !Number.isInteger(value.protocol)
    || value.protocol !== WEB_RPC_PROTOCOL_VERSION
  ) {
    return false;
  }

  const server = value.server;
  if (
    !isRecord(server)
    || typeof server.version !== 'string'
    || server.version.trim().length === 0
    || typeof server.conn_id !== 'string'
    || server.conn_id.trim().length === 0
  ) {
    return false;
  }

  const features = value.features;
  if (
    !isRecord(features)
    || !isStringArray(features.methods)
    || !isStringArray(features.events)
  ) {
    return false;
  }

  return (
    isRecord(value.snapshot)
    && isHelloPolicy(value.policy)
    && (value.auth === null || isRecord(value.auth))
  );
}

function isHelloOkCandidate(value: unknown): boolean {
  return isRecord(value)
    && (
      value.type === 'hello-ok'
      || Object.prototype.hasOwnProperty.call(value, 'protocol')
    );
}

export interface RpcErrorDetail {
  code?: string;
  message?: string;
  details?: unknown;
  retryable?: boolean;
  retry_after_ms?: number;
  accepted?: boolean | null;
}

export interface RpcClientError extends Error {
  code?: string;
  details?: unknown;
  retryable?: boolean;
  retry_after_ms?: number;
  accepted?: boolean | null;
}

function rpcResponseError(value: RpcErrorDetail | string | undefined): RpcClientError {
  const message =
    typeof value === 'string'
      ? value
      : (value && (value.message || value.code)) || 'RPC error';
  const error = new Error(message) as RpcClientError;
  if (value && typeof value === 'object') {
    error.code = value.code;
    error.details = value.details;
    error.retryable = value.retryable;
    error.retry_after_ms = value.retry_after_ms;
    error.accepted = value.accepted;
  }
  return error;
}

/** @deprecated Termination is always request-local, including legacy reconnect. */
export type RpcTerminationAction = 'reject' | 'reconnect';

export interface RpcConnectionIntent {
  authentication?: 'guest-allowed' | 'authenticated' | 'owner';
  /** Non-secret target identity (for example Desktop profile and instance). */
  key?: string;
  /** Optional deployment label; proxy/VPN routing cannot be inferred from a URL. */
  topology?: 'loopback' | 'remote' | 'proxy/vpn';
}

export type RpcLifecycle = 'stopped' | 'connecting' | 'connected' | 'recovering' | 'blocked';
export type RpcConsumptionResult = 'applied' | 'dirty';
export type RecoveryClass = 'safe-read' | 'read' | 'mutation' | 'ephemeral';
export type RpcRecoveryClass = RecoveryClass;
export type RpcConsumptionHandler = (
  payload: unknown, meta: Record<string, unknown>,
) => RpcConsumptionResult | Promise<RpcConsumptionResult>;

export interface RpcCallOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  timeoutAction?: RpcTerminationAction;
  abortAction?: RpcTerminationAction;
  /**
   * Whether a request is safe to issue while wake recovery is in progress.
   * Missing values fail closed as mutations once the transport is checking
   * or suspect; adapters may explicitly opt a bounded read in.
   */
  recoveryClass?: RpcRecoveryClass;
  /** Send a capability-gated cancellation frame before rejecting on abort. */
  cancelOnAbort?: boolean;
  /** Reject before send unless the current socket still owns this generation. */
  expectedGeneration?: number;
  /** Called synchronously only after the request frame is accepted by send(). */
  onSent?: (socketGeneration: number) => void;
}

export interface RpcConnectionWaitOptions {
  timeoutAction?: RpcTerminationAction;
  abortAction?: RpcTerminationAction;
}

export class RpcTimeoutError extends Error implements RpcClientError {
  readonly code = 'RPC_TIMEOUT';

  constructor(
    readonly method: string,
    readonly timeoutMs: number
  ) {
    super(`${method} timed out after ${timeoutMs}ms`);
    this.name = 'RpcTimeoutError';
  }
}

export class RpcAbortError extends Error implements RpcClientError {
  readonly code = 'RPC_ABORTED';

  constructor(readonly method: string) {
    super(`${method} was aborted`);
    this.name = 'RpcAbortError';
  }
}

/**
 * A connection failure with an explicit request-acceptance boundary.
 *
 * `false` means no request frame reached `WebSocket.send()`. `null` means the
 * frame may have reached the Gateway, so a mutation must resolve its original
 * request identity before it can safely issue another write.
 */
export class RpcTransportError extends Error implements RpcClientError {
  readonly code = 'RPC_TRANSPORT_ERROR';

  constructor(
    message: string,
    readonly accepted: boolean | null
  ) {
    super(message);
    this.name = 'RpcTransportError';
  }
}

export interface RpcFrame {
  type?: string;
  id?: string;
  method?: string;
  params?: Record<string, unknown>;
  event?: string;
  payload?: unknown;
  meta?: Record<string, unknown>;
  ok?: boolean;
  error?: string | RpcErrorDetail;
  protocol?: number;
  policy?: Record<string, unknown>;
  server?: {
    version?: string;
    conn_id?: string;
  };
  features?: {
    methods?: string[];
    events?: string[];
  };
  auth?: Record<string, unknown> | null;
  seq?: number;
  nonce?: string;
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected';
export type RpcEventHandler = {
  bivarianceHack(...args: unknown[]): void;
}['bivarianceHack'];

const RECONNECT_INITIAL_MS = 500;
const RECONNECT_MAX_MS = 15_000;
const CONNECTION_STABLE_MS = 30_000;
const CONNECT_CHALLENGE_TIMEOUT_MS = 15_000;
const CONNECT_HELLO_TIMEOUT_MS = 45_000;
const WAKE_DEBOUNCE_MS = 100;
const PROBE_TIMEOUT_MS = 10_000;
const NATIVE_RESUME_PROBE_TIMEOUT_MS = 2_000;
const SOFT_SUSPECT_MS = 5_000;
const SAFE_READ_TIMEOUT_MS = 5_000;
const SAFE_READ_MAX_PENDING = 8;
const SUSPECT_WINDOW_MS = 30_000;
const SCHEDULER_LAG_MS = 5_000;
// The first wake signal owns a bounded incident window. Later browser wake
// signals may request another probe but cannot move this deadline forward.
const WAKE_INCIDENT_BUDGET_MS = 20_000;

function isSafeReadRecoveryClass(value: RpcRecoveryClass | undefined): boolean {
  return value === 'read' || value === 'safe-read';
}

/** The source of a wake observation. It is telemetry, never a liveness proof. */
export type ResumeSource = 'desktop-resume' | 'pageshow' | 'online' | 'manual';
export type RpcResumeSource = ResumeSource;
export type TransportPhase = 'healthy' | 'checking' | 'suspect' | 'reconnecting';
export type RpcTransportPhase = TransportPhase;

interface WakeIncident {
  id: number;
  generation: number;
  startedAt: number;
  deadlineAt: number;
  source: RpcResumeSource;
  probeTimeoutMs: number;
  status: 'probing' | 'suspect' | 'reconnecting' | 'recovered';
  signals: number;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  method: string;
  generation: number;
  timeoutTimer: ReturnType<typeof setTimeout> | null;
  signal: AbortSignal | null;
  abortHandler: (() => void) | null;
  sentAt: number | null;
  incidentIdAtSend: number | null;
  recoveryClass: RpcRecoveryClass;
}

interface QueuedRecoveryRead {
  method: string;
  params: Record<string, unknown>;
  options: RpcCallOptions;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  deadlineAt: number;
  timeoutTimer: ReturnType<typeof setTimeout> | null;
  abortHandler: (() => void) | null;
}

const GUEST_SESSION_STORAGE_KEY = 'opensquilla.guestSessionKey';
const GUEST_SESSION_KEY_PATTERN = /^osqg_[A-Za-z0-9_-]{43}$/;

function persistGuestSessionKey(value: string): void {
  if (!GUEST_SESSION_KEY_PATTERN.test(value)) return;
  try {
    globalThis.localStorage?.setItem(GUEST_SESSION_STORAGE_KEY, value);
  } catch {
    // Storage can be disabled; the in-memory key still protects this connection.
  }
}

function newGuestSessionKey(): string {
  const bytes = new Uint8Array(32);
  globalThis.crypto.getRandomValues(bytes);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = globalThis.btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
  return `osqg_${encoded}`;
}

function loadGuestSessionKey(): string {
  try {
    const stored = globalThis.localStorage?.getItem(GUEST_SESSION_STORAGE_KEY) || '';
    if (GUEST_SESSION_KEY_PATTERN.test(stored)) return stored;
  } catch {
    // Fall through to an in-memory credential.
  }
  const generated = newGuestSessionKey();
  persistGuestSessionKey(generated);
  return generated;
}

export class RpcClient {
  private _ws: WebSocket | null = null;
  private _socketGeneration = 0;
  private _reqId = 0;
  private _pending = new Map<string, PendingRequest>();
  private _listeners = new Map<string, Set<RpcEventHandler>>();
  private _state: ConnectionState = 'disconnected';
  private _url = '';
  private _token: string | null = null;
  private _intent: RpcConnectionIntent = {};
  private _blockedReason: string | null = null;
  private _everConnected = false;
  private _stableTimer: ReturnType<typeof setTimeout> | null = null;
  private _lastExpediteAt = -Infinity;
  private _health: 'healthy' | 'suspect' = 'healthy';
  private _phase: RpcTransportPhase = 'healthy';
  private _suspectAt: number | null = null;
  private _probeNonce: string | null = null;
  private _probeCounter = 0;
  private _suspectProbes = 0;
  private _graceUntil = 0;
  private _lastHealthCheckAt = 0;
  private _lastLoopLagMs = 0;
  private _maxLoopLagMs = 0;
  private _recoveryStartedAt: number | null = null;
  private _lastProbeAt = 0;
  private _gapHandlers = new Set<(detail: unknown) => Promise<boolean>>();
  private _gapRecovery: Promise<void> | null = null;
  private _pendingGap: unknown = null;
  private _gapRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private _consumptionFlowEnabled = false;
  private _consumers = new Map<string, Set<RpcConsumptionHandler>>();
  private _guestSessionKey: string | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAttempt = 0;
  private _autoReconnect = false;
  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _pingInterval = 30_000;
  private _policy: Record<string, unknown> | null = null;
  private _lastSeq = 0;
  private _lastFrameAt = 0;
  private _tickWatchTimer: ReturnType<typeof setInterval> | null = null;
  private _wakeDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeProbeTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeProbeGeneration: number | null = null;
  private _wakeSoftSuspectTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeIncident: WakeIncident | null = null;
  private _wakeIncidentTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeIncidentCounter = 0;
  private _recoveryReads: QueuedRecoveryRead[] = [];
  private _firstSuccessfulRpc = false;
  private _challengeWatchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private _challengeWatchdogGeneration: number | null = null;
  private _helloWatchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private _helloWatchdogGeneration: number | null = null;
  private _lifecycleWatchStarted = false;

  private readonly _handleWakeSignal = (event: Event): void => {
    if (
      event.type === 'visibilitychange'
      && typeof document !== 'undefined'
      && document.visibilityState === 'hidden'
    ) {
      return;
    }
    // A normal page navigation emits pageshow with persisted=false. It is
    // initial document bootstrap, not a BFCache restore or an OS wake, and it
    // can arrive after Hello but before the first session hydration RPC.
    const persisted = (event as Event & { persisted?: unknown }).persisted;
    if (event.type === 'pageshow' && persisted === false) return;
    // The first pageshow/online/visibility signal belongs to initial page
    // boot, not to a sleep/wake incident.  Starting an incident before the
    // first successful Hello would gate the initial session hydration as an
    // unconfirmed mutation.  Preserve the normal initial-connect/backoff path
    // without publishing a false checking state.
    if (!this._everConnected) {
      this.ensureConnected();
      return;
    }
    const source: RpcResumeSource = event.type === 'pageshow'
      ? 'pageshow'
      : event.type === 'online' ? 'online' : 'manual';
    this.notifyResume(source);
  };

  connect(url: string, token?: string, intent: RpcConnectionIntent = {}): void {
    const authentication = intent.authentication || (token ? 'authenticated' : 'guest-allowed');
    if (this._autoReconnect && !this._blockedReason && this._url === url && this._token === (token || null)
      && this._intent.authentication === authentication && this._intent.key === intent.key) {
      this.ensureConnected();
      return;
    }
    this._url = url;
    this._token = token || null;
    this._intent = { ...intent, authentication };
    this._blockedReason = null;
    this._recoveryStartedAt = null;
    this._lastLoopLagMs = this._maxLoopLagMs = 0;
    this._guestSessionKey = this._guestSessionKey || loadGuestSessionKey();
    this._autoReconnect = true;
    this._reconnectAttempt = 0;
    this._startLifecycleWatch();
    this._clearReconnectTimer();
    if (this._ws) {
      this._retireCurrentSocket(
        new RpcTransportError('Connection replaced', null),
        false,
        'connection_replaced'
      );
    }
    this._doConnect();
  }

  /** An observation may accelerate recovery, but never replace a live attempt. */
  ensureConnected(): void {
    if (!this._autoReconnect || this._blockedReason) return;
    if (this._ws && (this._ws.readyState === WebSocket.OPEN || this._ws.readyState === WebSocket.CONNECTING)) return;
    const now = Date.now();
    if (now - this._lastExpediteAt < 1_000) return;
    this._lastExpediteAt = now;
    if (this._ws) {
      this._markRecoveryStarted();
      this._retireCurrentSocket(new Error('Socket no longer open'), false, 'socket_not_open');
    }
    this._clearReconnectTimer();
    this._doConnect();
  }

  notifyResume(source: RpcResumeSource = 'manual'): void {
    if (!this._autoReconnect || this._blockedReason) return;
    const now = Date.now();
    const generation = this._socketGeneration;
    const current = this._wakeIncident;
    if (current === null || current.generation !== generation) {
      this._beginWakeIncident(generation, now, source);
      if (!this._autoReconnect || generation !== this._socketGeneration || !this._wakeIncident) return;
      if (this._health !== 'suspect') {
        this._clearWakeProbe();
        // Wake is already an explicit liveness observation. Send one bounded
        // nonce probe immediately; the five-second soft state is reserved for
        // a probe that never produces a valid response.
        this._graceUntil = now;
      }
      // A wake signal is not a liveness proof. Preserve an existing suspect
      // state and any armed retry until this generation completes a round trip.
    } else {
      current.signals += 1;
      // Neither the deadline nor the grace period is extended. Keep an armed
      // probe intact, including its nonce, across all duplicate wake sources.
      if (this._wakeProbeGeneration !== null || this._health === 'suspect') return;
    }
    this._scheduleWakeProbe();
  }

  disconnect(): void {
    this._autoReconnect = false;
    this._blockedReason = null;
    this._recoveryStartedAt = null;
    this._stopLifecycleWatch();
    this._clearWakeIncident();
    this._clearReconnectTimer();
    this._retireCurrentSocket(
      new RpcTransportError('Disconnected', null),
      false,
      'client_disconnect'
    );
    this._clearQueuedRecoveryReads(new RpcTransportError('Disconnected', false));
    this._rejectAllPending(new RpcTransportError('Disconnected', null));
    this._setState('disconnected');
  }

  call(
    method: string,
    params: Record<string, unknown> = {},
    options: RpcCallOptions = {}
  ): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (options.signal?.aborted) {
        reject(new RpcAbortError(method));
        return;
      }
      if (this._phase !== 'healthy') {
        if (!isSafeReadRecoveryClass(options.recoveryClass)) {
          reject(new RpcTransportError(
            this._phase === 'checking'
              ? 'Connection is being checked after wake'
              : 'Connection health is suspect',
            false,
          ));
          return;
        }
        this._queueRecoveryRead(method, params, options, resolve, reject);
        return;
      }
      const socket = this._ws;
      const generation = this._socketGeneration;
      if (!socket || socket.readyState !== WebSocket.OPEN || this._state !== 'connected') {
        const error = new RpcTransportError('Not connected', false);
        // The socket can start closing before its close event reaches us.
        // Publish the lost connection so owners cancel reads from this generation.
        if (this._state === 'connected') {
          this._recycleConnection(generation, error, 'socket_not_open');
        }
        reject(error);
        return;
      }
      if (
        options.expectedGeneration !== undefined
        && options.expectedGeneration !== generation
      ) {
        reject(
          new RpcTransportError(
            `Connection generation changed before ${method} was sent`,
            false
          )
        );
        return;
      }

      const id = String(++this._reqId);
      let requestSent = false;
      const pending: PendingRequest = {
        resolve,
        reject,
        method,
        generation,
        timeoutTimer: null,
        signal: options.signal || null,
        abortHandler: null,
        sentAt: null,
        incidentIdAtSend: null,
        recoveryClass: options.recoveryClass || 'mutation',
      };
      this._pending.set(id, pending);

      const terminate = (error: Error, _action: RpcTerminationAction): void => {
        if (
          options.cancelOnAbort
          && requestSent
          && this._pending.get(id)?.generation === generation
          && this._isCurrentSocket(socket, generation)
          && socket.readyState === WebSocket.OPEN
          && this._cancellableRequestMethods().has(method)
        ) {
          try {
            socket.send(JSON.stringify({ type: 'cancel', id }));
          } catch {
            // Cancellation is best-effort. Preserve the existing local abort
            // behavior if the control frame cannot be sent.
          }
        }
        this._rejectPending(id, error, generation);
      };

      if (options.signal) {
        pending.abortHandler = () => terminate(new RpcAbortError(method), options.abortAction || 'reject');
        options.signal.addEventListener('abort', pending.abortHandler, { once: true });
      }

      const recoveryTimeoutMs = this._phase !== 'healthy'
        && isSafeReadRecoveryClass(options.recoveryClass)
        ? Math.min(options.timeoutMs ?? SAFE_READ_TIMEOUT_MS, SAFE_READ_TIMEOUT_MS)
        : options.timeoutMs;
      if (
        recoveryTimeoutMs !== undefined &&
        recoveryTimeoutMs > 0 &&
        Number.isFinite(recoveryTimeoutMs)
      ) {
        pending.timeoutTimer = setTimeout(() => {
          terminate(
            new RpcTimeoutError(method, recoveryTimeoutMs),
            options.timeoutAction || 'reject'
          );
        }, recoveryTimeoutMs);
      }

      let frame: string;
      try {
        frame = JSON.stringify({ type: 'req', id, method, params });
      } catch (error) {
        this._rejectPending(
          id,
          error instanceof Error ? error : new Error('Failed to serialize RPC request'),
          generation
        );
        return;
      }

      try {
        socket.send(frame);
        requestSent = true;
        pending.sentAt = Date.now();
        pending.incidentIdAtSend = this._wakeIncident?.id ?? null;
      } catch (error) {
        const sendError = new RpcTransportError(
          error instanceof Error ? error.message : 'Failed to send RPC request',
          false
        );
        this._rejectPending(id, sendError, generation);
        this._recycleConnection(generation, sendError, 'request_send_failure');
        return;
      }
      try {
        options.onSent?.(generation);
      } catch {
        // A send receipt is observational. It must never fail a request whose
        // frame is already on the wire.
      }
    });
  }

  private _queueRecoveryRead(
    method: string,
    params: Record<string, unknown>,
    options: RpcCallOptions,
    resolve: (value: unknown) => void,
    reject: (error: Error) => void,
  ): void {
    if (this._recoveryReads.length >= SAFE_READ_MAX_PENDING) {
      reject(new RpcTransportError('Too many recovery reads pending', false));
      return;
    }
    const deadlineAt = Date.now() + SAFE_READ_TIMEOUT_MS;
    const item: QueuedRecoveryRead = {
      method,
      params,
      options: { ...options, recoveryClass: 'safe-read' },
      resolve,
      reject,
      deadlineAt,
      timeoutTimer: null,
      abortHandler: null,
    };
    const remove = (): void => {
      const index = this._recoveryReads.indexOf(item);
      if (index >= 0) this._recoveryReads.splice(index, 1);
      if (item.timeoutTimer !== null) {
        clearTimeout(item.timeoutTimer);
        item.timeoutTimer = null;
      }
      if (item.options.signal && item.abortHandler) {
        item.options.signal.removeEventListener('abort', item.abortHandler);
        item.abortHandler = null;
      }
    };
    item.timeoutTimer = setTimeout(() => {
      remove();
      reject(new RpcTimeoutError(method, SAFE_READ_TIMEOUT_MS));
    }, SAFE_READ_TIMEOUT_MS);
    if (options.signal) {
      item.abortHandler = () => {
        remove();
        reject(new RpcAbortError(method));
      };
      options.signal.addEventListener('abort', item.abortHandler, { once: true });
    }
    this._recoveryReads.push(item);
  }

  private _clearQueuedRecoveryReads(error: Error): void {
    const queued = this._recoveryReads.splice(0);
    for (const item of queued) {
      if (item.timeoutTimer !== null) clearTimeout(item.timeoutTimer);
      if (item.options.signal && item.abortHandler) {
        item.options.signal.removeEventListener('abort', item.abortHandler);
      }
      item.reject(error);
    }
  }

  private _flushQueuedRecoveryReads(): void {
    if (this._phase !== 'healthy' || this._state !== 'connected') return;
    const queued = this._recoveryReads.splice(0);
    for (const item of queued) {
      if (item.timeoutTimer !== null) clearTimeout(item.timeoutTimer);
      if (item.options.signal && item.abortHandler) {
        item.options.signal.removeEventListener('abort', item.abortHandler);
      }
      const remainingMs = Math.max(0, item.deadlineAt - Date.now());
      if (remainingMs <= 0) {
        item.reject(new RpcTimeoutError(item.method, SAFE_READ_TIMEOUT_MS));
        continue;
      }
      const requestedTimeout = item.options.timeoutMs;
      const timeoutMs = requestedTimeout === undefined
        ? remainingMs
        : Math.min(Math.max(1, requestedTimeout), remainingMs);
      this.call(item.method, item.params, {
        ...item.options,
        timeoutMs,
        recoveryClass: 'safe-read',
      }).then(item.resolve, item.reject);
    }
  }

  on(event: string, handler: RpcEventHandler): () => void {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event)!.add(handler);
    return () => this._listeners.get(event)?.delete(handler);
  }

  private _emit(event: string, ...args: unknown[]): void {
    const handlers = this._listeners.get(event);
    if (!handlers) return;
    for (const handler of handlers) {
      try {
        handler(...args);
      } catch (error) {
        console.error(`[rpc] "${event}" listener failed`, error);
      }
    }
  }

  get state(): ConnectionState {
    return this._state;
  }

  get lifecycle(): RpcLifecycle {
    if (this._blockedReason) return 'blocked';
    if (!this._autoReconnect) return 'stopped';
    if (this._state === 'connected') return 'connected';
    return this._everConnected || this._reconnectAttempt > 0 ? 'recovering' : 'connecting';
  }

  get recoveryReason(): string | null { return this._blockedReason; }
  get health(): 'healthy' | 'suspect' { return this._health; }
  get phase(): RpcTransportPhase { return this._phase; }

  onGap(handler: (detail: unknown) => Promise<boolean>): () => void {
    this._gapHandlers.add(handler);
    return () => this._gapHandlers.delete(handler);
  }

  enableConsumptionFlow(): void { this._consumptionFlowEnabled = true; }

  async recoverGap(detail: unknown): Promise<boolean> {
    const handlers = [...this._gapHandlers];
    if (!handlers.length) return false;
    const results = await Promise.allSettled(handlers.map(handler => Promise.resolve().then(
      () => handler(detail),
    )));
    return results.every(result => result.status === 'fulfilled' && result.value === true);
  }

  /** Only domain owners register here; observation listeners do not ACK data. */
  onConsumedEvent(event: string, handler: RpcConsumptionHandler): () => void {
    if (!this._consumers.has(event)) this._consumers.set(event, new Set());
    this._consumers.get(event)!.add(handler);
    return () => this._consumers.get(event)?.delete(handler);
  }

  async consumeEvent(
    event: string, payload: unknown, meta: Record<string, unknown>,
  ): Promise<RpcConsumptionResult> {
    const handlers = [...(this._consumers.get(event) || [])];
    if (!handlers.length) throw new Error('No consumption owner for event');
    const results = await Promise.allSettled(handlers.map(handler => Promise.resolve().then(
      () => handler(payload, meta),
    )));
    if (results.some(result => result.status === 'rejected'
      || (result.value !== 'applied' && result.value !== 'dirty'))) {
      throw new Error('Consumption owner did not accept delivery');
    }
    return results.every(result => result.status === 'fulfilled' && result.value === 'applied')
      ? 'applied' : 'dirty';
  }

  get connectionGeneration(): number {
    return this._socketGeneration;
  }

  get policy(): Record<string, unknown> {
    return this._policy || {};
  }

  private _cancellableRequestMethods(): Set<string> {
    const methods = this._policy?.cancellable_request_methods;
    return new Set(isStringArray(methods) ? methods : []);
  }

  /**
   * Recover a connection whose server-side state may no longer be consistent.
   *
   * The generation fence prevents cleanup from an obsolete session lease from
   * retiring a replacement socket that it never owned.
   */
  recoverConnectionGeneration(
    expectedGeneration: number,
    reason: string = 'Connection consistency recovery requested'
  ): boolean {
    if (!this._ws || expectedGeneration !== this._socketGeneration) return false;
    this._recycleConnection(
      expectedGeneration,
      new Error(reason),
      'generation_consistency_recovery'
    );
    return true;
  }

  ready(
    timeoutMs: number = 30000,
    signal?: AbortSignal,
    actions: RpcConnectionWaitOptions = {}
  ): Promise<void> {
    if (signal?.aborted) {
      // No wait and no request ever started, so this caller owns no socket to
      // recycle. Retiring the current connection here could kill a newer
      // session's healthy generation.
      return Promise.reject(new RpcAbortError('ready'));
    }
    if (this._state === 'connected') return Promise.resolve();
    if (this._blockedReason) return Promise.reject(new RpcTransportError(this._blockedReason, false));

    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      let settled = false;
      let off: () => void = () => {};
      let offStatus: () => void = () => {};

      const cleanup = (): void => {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        off();
        offStatus();
        signal?.removeEventListener('abort', onAbort);
      };
      const finish = (
        error?: Error,
        _action: RpcTerminationAction = 'reject'
      ): void => {
        if (settled) return;
        settled = true;
        cleanup();
        if (!error) {
          resolve();
          return;
        }
        reject(error);
      };
      const onAbort = (): void => {
        finish(
          new RpcAbortError('ready'),
          actions.abortAction || 'reject'
        );
      };

      off = this.on('_state', (s: ConnectionState) => {
        if (s === 'connected') {
          finish();
        }
      });
      offStatus = this.on('_status', () => {
        if (this._blockedReason) finish(new RpcTransportError(this._blockedReason, false));
      });
      signal?.addEventListener('abort', onAbort, { once: true });
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timer = setTimeout(() => {
          finish(
            new RpcTimeoutError('ready', timeoutMs),
            actions.timeoutAction || 'reject'
          );
        }, timeoutMs);
      }
    });
  }

  private _doConnect(): void {
    if (this._ws || !this._autoReconnect || this._blockedReason) return;
    try {
      const url = new URL(this._url);
      if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error('Invalid WebSocket URL');
    } catch {
      this._blockConnection('invalid_url');
      return;
    }
    this._setState('connecting');
    this._lastSeq = 0;
    this._lastFrameAt = Date.now();
    this._stopTickWatch();
    const generation = ++this._socketGeneration;
    this._firstSuccessfulRpc = false;
    // The incident deadline belongs to the wake operation, not to one socket.
    // Carry it onto a replacement generation when resume found no usable
    // socket or an in-flight connection was replaced.
    if (this._wakeIncident) this._wakeIncident.generation = generation;
    this._emitTransport('connect_start', generation, {
      reason: 'connect_requested',
    });
    if (!this._autoReconnect || generation !== this._socketGeneration || this._ws) return;
    let socket: WebSocket;
    try {
      socket = new WebSocket(this._url);
    } catch {
      if (generation !== this._socketGeneration) return;
      this._markRecoveryStarted();
      this._setState('disconnected');
      this._scheduleReconnect();
      return;
    }
    this._ws = socket;
    this._armChallengeWatchdog(socket, generation);
    let handshakeRequestId: string | null = null;
    let handshakeRequestSent = false;

    socket.onopen = () => {
      if (!this._isCurrentSocket(socket, generation)) return;
      // Don't send connect yet — wait for connect.challenge from server
    };

    socket.onmessage = (ev: MessageEvent) => {
      if (!this._isCurrentSocket(socket, generation)) return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        if (this._state === 'connecting') {
          this._failInvalidHello(socket, generation, 'connect_frame_before_hello');
        }
        return;
      }
      if (!isRecord(parsed)) {
        if (this._state === 'connecting') {
          this._failInvalidHello(socket, generation, 'connect_frame_before_hello');
        }
        return;
      }
      const data = parsed as RpcFrame;

      if (this._state === 'connecting') {
        const helloCandidate = isHelloOkCandidate(parsed);
        if (helloCandidate) {
          // A positively identified protocol incompatibility cannot heal by
          // redialling. Malformed/unexpected pre-Hello data instead retires only
          // this generation and remains eligible for bounded-backoff recovery.
          if (handshakeRequestSent && handshakeRequestId !== null
            && parsed.type === 'hello-ok' && Number.isInteger(parsed.protocol)
            && parsed.protocol !== WEB_RPC_PROTOCOL_VERSION) {
            this._blockConnection('protocol_rejected');
            return;
          }
          if (
            !handshakeRequestSent
            || handshakeRequestId === null
            || !isHelloOkFrame(parsed)
          ) {
            this._failInvalidHello(socket, generation);
            return;
          }

          const hello = parsed;
          const principal = isRecord(hello.auth?.principal) ? hello.auth.principal : undefined;
          // Gateway auth:none grants a loopback owner authState=authenticated,
          // but authenticated=false means no token was verified. Preserve that
          // legitimate Desktop owner while keeping explicit-token intent strict.
          const ownerAuthorized = principal?.isOwner === true
            && (principal.authenticated === true || principal.authState === 'authenticated');
          if ((this._intent.authentication === 'authenticated' && principal?.authenticated !== true)
            || (this._intent.authentication === 'owner' && !ownerAuthorized)) {
            this._blockConnection('authentication_mismatch');
            return;
          }
          // The authenticated Hello is a liveness proof for this exact socket.
          // onmessage is generation/socket fenced above, and _clearWakeProbe
          // refuses to clear a deadline owned by any replacement generation.
          this._clearWakeProbe(generation);
          this._completeWakeIncident(generation, 'hello');
          this._clearHandshakeWatchdogs(generation);
          const recoveryMs = this._recoveryStartedAt === null
            ? undefined : Math.max(0, Date.now() - this._recoveryStartedAt);
          this._recoveryStartedAt = null;
          this._emitTransport('hello', generation, {
            reason: 'authenticated',
            ...(recoveryMs === undefined ? {} : { recoveryMs }),
            connId: hello.server.conn_id,
          });
          if (!this._isCurrentSocket(socket, generation)) return;
          this._policy = hello.policy;
          const serverGuestSessionKey = hello.auth?.guestSessionKey;
          if (
            typeof serverGuestSessionKey === 'string'
            && GUEST_SESSION_KEY_PATTERN.test(serverGuestSessionKey)
          ) {
            this._guestSessionKey = serverGuestSessionKey;
            persistGuestSessionKey(serverGuestSessionKey);
          }
          this._resolvePending(handshakeRequestId, hello, generation);
          handshakeRequestId = null;
          handshakeRequestSent = false;
          // A flapping valid Hello is not a stable connection. Preserve backoff
          // until this exact authenticated generation stays ready for 30 seconds.
          this._everConnected = true;
          this._stableTimer = setTimeout(() => {
            if (this._isCurrentSocket(socket, generation) && this._state === 'connected') {
              this._reconnectAttempt = 0;
            }
          }, CONNECTION_STABLE_MS);
          // Identity, method capabilities and flow policy must be installed by
          // synchronous owners before consumers observe connection readiness.
          this._emit('_hello', hello);
          if (!this._isCurrentSocket(socket, generation)) return;
          // Clear a previous suspect state before publishing connected. A
          // synchronous state listener may issue its first request immediately.
          this._setPhase('healthy');
          this._setHealth('healthy');
          if (!this._isCurrentSocket(socket, generation)) return;
          this._setState('connected');
          if (!this._isCurrentSocket(socket, generation)) return;
          this._startPing();
          this._startTickWatch();
          return;
        }

        // Handshake: server sends connect.challenge, we reply with connect request.
        // No pre-Hello frame participates in the application event sequence.
        if (
          data.type === 'event'
          && data.event === 'connect.challenge'
          && handshakeRequestId === null
        ) {
          this._clearChallengeWatchdog(generation);
          this._emitTransport('challenge', generation, {
            reason: 'server_challenge',
          });
          if (!this._isCurrentSocket(socket, generation)) return;
          const authParams = {
            auth: {
              ...(this._token ? { token: this._token } : {}),
              guestSessionKey: this._guestSessionKey || loadGuestSessionKey(),
            },
          };
          const id = String(++this._reqId);
          handshakeRequestId = id;
          this._pending.set(id, {
            resolve: () => {},
            reject: (error: Error) => {
              if (!this._isCurrentSocket(socket, generation)) return;
              const code = (error as RpcClientError).code;
              if (code === 'UNAUTHORIZED' || code === 'INVALID_REQUEST') {
                this._blockConnection(code === 'UNAUTHORIZED' ? 'authentication_failed' : 'protocol_rejected');
              } else {
                this._recycleConnection(generation, new Error('Connect handshake failed'), 'connect_request_failure');
              }
            },
            method: 'connect',
            generation,
            timeoutTimer: null,
            signal: null,
            abortHandler: null,
            sentAt: null,
            incidentIdAtSend: null,
            recoveryClass: 'safe-read',
          });
          try {
            socket.send(
              JSON.stringify({
                type: 'req',
                id,
                method: 'connect',
                params: {
                  minProtocol: WEB_RPC_PROTOCOL_VERSION,
                  maxProtocol: WEB_RPC_PROTOCOL_VERSION,
                  caps: [
                    ANSWER_GENERATION_RESET_CAPABILITY,
                    TURN_COMMITTED_CAPABILITY,
                    PROBE_CAPABILITY,
                    ...(this._consumptionFlowEnabled ? ['transport.flow.v1', 'transport.recovery.v1'] : []),
                  ],
                  client: { name: 'opensquilla-web' },
                  ...authParams,
                },
              })
            );
            handshakeRequestSent = true;
            const handshakePending = this._pending.get(id);
            if (handshakePending) {
              handshakePending.sentAt = Date.now();
              handshakePending.incidentIdAtSend = this._wakeIncident?.id ?? null;
            }
            this._armHelloWatchdog(socket, generation);
          } catch (error) {
            const sendError =
              error instanceof Error ? error : new Error('Failed to send connect request');
            this._rejectPending(id, sendError, generation);
            this._recycleConnection(generation, sendError, 'connect_send_failure');
          }
          return;
        }

        // Authentication failures use the ordinary response envelope. Only an
        // error for this exact connect request may run before Hello completes.
        if (
          handshakeRequestSent
          && handshakeRequestId !== null
          && data.type === 'res'
          && data.id === handshakeRequestId
          && data.ok === false
        ) {
          const id = handshakeRequestId;
          handshakeRequestId = null;
          handshakeRequestSent = false;
          this._rejectPending(id, rpcResponseError(data.error), generation);
          return;
        }

        this._failInvalidHello(socket, generation, 'connect_frame_before_hello');
        return;
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      if (!this._noteIncomingFrame(data)) return;

      if (data.type === 'res') {
        const id = data.id ?? '';
        const pending = this._pending.get(id);
        if (
          pending?.generation === generation
          && (this._wakeIncident === null
            || pending.incidentIdAtSend === this._wakeIncident.id)
        ) {
          this._noteRoundTrip(generation);
        }
        if (!this._isCurrentSocket(socket, generation)) return;
        if (data.ok) {
          if (pending?.generation === generation && this._pending.get(id) === pending && !this._firstSuccessfulRpc) {
            this._firstSuccessfulRpc = true;
            this._emitTransport('first_successful_rpc', generation, {
              roundTripMs: pending.sentAt === null ? null : Math.max(0, Date.now() - pending.sentAt),
            });
          }
          if (!this._resolvePending(id, data.payload, generation)) {
            // A request-local timeout/abort can leave a late response carrying
            // connection-owned resources. Adapters alone recognize its schema
            // and may explicitly discard those resources. No business replay,
            // semantic installation, or unbounded request tombstone lives here.
            this._emit('_orphan_response', { id, payload: data.payload, generation });
          }
        } else {
          this._rejectPending(id, rpcResponseError(data.error), generation);
        }
      } else if (data.type === 'event') {
        const meta = data.meta || {};
        this._emit(data.event ?? '', data.payload, meta);
        this._emit('*', data.event, data.payload, meta);
      }
    };

    socket.onclose = (event: CloseEvent) => {
      if (!this._isCurrentSocket(socket, generation)) return;
      this._markRecoveryStarted();
      this._emitTransport('close', generation, {
        code: event?.code ?? 1006,
        reason: event?.reason || 'socket_closed',
        wasClean: event?.wasClean ?? false,
      });
      if (!this._isCurrentSocket(socket, generation)) return;
      this._ws = null;
      this._clearGapRecovery();
      this._clearHandshakeWatchdogs(generation);
      ++this._socketGeneration;
      this._clearWakeProbe(generation);
      if (this._wakeIncident) this._wakeIncident.generation = this._socketGeneration;
      this._stopPing();
      this._stopTickWatch();
      this._clearStableTimer();
      this._rejectPendingForGeneration(
        generation,
        new RpcTransportError('Connection closed', null)
      );
      this._setState('disconnected');
      this._scheduleReconnect();
    };

    socket.onerror = () => {};
  }

  private _isCurrentSocket(socket: WebSocket, generation: number): boolean {
    return this._ws === socket && this._socketGeneration === generation;
  }

  private _armChallengeWatchdog(socket: WebSocket, generation: number): void {
    this._clearChallengeWatchdog();
    this._challengeWatchdogGeneration = generation;
    this._challengeWatchdogTimer = setTimeout(() => {
      if (!this._isCurrentSocket(socket, generation) || this._state !== 'connecting') {
        return;
      }
      this._failHandshake(
        socket,
        generation,
        'connect_challenge_timeout',
        'Timed out waiting for connect challenge'
      );
    }, CONNECT_CHALLENGE_TIMEOUT_MS);
  }

  private _armHelloWatchdog(socket: WebSocket, generation: number): void {
    this._clearHelloWatchdog();
    this._helloWatchdogGeneration = generation;
    this._helloWatchdogTimer = setTimeout(() => {
      if (!this._isCurrentSocket(socket, generation) || this._state !== 'connecting') {
        return;
      }
      this._failHandshake(
        socket,
        generation,
        'connect_hello_timeout',
        'Timed out waiting for connect hello'
      );
    }, CONNECT_HELLO_TIMEOUT_MS);
  }

  private _failHandshake(
    socket: WebSocket,
    generation: number,
    reason: string,
    message: string
  ): void {
    if (!this._isCurrentSocket(socket, generation)) return;
    this._markRecoveryStarted();
    this._emitTransport('watchdog_timeout', generation, { reason });
    if (!this._isCurrentSocket(socket, generation)) return;
    this._emit('_gap', { reason, generation });
    if (!this._isCurrentSocket(socket, generation)) return;
    this._retireCurrentSocket(new Error(message), false, reason);
    this._scheduleReconnect();
  }

  private _failInvalidHello(
    socket: WebSocket,
    generation: number,
    reason: string = 'connect_hello_invalid'
  ): void {
    if (!this._isCurrentSocket(socket, generation)) return;
    this._markRecoveryStarted();
    this._emitTransport('handshake_invalid', generation, { reason });
    if (!this._isCurrentSocket(socket, generation)) return;
    this._emit('_gap', { reason, generation });
    if (!this._isCurrentSocket(socket, generation)) return;
    this._retireCurrentSocket(new Error('Invalid connect hello'), false, reason);
    this._scheduleReconnect();
  }

  private _clearChallengeWatchdog(generation?: number): void {
    if (
      generation !== undefined
      && this._challengeWatchdogGeneration !== null
      && this._challengeWatchdogGeneration !== generation
    ) {
      return;
    }
    if (this._challengeWatchdogTimer !== null) {
      clearTimeout(this._challengeWatchdogTimer);
      this._challengeWatchdogTimer = null;
    }
    this._challengeWatchdogGeneration = null;
  }

  private _clearHelloWatchdog(generation?: number): void {
    if (
      generation !== undefined
      && this._helloWatchdogGeneration !== null
      && this._helloWatchdogGeneration !== generation
    ) {
      return;
    }
    if (this._helloWatchdogTimer !== null) {
      clearTimeout(this._helloWatchdogTimer);
      this._helloWatchdogTimer = null;
    }
    this._helloWatchdogGeneration = null;
  }

  private _clearHandshakeWatchdogs(generation?: number): void {
    this._clearChallengeWatchdog(generation);
    this._clearHelloWatchdog(generation);
  }

  private _emitTransport(
    phase: string,
    generation: number,
    detail: Record<string, unknown> = {}
  ): void {
    this._emit('_transport', {
      at: Date.now(),
      phase,
      generation,
      topology: this._transportTopology(),
      visibility: typeof document === 'undefined' ? 'unknown' : document.visibilityState,
      health: this._health,
      suspectAt: this._suspectAt,
      reconnectAttempt: this._reconnectAttempt,
      lastRxAt: this._lastFrameAt,
      loopLagMs: this._lastLoopLagMs,
      maxLoopLagMs: this._maxLoopLagMs,
      transportPhase: this._phase,
      wakeIncidentId: this._wakeIncident?.id ?? null,
      wakeIncidentStartedAt: this._wakeIncident?.startedAt ?? null,
      wakeIncidentDeadlineAt: this._wakeIncident?.deadlineAt ?? null,
      wakeIncidentStatus: this._wakeIncident?.status ?? null,
      wakeIncidentSource: this._wakeIncident?.source ?? null,
      wakeIncidentProbeTimeoutMs: this._wakeIncident?.probeTimeoutMs ?? null,
      wakeSignalCount: this._wakeIncident?.signals ?? 0,
      ...detail,
    });
  }

  private _transportTopology(): 'loopback' | 'remote' | 'proxy/vpn' | 'unknown' {
    if (this._intent.topology) return this._intent.topology;
    try {
      const hostname = new URL(this._url).hostname;
      return hostname === 'localhost' || hostname === '[::1]' || /^127\./.test(hostname)
        ? 'loopback' : 'remote';
    } catch {
      return 'unknown';
    }
  }

  private _beginWakeIncident(
    generation: number,
    startedAt: number,
    source: RpcResumeSource,
  ): void {
    this._clearWakeIncident();
    const incident: WakeIncident = {
      id: ++this._wakeIncidentCounter,
      generation,
      startedAt,
      deadlineAt: startedAt + WAKE_INCIDENT_BUDGET_MS,
      source,
      probeTimeoutMs: source === 'desktop-resume' ? NATIVE_RESUME_PROBE_TIMEOUT_MS : PROBE_TIMEOUT_MS,
      status: this._health === 'suspect' ? 'suspect' : 'probing',
      signals: 1,
    };
    this._wakeIncident = incident;
    this._setPhase('checking');
    const softSuspectAt = startedAt + SOFT_SUSPECT_MS;
    this._wakeSoftSuspectTimer = setTimeout(() => {
      if (
        this._wakeIncident?.id !== incident.id
        || this._health === 'suspect'
      ) return;
      this._wakeSoftSuspectTimer = null;
      const lagMs = Date.now() - softSuspectAt;
      const activeGeneration = this._socketGeneration;
      if (lagMs > SCHEDULER_LAG_MS) {
        this._emitTransport('soft_suspect_deferred', activeGeneration, {
          incidentId: incident.id,
          reason: 'scheduler_lag',
          lagMs,
        });
        this._wakeSoftSuspectTimer = setTimeout(() => {
          if (this._wakeIncident?.id !== incident.id || this._health === 'suspect') return;
          incident.status = 'suspect';
          this._setPhase('suspect');
          this._setHealth('suspect');
          this._emitTransport('soft_suspect', this._socketGeneration, {
            incidentId: incident.id,
            reason: 'wake_probe_unconfirmed',
          });
        }, 1_000);
        return;
      }
      incident.status = 'suspect';
      this._setPhase('suspect');
      this._setHealth('suspect');
      this._emitTransport('soft_suspect', activeGeneration, {
        incidentId: incident.id,
        reason: 'wake_probe_unconfirmed',
      });
    }, SOFT_SUSPECT_MS);
    this._wakeIncidentTimer = setTimeout(() => {
      if (
        this._wakeIncident?.id !== incident.id
      ) return;
      const activeGeneration = this._socketGeneration;
      this._wakeIncidentTimer = null;
      this._suspectAt ??= Date.now();
      incident.status = 'reconnecting';
      this._setPhase('reconnecting');
      this._setHealth('suspect');
      // Store owners may proxy this client and its incident through Vue ref().
      // Stable IDs preserve the reentrancy fence across raw/proxied access.
      if (
        this._wakeIncident?.id !== incident.id
      ) return;
      this._emitTransport('wake_incident_timeout', activeGeneration, {
        incidentId: incident.id,
        reason: 'wake_incident_timeout',
      });
      if (
        this._wakeIncident?.id !== incident.id
      ) return;
      this._clearWakeIncident(undefined, incident.id);
      this._recycleConnection(
        activeGeneration,
        new Error('Wake incident budget expired'),
        'wake_incident_timeout',
      );
    }, WAKE_INCIDENT_BUDGET_MS);
    this._emitTransport('wake_incident_start', generation, {
      incidentId: incident.id,
      deadlineAt: incident.deadlineAt,
    });
  }

  private _completeWakeIncident(generation: number, reason: string): void {
    const incident = this._wakeIncident;
    if (!incident || incident.generation !== generation) return;
    incident.status = 'recovered';
    // Retire ownership before notifying observers: a reentrant wake starts a
    // new incident and must not be deduplicated into this completed one.
    this._clearWakeIncident(generation, incident.id);
    this._setPhase('healthy');
    this._emitTransport('wake_incident_recovered', generation, {
      incidentId: incident.id,
      reason,
      recoveryMs: Math.max(0, Date.now() - incident.startedAt),
      wakeIncidentId: incident.id,
      wakeIncidentStartedAt: incident.startedAt,
      wakeIncidentDeadlineAt: incident.deadlineAt,
      wakeIncidentStatus: incident.status,
      wakeSignalCount: incident.signals,
    });
  }

  private _clearWakeIncident(generation?: number, incidentId?: number): void {
    const incident = this._wakeIncident;
    if (!incident) return;
    if (generation !== undefined && incident.generation !== generation) return;
    if (incidentId !== undefined && incident.id !== incidentId) return;
    if (this._wakeIncidentTimer !== null) clearTimeout(this._wakeIncidentTimer);
    this._wakeIncidentTimer = null;
    if (this._wakeSoftSuspectTimer !== null) clearTimeout(this._wakeSoftSuspectTimer);
    this._wakeSoftSuspectTimer = null;
    this._wakeIncident = null;
  }

  private _blockConnection(reason: string): void {
    this._blockedReason = reason;
    this._recoveryStartedAt = null;
    this._clearReconnectTimer();
    this._clearQueuedRecoveryReads(new RpcTransportError(reason, false));
    this._retireCurrentSocket(new Error(reason), false, reason);
    this._emit('_blocked', { reason });
    this._emitStatus();
  }

  private _clearStableTimer(): void {
    if (this._stableTimer !== null) clearTimeout(this._stableTimer);
    this._stableTimer = null;
  }

  private _emitStatus(): void {
    this._emit('_status', {
      lifecycle: this.lifecycle,
      reason: this.recoveryReason,
      health: this._health,
      phase: this._phase,
    });
  }

  private _setPhase(phase: RpcTransportPhase): void {
    if (this._phase === phase) return;
    this._phase = phase;
    this._emitStatus();
    this._flushQueuedRecoveryReads();
  }

  private _setHealth(health: 'healthy' | 'suspect'): void {
    if (this._health === health) return;
    this._health = health;
    this._emitStatus();
  }

  private _noteRoundTrip(generation: number = this._socketGeneration): void {
    if (generation !== this._socketGeneration) return;
    this._clearWakeProbe();
    this._suspectAt = null;
    this._suspectProbes = 0;
    const healthChanged = this._health !== 'healthy';
    // Commit the complete recovery before emitting either observer callback.
    // A callback may start a new incident on this same socket.
    this._health = 'healthy';
    this._completeWakeIncident(generation, 'round_trip');
    this._setPhase('healthy');
    if (generation !== this._socketGeneration) return;
    if (healthChanged) this._emitStatus();
  }

  private _takePending(id: string, generation?: number): PendingRequest | undefined {
    const pending = this._pending.get(id);
    if (!pending || (generation !== undefined && pending.generation !== generation)) {
      return undefined;
    }
    this._pending.delete(id);
    if (pending.timeoutTimer !== null) {
      clearTimeout(pending.timeoutTimer);
      pending.timeoutTimer = null;
    }
    if (pending.signal && pending.abortHandler) {
      pending.signal.removeEventListener('abort', pending.abortHandler);
      pending.abortHandler = null;
    }
    return pending;
  }

  private _resolvePending(id: string, value: unknown, generation?: number): boolean {
    const pending = this._takePending(id, generation);
    if (!pending) return false;
    pending.resolve(value);
    return true;
  }

  private _rejectPending(id: string, error: Error, generation?: number): boolean {
    const pending = this._takePending(id, generation);
    if (!pending) return false;
    pending.reject(error);
    return true;
  }

  private _rejectPendingForGeneration(generation: number, error: Error): void {
    for (const [id, pending] of [...this._pending]) {
      if (pending.generation === generation) {
        this._rejectPending(id, error, generation);
      }
    }
  }

  private _rejectAllPending(error: Error): void {
    for (const id of [...this._pending.keys()]) {
      this._rejectPending(id, error);
    }
  }

  private _retireCurrentSocket(
    error: Error,
    reconnect: boolean,
    reason: string = 'internal_retire'
  ): void {
    const socket = this._ws;
    const generation = this._socketGeneration;
    this._clearGapRecovery();
    this._clearStableTimer();
    this._clearWakeProbe(generation);
    if (!this._wakeIncident || reason === 'client_disconnect' || reason === 'connection_replaced') {
      this._clearWakeIncident(generation);
    }
    this._clearHandshakeWatchdogs(generation);
    this._emitTransport('retire', generation, { reason });
    // Diagnostic listeners are isolated, but they may still deliberately
    // replace the connection. Never let this retirement continue onto a socket
    // installed by such a listener.
    if (this._ws !== socket || this._socketGeneration !== generation) return;
    if (!socket) {
      this._stopPing();
      this._stopTickWatch();
      this._setState('disconnected');
      if (reconnect) this._scheduleReconnect(true);
      return;
    }

    this._ws = null;
    ++this._socketGeneration;
    if (this._wakeIncident) this._wakeIncident.generation = this._socketGeneration;
    this._stopPing();
    this._stopTickWatch();
    // The request that triggered retirement has already been removed. Every
    // remaining request may have reached the Gateway, even when the triggering
    // send itself was rejected before acceptance.
    const pendingError = new RpcTransportError(error.message, null);
    this._rejectPendingForGeneration(generation, pendingError);
    this._setState('disconnected');
    try {
      socket.close();
    } catch {}
    if (reconnect) this._scheduleReconnect(true);
  }

  private _recycleConnection(
    generation: number,
    error: Error,
    reason: string = 'transport_recovery'
  ): void {
    if (generation !== this._socketGeneration) return;
    this._markRecoveryStarted();
    this._setPhase('reconnecting');
    this._retireCurrentSocket(error, true, reason);
  }

  private _markRecoveryStarted(): void {
    if (this._autoReconnect && !this._blockedReason && this._recoveryStartedAt === null) {
      this._recoveryStartedAt = Date.now();
    }
  }

  private _clearReconnectTimer(): void {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  private _startPing(): void {
    this._stopPing();
    this._lastProbeAt = Date.now();
    this._pingTimer = setInterval(() => this._sendProbe(), this._pingInterval);
  }

  private _sendProbe(): void {
    const socket = this._ws;
    if (!socket || this._state !== 'connected'
      || this._wakeProbeGeneration !== null || Date.now() < this._graceUntil) return;
    if (socket.readyState !== WebSocket.OPEN) {
      const generation = this._socketGeneration;
      this._emitTransport('probe_socket_unavailable', generation, {
        readyState: socket.readyState,
        reason: 'socket_not_open',
      });
      this._recycleConnection(generation, new Error('Probe socket is not open'), 'probe_socket_unavailable');
      return;
    }
    if (this._suspectAt !== null && this._suspectProbes >= 2) return;
    const generation = this._socketGeneration;
    this._probeNonce = this._policy?.transport_probe_nonce === true
      ? `${generation}:${++this._probeCounter}` : null;
    const nonce = this._probeNonce;
    try {
      socket.send(JSON.stringify({ type: 'ping', ...(nonce ? { nonce } : {}) }));
    } catch {
      this._recycleConnection(generation, new Error('Probe send failed'), 'probe_send_failure');
      return;
    }
    this._lastProbeAt = Date.now();
    if (this._suspectAt !== null) this._suspectProbes += 1;
    this._armWakeProbe(
      socket,
      generation,
      nonce,
      this._wakeIncident?.probeTimeoutMs ?? PROBE_TIMEOUT_MS,
    );
  }

  private _stopPing(): void {
    if (this._pingTimer !== null) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
  }

  private _startLifecycleWatch(): void {
    if (
      this._lifecycleWatchStarted
      || typeof window === 'undefined'
      || typeof document === 'undefined'
    ) {
      return;
    }
    this._lifecycleWatchStarted = true;
    window.addEventListener('online', this._handleWakeSignal);
    window.addEventListener('pageshow', this._handleWakeSignal);
    document.addEventListener('visibilitychange', this._handleWakeSignal);
    document.addEventListener('resume', this._handleWakeSignal);
  }

  private _stopLifecycleWatch(): void {
    if (
      this._lifecycleWatchStarted
      && typeof window !== 'undefined'
      && typeof document !== 'undefined'
    ) {
      window.removeEventListener('online', this._handleWakeSignal);
      window.removeEventListener('pageshow', this._handleWakeSignal);
      document.removeEventListener('visibilitychange', this._handleWakeSignal);
      document.removeEventListener('resume', this._handleWakeSignal);
    }
    this._lifecycleWatchStarted = false;
    if (this._wakeDebounceTimer !== null) {
      clearTimeout(this._wakeDebounceTimer);
      this._wakeDebounceTimer = null;
    }
    this._clearWakeProbe();
    this._clearWakeIncident();
  }

  private _scheduleWakeProbe(): void {
    if (!this._autoReconnect) return;
    if (this._wakeDebounceTimer !== null) {
      clearTimeout(this._wakeDebounceTimer);
    }
    this._wakeDebounceTimer = setTimeout(() => {
      this._wakeDebounceTimer = null;
      this._runWakeProbe();
    }, WAKE_DEBOUNCE_MS);
  }

  private _runWakeProbe(): void {
    if (!this._autoReconnect || this._blockedReason) return;
    const incident = this._wakeIncident;
    if (incident?.source === 'desktop-resume') {
      // Native resume has a two-second decision window.  There is no useful
      // probe to send while a socket is still handshaking, closing, or absent;
      // move through the same generation-safe replacement path instead of
      // silently waiting for the twenty-second incident deadline.
      if (!this._ws) {
        this._clearReconnectTimer();
        this._doConnect();
        return;
      }
      if (this._ws.readyState !== WebSocket.OPEN || this._state !== 'connected') {
        this._recycleConnection(
          this._socketGeneration,
          new Error('Native resume socket is not probeable'),
          'native_resume_socket_unavailable',
        );
        return;
      }
    } else if (!this._ws) this.ensureConnected();
    else if (this._ws.readyState !== WebSocket.OPEN && this._ws.readyState !== WebSocket.CONNECTING) {
      this._recycleConnection(this._socketGeneration, new Error('Socket closed after wake'), 'wake_socket_stale');
    } else this._sendProbe();
    if (incident?.source === 'desktop-resume') this._sendProbe();
  }

  private _armWakeProbe(
    socket: WebSocket,
    generation: number,
    nonce: string | null,
    timeoutMs: number = PROBE_TIMEOUT_MS,
  ): void {
    this._clearWakeProbe();
    this._probeNonce = nonce;
    this._wakeProbeGeneration = generation;
    const dueAt = Date.now() + timeoutMs;
    this._wakeProbeTimer = setTimeout(() => {
      if (!this._isCurrentSocket(socket, generation)) return;
      if (Date.now() - dueAt > SCHEDULER_LAG_MS || Date.now() < this._graceUntil) {
        this._clearWakeProbe(generation);
        this._emitTransport('probe_deferred', generation, {
          reason: Date.now() - dueAt > SCHEDULER_LAG_MS ? 'scheduler_lag' : 'wake_grace',
        });
        this.notifyResume();
        return;
      }
      const incident = this._wakeIncident;
      // A native resume has a deliberately short probe budget. Failure means
      // the old generation is no longer trustworthy, so recycle immediately.
      if (incident?.generation === generation && incident.source === 'desktop-resume') {
        this._clearWakeProbe(generation);
        this._emitTransport('probe_timeout', generation, { reason: 'native_resume_probe_timeout' });
        this._recycleConnection(
          generation,
          new Error('Native resume probe timed out'),
          'native_resume_probe_timeout',
        );
        return;
      }
      // Browser wake incidents retain the current nonce until the 20-second
      // incident deadline. This keeps a delayed but generation-matching pong
      // at 13 seconds a valid recovery while the UI is already suspect.
      if (incident?.generation === generation && incident.source !== 'desktop-resume') {
        this._wakeProbeTimer = null;
      } else {
        this._clearWakeProbe(generation);
      }
      if (this._suspectAt === null) {
        this._suspectAt = Date.now();
        if (this._wakeIncident?.generation === generation) this._wakeIncident.status = 'suspect';
        this._suspectProbes = 0;
        this._setPhase('suspect');
        this._setHealth('suspect');
        this._emitTransport('probe_timeout', generation, { reason: 'control_unconfirmed' });
      }
    }, timeoutMs);
  }

  private _clearWakeProbe(generation?: number): void {
    if (
      generation !== undefined
      && this._wakeProbeGeneration !== null
      && this._wakeProbeGeneration !== generation
    ) {
      return;
    }
    if (this._wakeProbeTimer !== null) {
      clearTimeout(this._wakeProbeTimer);
      this._wakeProbeTimer = null;
    }
    this._wakeProbeGeneration = null;
    this._probeNonce = null;
  }

  private _noteIncomingFrame(data: RpcFrame): boolean {
    this._lastFrameAt = Date.now();
    if (data?.type === 'pong' && this._wakeProbeGeneration === this._socketGeneration
      && (this._probeNonce === null ? data.nonce === undefined : data.nonce === this._probeNonce)) {
      this._noteRoundTrip();
    }
    // Gateway ticks are an inbound liveness proof for the current generation
    // even when a nonce pong was delayed by the browser or writer queue.
    if (data?.type === 'event' && data.event === 'tick'
      && this._wakeIncident?.generation === this._socketGeneration) {
      this._noteRoundTrip(this._socketGeneration);
    }
    if (!data || data.type !== 'event' || typeof data.seq !== 'number') return true;

    const seq = data.seq;
    if (this._lastSeq > 0 && seq !== this._lastSeq + 1) {
      const generation = this._socketGeneration;
      const detail = { expected: this._lastSeq + 1, actual: seq, event: data.event };
      this._emit('_gap', detail);
      if (generation !== this._socketGeneration) return false;
      if (this._gapHandlers.size > 0 || this._consumptionFlowEnabled) {
        this._lastSeq = seq;
        // This is a global transport-sequence recovery intent. The newest gap
        // subsumes earlier queued gaps; do not retain any event body here.
        this._pendingGap = detail;
        this._tryRecoverPendingGap();
        return this._consumptionFlowEnabled && !!data.meta?.flow;
      }
      try {
        this._ws?.close();
      } catch {}
      return false;
    }
    this._lastSeq = seq;
    return true;
  }

  private _tryRecoverPendingGap(): void {
    if (this._pendingGap === null || this._gapRecovery || this._gapRetryTimer !== null
      || !this._autoReconnect || this._blockedReason) return;
    const generation = this._socketGeneration;
    const pending = this._pendingGap;
    this._pendingGap = null;
    const recovery = Promise.resolve().then(() => {
      if (generation !== this._socketGeneration || !this._autoReconnect || this._blockedReason) return false;
      return this.recoverGap(pending);
    }).catch(() => false).then(recovered => {
      if (generation !== this._socketGeneration || !this._autoReconnect || this._blockedReason) return;
      if (!recovered) {
        // A missing domain owner or a failed snapshot does not prove transport
        // failure. Keep responsibility without closing the healthy socket.
        if (this._pendingGap === null) this._pendingGap = pending;
        const timer = setTimeout(() => {
          if (this._gapRetryTimer !== timer) return;
          this._gapRetryTimer = null;
          if (generation === this._socketGeneration) this._tryRecoverPendingGap();
        }, 1_000);
        this._gapRetryTimer = timer;
      }
    }).finally(() => {
      if (this._gapRecovery !== recovery) return;
      this._gapRecovery = null;
      this._tryRecoverPendingGap();
    });
    this._gapRecovery = recovery;
  }

  private _clearGapRecovery(): void {
    this._gapRecovery = null;
    this._pendingGap = null;
    if (this._gapRetryTimer !== null) clearTimeout(this._gapRetryTimer);
    this._gapRetryTimer = null;
  }

  private _startTickWatch(): void {
    this._stopTickWatch();
    this._lastFrameAt = Date.now();
    this._lastHealthCheckAt = Date.now();
    this._suspectAt = null;
    this._suspectProbes = 0;
    this._setHealth('healthy');
    this._tickWatchTimer = setInterval(() => {
      const now = Date.now();
      const lag = now - this._lastHealthCheckAt - 1_000;
      this._lastHealthCheckAt = now;
      this._lastLoopLagMs = Math.max(0, lag);
      this._maxLoopLagMs = Math.max(this._maxLoopLagMs, this._lastLoopLagMs);
      if (lag > SCHEDULER_LAG_MS) {
        this._emitTransport('scheduler_lag', this._socketGeneration, { lagMs: lag });
        this.notifyResume();
        return;
      }
      if (this._state !== 'connected' || now < this._graceUntil) return;
      if (this._suspectAt !== null && now - this._suspectAt >= SUSPECT_WINDOW_MS) {
        this._recycleConnection(this._socketGeneration, new Error('Control channel unavailable'), 'probe_failed');
        return;
      }
      if (this._graceUntil > 0 || (this._suspectAt !== null && now - this._lastProbeAt >= PROBE_TIMEOUT_MS)) {
        this._graceUntil = 0;
        this._sendProbe();
      }
    }, 1_000);
  }

  private _stopTickWatch(): void {
    if (this._tickWatchTimer !== null) {
      clearInterval(this._tickWatchTimer);
      this._tickWatchTimer = null;
    }
  }

  private _scheduleReconnect(immediate: boolean = false): void {
    if (!this._autoReconnect || this._blockedReason || this._reconnectTimer !== null) return;
    const cap = Math.min(RECONNECT_MAX_MS, RECONNECT_INITIAL_MS * 2 ** Math.min(this._reconnectAttempt, 10));
    const delay = Math.max(250, Math.floor(cap * (0.5 + Math.random() * 0.5)));
    this._emitTransport('reconnect_scheduled', this._socketGeneration, {
      reason: immediate ? 'immediate_recovery' : 'transport_backoff',
      reconnectAttempt: this._reconnectAttempt + 1,
      delay,
    });
    if (!this._autoReconnect || this._ws) return;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this._autoReconnect || this._ws) return;
      this._doConnect();
    }, delay);
    this._reconnectAttempt += 1;
    this._emitStatus();
  }

  private _setState(s: ConnectionState): void {
    if (this._state === s) { this._emitStatus(); return; }
    this._state = s;
    this._emit('_state', s);
    this._emitStatus();
    this._flushQueuedRecoveryReads();
  }
}
