/** OpenSquilla Web UI — WebSocket RPC client (TypeScript port). */

const ANSWER_GENERATION_RESET_CAPABILITY = 'session.answer_generation_reset.v1';
const TURN_COMMITTED_CAPABILITY = 'session.turn_committed.v1';
const PROBE_CAPABILITY = 'transport.probe.v1';

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

/** @deprecated Termination is always request-local, including legacy reconnect. */
export type RpcTerminationAction = 'reject' | 'reconnect';

export interface RpcConnectionIntent {
  authentication?: 'guest-allowed' | 'authenticated' | 'owner';
  /** Non-secret target identity (for example Desktop profile and instance). */
  key?: string;
}

export type RpcLifecycle = 'stopped' | 'connecting' | 'connected' | 'recovering' | 'blocked';
export type RpcConsumptionResult = 'applied' | 'dirty';
export type RpcConsumptionHandler = (
  payload: unknown, meta: Record<string, unknown>,
) => RpcConsumptionResult | Promise<RpcConsumptionResult>;

export interface RpcCallOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  timeoutAction?: RpcTerminationAction;
  abortAction?: RpcTerminationAction;
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
  auth?: Record<string, unknown>;
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
const SUSPECT_WINDOW_MS = 30_000;
const WAKE_GRACE_MS = 5_000;
const SCHEDULER_LAG_MS = 5_000;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  method: string;
  generation: number;
  timeoutTimer: ReturnType<typeof setTimeout> | null;
  signal: AbortSignal | null;
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
    this.notifyResume();
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

  notifyResume(): void {
    if (!this._autoReconnect || this._blockedReason) return;
    this._clearWakeProbe();
    this._suspectAt = null;
    this._graceUntil = Date.now() + WAKE_GRACE_MS;
    this._setHealth('healthy');
    this._scheduleWakeProbe();
  }

  disconnect(): void {
    this._autoReconnect = false;
    this._blockedReason = null;
    this._recoveryStartedAt = null;
    this._stopLifecycleWatch();
    this._clearReconnectTimer();
    this._retireCurrentSocket(
      new RpcTransportError('Disconnected', null),
      false,
      'client_disconnect'
    );
    this._rejectAllPending(new RpcTransportError('Disconnected', null));
    this._setState('disconnected');
  }

  call(
    method: string,
    params: Record<string, unknown> = {},
    options: RpcCallOptions = {}
  ): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const socket = this._ws;
      const generation = this._socketGeneration;
      if (!socket || socket.readyState !== WebSocket.OPEN || this._state !== 'connected') {
        reject(new RpcTransportError('Not connected', false));
        return;
      }
      if (options.signal?.aborted) {
        reject(new RpcAbortError(method));
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
      const pending: PendingRequest = {
        resolve,
        reject,
        method,
        generation,
        timeoutTimer: null,
        signal: options.signal || null,
        abortHandler: null,
      };
      this._pending.set(id, pending);

      const terminate = (error: Error, _action: RpcTerminationAction): void => {
        this._rejectPending(id, error, generation);
      };

      if (options.signal) {
        pending.abortHandler = () => {
          terminate(new RpcAbortError(method), options.abortAction || 'reject');
        };
        options.signal.addEventListener('abort', pending.abortHandler, { once: true });
      }

      if (
        options.timeoutMs !== undefined &&
        options.timeoutMs > 0 &&
        Number.isFinite(options.timeoutMs)
      ) {
        pending.timeoutTimer = setTimeout(() => {
          terminate(
            new RpcTimeoutError(method, options.timeoutMs!),
            options.timeoutAction || 'reject'
          );
        }, options.timeoutMs);
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

    socket.onopen = () => {
      if (!this._isCurrentSocket(socket, generation)) return;
      // Don't send connect yet — wait for connect.challenge from server
    };

    socket.onmessage = (ev: MessageEvent) => {
      if (!this._isCurrentSocket(socket, generation)) return;
      let data: RpcFrame;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      if (!this._noteIncomingFrame(data)) return;

      // Handshake: server sends connect.challenge, we reply with connect request
      if (data.type === 'event' && data.event === 'connect.challenge') {
        if (handshakeRequestId) return;
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
        });
        try {
          socket.send(
            JSON.stringify({
              type: 'req',
              id,
              method: 'connect',
              params: {
                minProtocol: 3,
                maxProtocol: 3,
                caps: [
                  ANSWER_GENERATION_RESET_CAPABILITY,
                  TURN_COMMITTED_CAPABILITY,
                  PROBE_CAPABILITY,
                  ...(this._consumptionFlowEnabled ? ['transport.flow.v1'] : []),
                ],
                client: { name: 'opensquilla-web' },
                ...authParams,
              },
            })
          );
          this._armHelloWatchdog(socket, generation);
        } catch (error) {
          const sendError =
            error instanceof Error ? error : new Error('Failed to send connect request');
          this._rejectPending(id, sendError, generation);
          this._recycleConnection(generation, sendError, 'connect_send_failure');
        }
        return;
      }

      // Handshake: HelloOk frame
      if (data.protocol !== undefined && this._state === 'connecting') {
        if (!handshakeRequestId) return;
        if (data.protocol !== 3) {
          this._blockConnection('protocol_rejected');
          return;
        }
        const principal = data.auth?.principal as Record<string, unknown> | undefined;
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
        this._clearHandshakeWatchdogs(generation);
        const recoveryMs = this._recoveryStartedAt === null
          ? undefined : Math.max(0, Date.now() - this._recoveryStartedAt);
        this._recoveryStartedAt = null;
        this._emitTransport('hello', generation, {
          reason: 'authenticated',
          ...(recoveryMs === undefined ? {} : { recoveryMs }),
          ...(typeof data.server?.conn_id === 'string'
            ? { connId: data.server.conn_id }
            : {}),
        });
        if (!this._isCurrentSocket(socket, generation)) return;
        this._policy = data.policy || null;
        const serverGuestSessionKey = data.auth?.guestSessionKey;
        if (
          typeof serverGuestSessionKey === 'string'
          && GUEST_SESSION_KEY_PATTERN.test(serverGuestSessionKey)
        ) {
          this._guestSessionKey = serverGuestSessionKey;
          persistGuestSessionKey(serverGuestSessionKey);
        }
        if (handshakeRequestId) {
          this._resolvePending(handshakeRequestId, data, generation);
          handshakeRequestId = null;
        }
        // Only a completed protocol handshake proves recovery. Merely opening
        // a socket must not reset backoff when a Gateway is repeatedly dying
        // before connect completes.
        this._everConnected = true;
        this._stableTimer = setTimeout(() => {
          if (this._isCurrentSocket(socket, generation) && this._state === 'connected') {
            this._reconnectAttempt = 0;
          }
        }, CONNECTION_STABLE_MS);
        this._emit('_hello', data);
        if (!this._isCurrentSocket(socket, generation)) return;
        this._setState('connected');
        this._startPing();
        this._startTickWatch();
        return;
      }

      if (data.type === 'res') {
        const id = data.id ?? '';
        if (this._pending.get(id)?.generation === generation) this._noteRoundTrip();
        if (data.ok) {
          if (!this._resolvePending(id, data.payload, generation)) {
            // A request-local timeout/abort can leave a late response carrying
            // connection-owned resources. Adapters alone recognize its schema
            // and may explicitly discard those resources. No business replay,
            // semantic installation, or unbounded request tombstone lives here.
            this._emit('_orphan_response', { id, payload: data.payload, generation });
          }
        } else {
          const err = data.error;
          const message =
            typeof err === 'string'
              ? err
              : (err && (err.message || err.code)) || 'RPC error';
          const error = new Error(message) as RpcClientError;
          if (err && typeof err === 'object') {
            error.code = err.code;
            error.details = err.details;
            error.retryable = err.retryable;
            error.retry_after_ms = err.retry_after_ms;
            error.accepted = err.accepted;
          }
          this._rejectPending(id, error, generation);
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
      phase,
      generation,
      reconnectAttempt: this._reconnectAttempt,
      lastRxAt: this._lastFrameAt,
      loopLagMs: this._lastLoopLagMs,
      maxLoopLagMs: this._maxLoopLagMs,
      ...detail,
    });
  }

  private _blockConnection(reason: string): void {
    this._blockedReason = reason;
    this._recoveryStartedAt = null;
    this._clearReconnectTimer();
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
      lifecycle: this.lifecycle, reason: this.recoveryReason, health: this._health,
    });
  }

  private _setHealth(health: 'healthy' | 'suspect'): void {
    if (this._health === health) return;
    this._health = health;
    this._emitStatus();
  }

  private _noteRoundTrip(): void {
    this._clearWakeProbe();
    this._suspectAt = null;
    this._suspectProbes = 0;
    this._setHealth('healthy');
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
    if (!socket || socket.readyState !== WebSocket.OPEN || this._state !== 'connected'
      || this._wakeProbeGeneration !== null || Date.now() < this._graceUntil) return;
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
    this._armWakeProbe(socket, generation, nonce);
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
    if (!this._ws) this.ensureConnected();
    else if (this._ws.readyState !== WebSocket.OPEN && this._ws.readyState !== WebSocket.CONNECTING) {
      this._recycleConnection(this._socketGeneration, new Error('Socket closed after wake'), 'wake_socket_stale');
    } else this._sendProbe();
  }

  private _armWakeProbe(socket: WebSocket, generation: number, nonce: string | null): void {
    this._clearWakeProbe();
    this._probeNonce = nonce;
    this._wakeProbeGeneration = generation;
    const dueAt = Date.now() + PROBE_TIMEOUT_MS;
    this._wakeProbeTimer = setTimeout(() => {
      if (!this._isCurrentSocket(socket, generation)) return;
      this._clearWakeProbe(generation);
      if (Date.now() - dueAt > SCHEDULER_LAG_MS || Date.now() < this._graceUntil) {
        this.notifyResume();
        return;
      }
      if (this._suspectAt === null) {
        this._suspectAt = Date.now();
        this._suspectProbes = 0;
        this._setHealth('suspect');
        this._emitTransport('probe_timeout', generation, { reason: 'control_unconfirmed' });
      }
    }, PROBE_TIMEOUT_MS);
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
  }
}
