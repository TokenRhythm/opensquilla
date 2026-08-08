// GatewayRpc — a minimal WebSocket JSON-RPC client for the OpenSquilla gateway.
//
// Wire protocol (src/opensquilla/gateway/protocol.py + websocket.py):
//   1. Server sends an event `connect.challenge` {nonce}.
//   2. Client replies `{type:"req", id, method:"connect", params:{minProtocol:3,
//      maxProtocol:3, client:{...}, role:"operator"}}`.
//   3. Server either sends a raw `hello-ok` frame (success) or a `res` with an
//      UNAUTHORIZED error (need auth token).
//   4. Then frames are `event` / `res` / `ping` / `pong`.
//
// Uses the Node built-in `WebSocket` (Node 22+, bundled by Electron 42) — no
// runtime dependency.

export type RpcEventHandler = (payload: any, meta: any) => void

export interface RpcClientInfo {
  id: string
  version: string
  platform: string
  mode: string
  display_name?: string
}

export interface GatewayRpcOptions {
  url: string
  token?: string
  role?: string
  client: RpcClientInfo
  /** Invoked on connection state changes ('connecting'|'ready'|'offline'). */
  onStatus?: (status: 'connecting' | 'ready' | 'offline', info?: any) => void
  /** Invoked after a reconnect has completed the handshake (caller re-subscribes). */
  onReconnect?: () => void
  /** Called with the raw parsed frame; a debug/observability hook. */
  onFrame?: (frame: any) => void
}

interface PendingCall {
  resolve: (value: any) => void
  reject: (err: Error) => void
  timer: ReturnType<typeof setTimeout>
}

const CONNECT_TIMEOUT_MS = 10_000
const CALL_TIMEOUT_MS = 15_000
const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 15_000

export class GatewayRpc {
  private ws: WebSocket | null = null
  private readonly url: string
  private readonly token: string | undefined
  private readonly role: string
  private readonly client: RpcClientInfo
  private readonly onStatus?: (status: 'connecting' | 'ready' | 'offline', info?: any) => void
  private readonly onReconnect?: () => void
  private readonly onFrame?: (frame: any) => void

  private pending = new Map<string, PendingCall>()
  private handlers = new Map<string, Set<RpcEventHandler>>()
  private idCounter = 0
  private connected = false
  private stopped = false
  private handshakeResolver: (() => void) | null = null
  private handshakeRejector: ((err: Error) => void) | null = null
  private handshakeDone = false
  private backoff = RECONNECT_BASE_MS
  private readyFired = false

  constructor(opts: GatewayRpcOptions) {
    this.url = opts.url
    this.token = opts.token
    this.role = opts.role ?? 'operator'
    this.client = opts.client
    this.onStatus = opts.onStatus
    this.onReconnect = opts.onReconnect
    this.onFrame = opts.onFrame
  }

  get isConnected(): boolean {
    return this.connected
  }

  /** Connect and complete the handshake. Resolves once hello-ok is received. */
  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.stopped) return reject(new Error('rpc stopped'))
      this.handshakeResolver = resolve
      this.handshakeRejector = reject
      this.handshakeDone = false
      this.open()
    })
  }

  private open(): void {
    this.emitStatus('connecting')
    let ws: WebSocket
    try {
      ws = new WebSocket(this.url)
    } catch (err) {
      this.scheduleReconnect(err instanceof Error ? err : new Error(String(err)))
      return
    }
    this.ws = ws
    const openTimer = setTimeout(() => {
      if (this.ws === ws) {
        try { ws.close() } catch {}
      }
    }, CONNECT_TIMEOUT_MS)

    ws.addEventListener('open', () => {
      clearTimeout(openTimer)
      // Wait for the challenge event; the server drives the handshake.
    })

    ws.addEventListener('message', (ev: any) => {
      this.handleMessage(ev.data)
    })

    ws.addEventListener('close', () => {
      if (this.ws === ws) this.ws = null
      this.connected = false
      if (!this.handshakeDone && this.handshakeRejector) {
        const reject = this.handshakeRejector
        this.handshakeRejector = null
        this.handshakeResolver = null
        reject(new Error('connection closed during handshake'))
      }
      this.emitStatus('offline')
      this.scheduleReconnect(new Error('connection closed'))
    })

    ws.addEventListener('error', () => {
      // close follows; nothing to do here
    })
  }

  private handleMessage(raw: any): void {
    let frame: any
    try {
      frame = typeof raw === 'string' ? JSON.parse(raw) : raw
    } catch {
      return
    }
    if (this.onFrame) this.onFrame(frame)
    const type = frame && frame.type

    // connect.challenge arrives as an event frame {type:"event", event:"connect.challenge"}.
    if (type === 'event' && frame.event === 'connect.challenge') {
      this.sendConnect()
      return
    }

    if (type === 'hello-ok') {
      this.connected = true
      this.handshakeDone = true
      this.backoff = RECONNECT_BASE_MS
      const fire = this.handshakeResolver
      this.handshakeResolver = null
      this.handshakeRejector = null
      if (!this.readyFired) {
        this.readyFired = true
        this.emitStatus('ready', frame)
      }
      if (fire) fire()
      else if (this.onReconnect) this.onReconnect()
      return
    }

    if (type === 'res') {
      const id = frame.id
      const call = this.pending.get(id)
      if (!call) return
      this.pending.delete(id)
      clearTimeout(call.timer)
      if (frame.ok) call.resolve(frame.payload ?? null)
      else call.reject(new Error((frame.error && frame.error.code) || 'rpc_error'))
      return
    }

    if (type === 'event') {
      const name = frame.event
      const payload = frame.payload ?? {}
      const meta = frame.meta ?? {}
      const set = this.handlers.get(name)
      if (set) for (const h of set) { try { h(payload, meta) } catch {} }
      return
    }

    if (type === 'ping') {
      this.sendRaw(JSON.stringify({ type: 'pong' }))
    }
  }

  private sendConnect(): void {
    this.sendRaw(JSON.stringify({
      type: 'req',
      id: 'connect-0',
      method: 'connect',
      params: {
        minProtocol: 3,
        maxProtocol: 3,
        client: this.client,
        role: this.role,
        ...(this.token ? { auth: { token: this.token } } : {}),
      },
    }))
  }

  private sendRaw(text: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(text) } catch {}
    }
  }

  private emitStatus(status: 'connecting' | 'ready' | 'offline', info?: any): void {
    if (this.onStatus) {
      try { this.onStatus(status, info) } catch {}
    }
  }

  private scheduleReconnect(err: Error): void {
    if (this.stopped) return
    const delay = Math.min(this.backoff, RECONNECT_MAX_MS)
    this.backoff = Math.min(this.backoff * 2, RECONNECT_MAX_MS)
    setTimeout(() => {
      if (this.stopped) return
      this.connect().catch(() => {
        // loop handled by close -> scheduleReconnect
      })
    }, delay)
    // Keep a pending handshake honest if we are mid-connect.
    if (!this.handshakeDone && this.handshakeRejector) {
      const reject = this.handshakeRejector
      this.handshakeRejector = null
      this.handshakeResolver = null
      reject(err)
    }
  }

  /** Make an RPC call. Resolves with the `payload`, rejects with an Error. */
  call<T = any>(method: string, params?: any, timeoutMs = CALL_TIMEOUT_MS): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.connected) {
        reject(new Error('not-connected'))
        return
      }
      const id = `r${++this.idCounter}`
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error('rpc-timeout'))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.sendRaw(JSON.stringify({ type: 'req', id, method, params: params ?? {} }))
    })
  }

  /** Subscribe to a server-pushed event. Returns an unsubscribe function. */
  on(name: string, handler: RpcEventHandler): () => void {
    let set = this.handlers.get(name)
    if (!set) {
      set = new Set()
      this.handlers.set(name, set)
    }
    set.add(handler)
    return () => {
      if (set) set.delete(handler)
    }
  }

  close(): void {
    this.stopped = true
    for (const call of this.pending.values()) {
      clearTimeout(call.timer)
      call.reject(new Error('rpc closed'))
    }
    this.pending.clear()
    this.handlers.clear()
    if (this.ws) {
      try { this.ws.close() } catch {}
      this.ws = null
    }
  }
}
