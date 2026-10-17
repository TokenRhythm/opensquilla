import { helloOkFrame } from '../support/gateway-fixture'
import { CONTINUOUS_SESSION, continuousPayloads } from '../support/continuous-reading-fixture'

// Loads the entire shipped App. Only the transport is replaced: no substitute
// layout CSS, replica components, real Gateway, model, or user history.
const selected = new URL(location.href).searchParams.get('theme') || 'miami'
const theme = ['miami', 'light', 'dark'].includes(selected) ? selected : 'miami'
localStorage.setItem('opensquilla-theme', theme)
localStorage.setItem('opensquilla-locale', 'zh-Hans')
localStorage.removeItem('opensquilla.appearance.toolDetails.v1')
document.documentElement.dataset.continuousReadingFixture = 'true'

class FixtureSocket extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = FixtureSocket.CONNECTING
  readonly bufferedAmount = 0
  readonly extensions = ''
  readonly protocol = ''
  binaryType: BinaryType = 'blob'
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
    queueMicrotask(() => {
      this.readyState = FixtureSocket.OPEN
      const event = new Event('open')
      this.onopen?.(event)
      this.dispatchEvent(event)
      this.deliver({ type: 'event', event: 'connect.challenge', payload: {} })
    })
  }
  private deliver(frame: unknown) {
    if (this.readyState !== FixtureSocket.OPEN) return
    const event = new MessageEvent('message', { data: JSON.stringify(frame) })
    this.onmessage?.(event)
    this.dispatchEvent(event)
  }
  send(data: string) {
    const frame = JSON.parse(data) as { type?: string; id?: string; method?: string; nonce?: string }
    // The real client probes idle connections. Echo its nonce just as the
    // Gateway does, so an idle visual fixture does not show a false warning.
    if (frame.type === 'ping') {
      queueMicrotask(() => this.deliver({ type: 'pong', ...(frame.nonce === undefined ? {} : { nonce: frame.nonce }) }))
      return
    }
    if (frame.type !== 'req') return
    queueMicrotask(() => this.deliver(frame.method === 'connect'
      ? helloOkFrame()
      : { type: 'res', id: frame.id, ok: true, payload: continuousPayloads[frame.method || ''] ?? {} }))
  }
  close(code = 1000, reason = '') {
    this.readyState = FixtureSocket.CLOSED
    const event = new CloseEvent('close', { code, reason, wasClean: true })
    this.onclose?.(event)
    this.dispatchEvent(event)
  }
}

Object.defineProperty(window, 'WebSocket', { configurable: true, value: FixtureSocket })
const nativeFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href)
  if (url.pathname.startsWith('/api/')) {
    return Promise.resolve(new Response('{}', { headers: { 'Content-Type': 'application/json' } }))
  }
  return nativeFetch(input, init)
}

history.replaceState(null, '', `/control/chat?session=${encodeURIComponent(CONTINUOUS_SESSION)}&fixture=continuous-reading&theme=${theme}`)
await import('../../src/main')
