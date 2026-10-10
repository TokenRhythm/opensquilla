import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { BrowserWindow, session, type Session, type WebContents } from 'electron'

export const BROWSER_PROMPT_PRELOAD = fileURLToPath(
  new URL('./browser-prompt-preload.cjs', import.meta.url),
)

const CHANNEL = 'opensquilla-browser-prompt'
const MAX_MESSAGE = 2048
const MAX_VALUE = 4096
const MAX_PROMPTS_PER_MINUTE = 8

const HTML = `<!doctype html><meta charset="utf-8"><title>Page prompt</title>
<style>
  html { color-scheme: light dark; font: 14px system-ui; }
  body { margin: 20px; }
  #origin { font-size: 12px; opacity: .7; margin-bottom: 12px; }
  #message { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 100px; overflow: auto; }
  input { box-sizing: border-box; width: 100%; padding: 8px; margin: 12px 0; font: inherit; }
  footer { display: flex; justify-content: flex-end; gap: 8px; }
  button { font: inherit; min-width: 80px; padding: 6px 12px; }
</style>
<div id="origin"></div><p id="message"></p>
<form id="answer"><input id="value" type="text" autocomplete="off" maxlength="4096">
<footer><button id="cancel" type="button">Cancel</button><button type="submit">OK</button></footer>
</form>`

export interface BrowserPromptState {
  id: string
  origin: string
  message: string
  defaultValue: string
  openedAt: number
}

interface PendingPrompt {
  state: BrowserPromptState
  event: Electron.IpcMainEvent
  dialog: BrowserWindow | null
}

export interface BrowserPromptControllerOptions {
  owner: BrowserWindow
  contents: WebContents
  isAllowed(): boolean
  onStateChange?(state: BrowserPromptState | null): void
}

function promptRequest(value: unknown): { message: string; defaultValue: string } | null {
  if (!value || typeof value !== 'object') return null
  const request = value as Record<string, unknown>
  return typeof request.message === 'string' && request.message.length <= MAX_MESSAGE
    && typeof request.defaultValue === 'string' && request.defaultValue.length <= MAX_VALUE
    ? { message: request.message, defaultValue: request.defaultValue }
    : null
}

function httpOrigin(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.origin : null
  } catch {
    return null
  }
}

/** One narrow prompt capability for an untrusted URL preview WebContents. */
export class BrowserPromptController {
  private pending: PendingPrompt | null = null
  private readonly starts: number[] = []
  private promptSession: Session | null = null
  private disposed = false

  private readonly onSync = (event: Electron.IpcMainEvent, channel: string, value: unknown): void => {
    if (channel !== CHANNEL) return
    const request = promptRequest(value)
    const frame = event.senderFrame
    const origin = frame && httpOrigin(frame.url)
    let ownerAllows = false
    try { ownerAllows = this.options.isAllowed() } catch { /* Deny if state changed. */ }
    const allowed = !this.disposed && !this.options.contents.isDestroyed()
      && event.sender === this.options.contents && frame === this.options.contents.mainFrame
      && !this.options.owner.isDestroyed() && ownerAllows
      && request && origin && !this.pending
    const now = Date.now()
    while (this.starts.length && this.starts[0]! <= now - 60_000) this.starts.shift()
    if (!allowed || this.starts.length >= MAX_PROMPTS_PER_MINUTE) {
      event.returnValue = null
      return
    }
    this.starts.push(now)
    const state: BrowserPromptState = {
      id: randomUUID(), origin, message: request.message,
      defaultValue: request.defaultValue, openedAt: now,
    }
    const pending = { state, event, dialog: null }
    this.pending = pending
    try { this.options.onStateChange?.({ ...state }) } catch { /* Keep IPC reply live. */ }
    void this.show(pending).catch(() => { this.respond(state.id, null) })
  }

  constructor(private readonly options: BrowserPromptControllerOptions) {
    options.contents.on('ipc-message-sync', this.onSync)
  }

  state(): BrowserPromptState | null {
    return this.pending ? { ...this.pending.state } : null
  }

  respond(id: string, value: string | null): boolean {
    const pending = this.pending
    if (!pending || pending.state.id !== id || (value !== null
      && (typeof value !== 'string' || value.length > MAX_VALUE))) return false
    this.pending = null
    try { this.options.onStateChange?.(null) } catch { /* Keep IPC reply live. */ }
    try { pending.event.returnValue = value } catch { /* Renderer may already be gone. */ }
    if (pending.dialog && !pending.dialog.isDestroyed()) pending.dialog.destroy()
    return true
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.options.contents.removeListener('ipc-message-sync', this.onSync)
    if (this.pending) this.respond(this.pending.state.id, null)
    if (this.promptSession) {
      void Promise.allSettled([
        this.promptSession.clearStorageData(),
        this.promptSession.clearCache(),
        this.promptSession.clearAuthCache(),
      ])
    }
  }

  private async show(pending: PendingPrompt): Promise<void> {
    if (!this.promptSession) {
      this.promptSession = session.fromPartition(`opensquilla-browser-prompt:${randomUUID()}`,
        { cache: false })
      this.promptSession.setPermissionCheckHandler(() => false)
      this.promptSession.setPermissionRequestHandler((_contents, _permission, done) => done(false))
      this.promptSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, done) => {
        done({ cancel: !details.url.startsWith('data:text/html') })
      })
    }
    const prompt = new BrowserWindow({
      parent: this.options.owner, modal: true, show: false,
      width: 460, height: 255, resizable: false, maximizable: false,
      minimizable: false, autoHideMenuBar: true, title: 'Page prompt',
      webPreferences: { session: this.promptSession, contextIsolation: true,
        nodeIntegration: false, sandbox: true, webSecurity: true,
        webviewTag: false, devTools: false, spellcheck: false },
    })
    pending.dialog = prompt
    prompt.setMenu(null)
    prompt.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    prompt.webContents.on('will-navigate', event => event.preventDefault())
    prompt.once('closed', () => { this.respond(pending.state.id, null) })
    await prompt.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(HTML)}`)
    if (this.pending !== pending) return
    prompt.show()
    const result = await prompt.webContents.executeJavaScript(`(() => {
      document.getElementById('origin').textContent = ${JSON.stringify(pending.state.origin)}
      document.getElementById('message').textContent = ${JSON.stringify(pending.state.message)}
      const input = document.getElementById('value')
      input.value = ${JSON.stringify(pending.state.defaultValue)}
      input.focus()
      input.select()
      return new Promise(resolve => {
        document.getElementById('answer').addEventListener('submit', event => {
          event.preventDefault()
          resolve(String(input.value))
        }, { once: true })
        document.getElementById('cancel').addEventListener('click', () => resolve(null), { once: true })
        document.addEventListener('keydown', event => {
          if (event.key === 'Escape') resolve(null)
        })
      })
    })()`) as unknown
    this.respond(pending.state.id, typeof result === 'string' ? result : null)
  }
}
