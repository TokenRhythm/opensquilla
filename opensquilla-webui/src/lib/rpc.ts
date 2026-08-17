/** OpenSquilla Web UI — WebSocket RPC client (TypeScript port). */

const ANSWER_GENERATION_RESET_CAPABILITY = 'session.answer_generation_reset.v1';

export interface RpcErrorDetail {
  code?: string;
  message?: string;
  details?: unknown;
  retryable?: boolean;
  retry_after_ms?: number;
  accepted?: boolean;
}

export interface RpcClientError extends Error {
  code?: string;
  details?: unknown;
  retryable?: boolean;
  retry_after_ms?: number;
  accepted?: boolean;
}

export type RpcTerminationAction = 'reject' | 'reconnect';

export interface RpcCallOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  timeoutAction?: RpcTerminationAction;
  abortAction?: RpcTerminationAction;
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
  features?: {
    methods?: string[];
    events?: string[];
  };
  auth?: Record<string, unknown>;
  seq?: number;
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected';
export type RpcEventHandler = {
  bivarianceHack(...args: unknown[]): void;
}['bivarianceHack'];

const RECONNECT_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;
const WAKE_DEBOUNCE_MS = 100;
const WAKE_PROBE_TIMEOUT_MS = 3_000;

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
  private _guestSessionKey: string | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAttempt = 0;
  private _autoReconnect = true;
  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _pingInterval = 55000;
  private _policy: Record<string, unknown> | null = null;
  private _lastSeq = 0;
  private _lastFrameAt = 0;
  private _tickWatchTimer: ReturnType<typeof setInterval> | null = null;
  private _tickTimeoutMs = 60000;
  private _wakeDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeProbeTimer: ReturnType<typeof setTimeout> | null = null;
  private _wakeProbeGeneration: number | null = null;
  private _lifecycleWatchStarted = false;

  private readonly _handleWakeSignal = (event: Event): void => {
    if (
      event.type === 'visibilitychange'
      && typeof document !== 'undefined'
      && document.visibilityState === 'hidden'
    ) {
      return;
    }
    this._scheduleWakeProbe();
  };

  connect(url: string, token?: string): void {
    this._url = url;
    this._token = token || null;
    this._guestSessionKey = this._guestSessionKey || loadGuestSessionKey();
    this._autoReconnect = true;
    this._startLifecycleWatch();
    this._clearReconnectTimer();
    if (this._ws) {
      this._retireCurrentSocket(new Error('Connection replaced'), false);
    }
    this._doConnect();
  }

  disconnect(): void {
    this._autoReconnect = false;
    this._stopLifecycleWatch();
    this._clearReconnectTimer();
    this._retireCurrentSocket(new Error('Disconnected'), false);
    this._rejectAllPending(new Error('Disconnected'));
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
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        reject(new Error('Not connected'));
        return;
      }
      if (options.signal?.aborted) {
        reject(new RpcAbortError(method));
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

      const terminate = (error: Error, action: RpcTerminationAction): void => {
        if (!this._rejectPending(id, error, generation)) return;
        if (this._terminationAction(method, action) === 'reconnect') {
          this._recycleConnection(
            generation,
            new Error(`Connection recycled after ${method} terminated`)
          );
        }
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
        const sendError =
          error instanceof Error ? error : new Error('Failed to send RPC request');
        this._rejectPending(id, sendError, generation);
        this._recycleConnection(generation, sendError);
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

  get policy(): Record<string, unknown> {
    return this._policy || {};
  }

  waitForConnection(
    timeoutMs: number = 30000,
    signal?: AbortSignal,
    actions: RpcConnectionWaitOptions = {}
  ): Promise<void> {
    if (signal?.aborted) {
      // No wait and no request ever started, so this caller owns no socket to
      // recycle. Retiring the current connection here could kill a newer
      // session's healthy generation.
      return Promise.reject(new RpcAbortError('waitForConnection'));
    }
    if (this._state === 'connected') return Promise.resolve();

    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      let settled = false;
      let off: () => void = () => {};

      const cleanup = (): void => {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        off();
        signal?.removeEventListener('abort', onAbort);
      };
      const finish = (
        error?: Error,
        action: RpcTerminationAction = 'reject'
      ): void => {
        if (settled) return;
        settled = true;
        cleanup();
        if (!error) {
          resolve();
          return;
        }
        reject(error);
        if (action === 'reconnect') {
          // A disconnected waiter spans the reconnect gap. Its originally
          // observed generation may already have been retired before the
          // replacement handshake started; recycle the current still-
          // unconnected generation so the next attempt gets a fresh socket.
          if (this._state !== 'connected') {
            this._recycleConnection(
              this._socketGeneration,
              new Error('Connection recycled after waitForConnection terminated')
            );
          }
        }
      };
      const onAbort = (): void => {
        finish(
          new RpcAbortError('waitForConnection'),
          actions.abortAction || 'reject'
        );
      };

      off = this.on('_state', (s: ConnectionState) => {
        if (s === 'connected') {
          finish();
        }
      });
      signal?.addEventListener('abort', onAbort, { once: true });
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timer = setTimeout(() => {
          finish(
            new RpcTimeoutError('waitForConnection', timeoutMs),
            actions.timeoutAction || 'reject'
          );
        }, timeoutMs);
      }
    });
  }

  private _doConnect(): void {
    if (this._ws) return;
    this._setState('connecting');
    this._lastSeq = 0;
    this._lastFrameAt = Date.now();
    this._stopTickWatch();
    const generation = ++this._socketGeneration;
    let socket: WebSocket;
    try {
      socket = new WebSocket(this._url);
    } catch {
      if (generation !== this._socketGeneration) return;
      this._setState('disconnected');
      this._scheduleReconnect();
      return;
    }
    this._ws = socket;
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
      if (!this._noteIncomingFrame(data)) return;

      // Handshake: server sends connect.challenge, we reply with connect request
      if (data.type === 'event' && data.event === 'connect.challenge') {
        const authParams = {
          auth: {
            ...(this._token ? { token: this._token } : {}),
            guestSessionKey: this._guestSessionKey || loadGuestSessionKey(),
          },
        };
        const id = String(++this._reqId);
        if (handshakeRequestId) return;
        handshakeRequestId = id;
        this._pending.set(id, {
          resolve: () => {},
          reject: (_err: Error) => {
            this._recycleConnection(generation, new Error('Connect handshake failed'));
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
                caps: [ANSWER_GENERATION_RESET_CAPABILITY],
                client: { name: 'opensquilla-web' },
                ...authParams,
              },
            })
          );
        } catch (error) {
          const sendError =
            error instanceof Error ? error : new Error('Failed to send connect request');
          this._rejectPending(id, sendError, generation);
          this._recycleConnection(generation, sendError);
        }
        return;
      }

      // Handshake: HelloOk frame
      if (data.protocol !== undefined && this._state === 'connecting') {
        // The authenticated Hello is a liveness proof for this exact socket.
        // onmessage is generation/socket fenced above, and _clearWakeProbe
        // refuses to clear a deadline owned by any replacement generation.
        this._clearWakeProbe(generation);
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
        this._reconnectAttempt = 0;
        this._setState('connected');
        this._emit('_hello', data);
        this._startPing();
        this._startTickWatch();
        return;
      }

      if (data.type === 'res') {
        const id = data.id ?? '';
        if (data.ok) {
          this._resolvePending(id, data.payload, generation);
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

    socket.onclose = () => {
      if (!this._isCurrentSocket(socket, generation)) return;
      this._ws = null;
      ++this._socketGeneration;
      this._clearWakeProbe(generation);
      this._stopPing();
      this._stopTickWatch();
      this._rejectPendingForGeneration(generation, new Error('Connection closed'));
      this._setState('disconnected');
      this._scheduleReconnect();
    };

    socket.onerror = () => {};
  }

  private _isCurrentSocket(socket: WebSocket, generation: number): boolean {
    return this._ws === socket && this._socketGeneration === generation;
  }

  private _terminationAction(
    method: string,
    requested: RpcTerminationAction
  ): RpcTerminationAction {
    if (requested !== 'reconnect') return requested;
    // A current Gateway advertises only reads that it dispatches outside the
    // serialized request loop. Their local timeout can therefore reject in
    // isolation; older Gateways retain the reconnect escape hatch.
    const concurrentMethods = this._policy?.concurrent_optional_read_methods;
    if (
      Array.isArray(concurrentMethods)
      && concurrentMethods.some((candidate) => candidate === method)
    ) {
      return 'reject';
    }
    return requested;
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

  private _retireCurrentSocket(error: Error, reconnect: boolean): void {
    const socket = this._ws;
    const generation = this._socketGeneration;
    this._clearWakeProbe(generation);
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
    this._rejectPendingForGeneration(generation, error);
    this._setState('disconnected');
    try {
      socket.close();
    } catch {}
    if (reconnect) this._scheduleReconnect(true);
  }

  private _recycleConnection(generation: number, error: Error): void {
    if (generation !== this._socketGeneration) return;
    this._retireCurrentSocket(error, true);
  }

  private _clearReconnectTimer(): void {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  private _startPing(): void {
    this._stopPing();
    this._pingTimer = setInterval(() => {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send('{"type":"ping"}');
      }
    }, this._pingInterval);
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
    if (!this._autoReconnect) return;
    this._reconnectAttempt = 0;

    let socket = this._ws;
    if (!socket) {
      this._clearReconnectTimer();
      this._doConnect();
      socket = this._ws;
      if (!socket) return;
    }

    const generation = this._socketGeneration;
    if (this._wakeProbeGeneration === generation) return;
    if (socket.readyState === WebSocket.OPEN) {
      try {
        socket.send('{"type":"ping"}');
      } catch (error) {
        this._recycleConnection(
          generation,
          error instanceof Error ? error : new Error('Wake probe send failed')
        );
        return;
      }
    } else if (socket.readyState !== WebSocket.CONNECTING) {
      this._recycleConnection(generation, new Error('Connection stale after wake'));
      return;
    }
    this._armWakeProbe(socket, generation);
  }

  private _armWakeProbe(socket: WebSocket, generation: number): void {
    this._clearWakeProbe();
    this._wakeProbeGeneration = generation;
    this._wakeProbeTimer = setTimeout(() => {
      if (!this._isCurrentSocket(socket, generation)) return;
      this._clearWakeProbe(generation);
      this._emit('_gap', { reason: 'wake_probe_timeout' });
      this._retireCurrentSocket(new Error('Wake probe timed out'), true);
    }, WAKE_PROBE_TIMEOUT_MS);
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
  }

  private _noteIncomingFrame(data: RpcFrame): boolean {
    this._lastFrameAt = Date.now();
    if (data?.type === 'pong') this._clearWakeProbe(this._socketGeneration);
    if (!data || data.type !== 'event' || typeof data.seq !== 'number') return true;

    const seq = data.seq;
    if (this._lastSeq > 0 && seq !== this._lastSeq + 1) {
      const detail = { expected: this._lastSeq + 1, actual: seq, event: data.event };
      this._emit('_gap', detail);
      try {
        this._ws?.close();
      } catch {}
      return false;
    }
    this._lastSeq = seq;
    return true;
  }

  private _startTickWatch(): void {
    this._stopTickWatch();
    const tickMs = (this._policy?.tick_interval_ms as number) || 30000;
    this._tickTimeoutMs = Math.max(10000, tickMs * 2.5);
    this._lastFrameAt = Date.now();
    this._tickWatchTimer = setInterval(() => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      const idleMs = Date.now() - this._lastFrameAt;
      if (idleMs <= this._tickTimeoutMs) return;
      this._emit('_gap', { reason: 'tick_timeout', idleMs });
      try {
        this._ws.close();
      } catch {}
    }, Math.min(tickMs, 10000));
  }

  private _stopTickWatch(): void {
    if (this._tickWatchTimer !== null) {
      clearInterval(this._tickWatchTimer);
      this._tickWatchTimer = null;
    }
  }

  private _scheduleReconnect(immediate: boolean = false): void {
    if (!this._autoReconnect) return;
    this._clearReconnectTimer();
    const delay = immediate
      ? 0
      : RECONNECT_BACKOFF_MS[
          Math.min(this._reconnectAttempt, RECONNECT_BACKOFF_MS.length - 1)
        ];
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this._autoReconnect || this._ws) return;
      this._doConnect();
    }, delay);
    if (immediate) return;
    this._reconnectAttempt += 1;
  }

  private _setState(s: ConnectionState): void {
    if (this._state === s) return;
    this._state = s;
    this._emit('_state', s);
  }
}
