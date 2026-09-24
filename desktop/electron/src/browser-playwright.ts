import { randomUUID } from 'node:crypto'
import { BrowserWindow, WebContentsView, nativeImage, type WebContents } from 'electron'
import { chromium, type Browser, type ConnectOverCDPTransport, type ElementHandle, type Page } from 'playwright-core'
import { DesktopBrowserError, type DesktopBrowserObservationReason, type DesktopBrowserRequest } from './desktop-browser.js'
import { BrowserPointerController } from './browser-pointer-controller.js'
import { planMouseTrajectory } from './browser-mouse-trajectory.js'

type Message = { id?: number; method?: string; params?: Record<string, unknown>; sessionId?: string }
type Guard = () => void
type MouseInput = Record<string, unknown>
type MouseMotion = (params: MouseInput, send: (point: MouseInput) => Promise<unknown>) => Promise<unknown>

/** A single existing renderer exposed through Playwright's public CDP transport interface. */
class RendererTransport implements ConnectOverCDPTransport {
  onmessage?: (message: object) => void
  onclose?: (reason?: string) => void
  guard: Guard = () => {}
  onMouseInput?: (params: Record<string, unknown>) => Promise<void>
  mouseMotion?: MouseMotion
  mouseGuard?: Guard
  mouseCommitGuard?: () => Promise<void>
  onDialogOpening?: (params: Record<string, unknown>, sessionId: string) => void
  onDialogClosed?: (sessionId: string) => void
  private closed = false
  private announced = false
  private readonly sessionId = `page-${randomUUID()}`
  private nativeSessionId: string | undefined
  private readonly childSessions = new Set<string>()
  private readonly ownedAttachment: boolean
  private info: Record<string, unknown> = {}

  constructor(private readonly contents: WebContents) {
    this.ownedAttachment = !contents.debugger.isAttached()
    if (this.ownedAttachment) contents.debugger.attach('1.3')
    contents.debugger.on('message', this.onDebuggerMessage)
    contents.debugger.on('detach', this.onDebuggerDetach)
    contents.once('destroyed', this.onDestroyed)
  }

  async initialize(): Promise<void> {
    if (this.closed) throw new Error('Renderer connection ended before initialization.')
    const { targetInfo } = await this.contents.debugger.sendCommand('Target.getTargetInfo')
    if (this.closed) throw new Error('Renderer connection ended during initialization.')
    if (!targetInfo?.targetId || targetInfo.type !== 'page') throw new Error('Expected an existing page target.')
    this.info = { ...targetInfo, browserContextId: targetInfo.browserContextId || `renderer-${this.contents.id}` }
    const attached = await this.contents.debugger.sendCommand('Target.attachToTarget', {
      targetId: targetInfo.targetId, flatten: true,
    })
    if (typeof attached.sessionId !== 'string') throw new Error('Could not attach an isolated renderer session.')
    if (this.closed) {
      await this.contents.debugger.sendCommand('Target.detachFromTarget', { sessionId: attached.sessionId }).catch(() => {})
      throw new Error('Renderer connection ended during initialization.')
    }
    this.nativeSessionId = attached.sessionId
  }

  private emit(message: object): void {
    queueMicrotask(() => { if (!this.closed) this.onmessage?.(message) })
  }

  private onDebuggerMessage = (_event: Electron.Event, method: string, params: Record<string, unknown>, sessionId?: string): void => {
    if (this.closed || !sessionId || (sessionId !== this.nativeSessionId && !this.childSessions.has(sessionId))) return
    // The host owns dialog responses, including dialogs raised while Playwright
    // is still attaching. Forwarding these would permit its default dismissal
    // before a client-side dialog listener has been installed.
    if (method === 'Page.javascriptDialogOpening') { this.onDialogOpening?.(params, sessionId); return }
    if (method === 'Page.javascriptDialogClosed') { this.onDialogClosed?.(sessionId); return }
    if (method === 'Target.attachedToTarget' && typeof params.sessionId === 'string') {
      const info = params.targetInfo as { type?: string } | undefined
      if (!['iframe', 'worker'].includes(info?.type ?? '')) {
        // A related popup must acquire its own task binding before it is exposed.
        const childSessionId = params.sessionId
        void this.contents.debugger.sendCommand('Runtime.runIfWaitingForDebugger', {}, childSessionId)
          .catch(() => {})
          .then(() => this.contents.debugger.sendCommand('Target.detachFromTarget', { sessionId: childSessionId }, sessionId))
          .catch(() => {})
        return
      }
      this.childSessions.add(params.sessionId)
    }
    this.emit({ method, params, sessionId: sessionId === this.nativeSessionId ? this.sessionId : sessionId })
    if (method === 'Target.detachedFromTarget' && typeof params.sessionId === 'string') {
      this.childSessions.delete(params.sessionId)
    }
  }

  private onDebuggerDetach = (): void => { this.close() }
  private onDestroyed = (): void => { this.close() }

  send(message: object): void {
    const command = message as Message
    if (this.closed) return
    void this.dispatch(command).then(result => {
      this.emit({ id: command.id, result, sessionId: command.sessionId })
    }, error => {
      this.emit({ id: command.id, error: { code: -32000, message: error instanceof Error ? error.message : String(error) }, sessionId: command.sessionId })
    })
  }

  private async dispatch(command: Message): Promise<unknown> {
    this.guard()
    const method = command.method ?? ''
    const params = command.params ?? {}
    if (!command.sessionId) {
      if (method === 'Browser.getVersion') return await this.contents.debugger.sendCommand(method)
      if (method === 'Target.getTargets') return { targetInfos: [this.info] }
      if (method === 'Target.getTargetInfo') {
        if (params.targetId && params.targetId !== this.info.targetId) throw new Error('Target is outside this browser binding.')
        return { targetInfo: this.info }
      }
      if (method === 'Target.setAutoAttach') {
        if (params.autoAttach && !this.announced) {
          this.announced = true
          this.emit({ method: 'Target.attachedToTarget', params: {
            sessionId: this.sessionId, targetInfo: this.info, waitingForDebugger: false,
          } })
        }
        return {}
      }
      throw new Error(`Browser-wide command is unavailable: ${method}`)
    }
    if (command.sessionId !== this.sessionId && !this.childSessions.has(command.sessionId)) {
      throw new Error('Target session is outside this browser binding.')
    }
    if (method.startsWith('Browser.') || method.startsWith('Storage.') || method === 'Page.close'
      || (method.startsWith('Target.') && !['Target.setAutoAttach', 'Target.detachFromTarget'].includes(method))) {
      throw new Error(`Command is unavailable for an attached page: ${method}`)
    }
    if (method === 'Target.detachFromTarget' && !this.childSessions.has(String(params.sessionId))) {
      throw new Error('Target session is outside this browser binding.')
    }
    if (method === 'Target.setAutoAttach') {
      // These are live embedded pages, not newly launched automation targets.
      // Pausing a renderer during cross-process frame creation can prevent
      // Electron from committing that frame even after a resume acknowledgment.
      return await this.contents.debugger.sendCommand(method, { ...params, waitForDebuggerOnStart: false },
        command.sessionId === this.sessionId ? this.nativeSessionId : command.sessionId)
    }
    if (method === 'Input.dispatchMouseEvent' && command.sessionId === this.sessionId) {
      const guard = this.guard
      const mouseGuard = this.mouseGuard
      const commitGuard = this.mouseCommitGuard
      const observe = this.onMouseInput
      const send = async (point: MouseInput) => {
        guard()
        mouseGuard?.()
        if (point.type === 'mousePressed' && commitGuard) {
          await commitGuard()
          guard()
          mouseGuard?.()
        }
        if (this.closed) throw new Error('The mouse connection ended.')
        const result = await this.contents.debugger.sendCommand(method, point, this.nativeSessionId)
        // Display errors cannot change the receipt of accepted browser input.
        await observe?.(point).catch(() => {})
        return result
      }
      if (params.type === 'mouseMoved' && this.mouseMotion) return await this.mouseMotion(params, send)
      return await send(params)
    }
    return await this.contents.debugger.sendCommand(method, params,
      command.sessionId === this.sessionId ? this.nativeSessionId : command.sessionId)
  }

  async respondToDialog(accept: boolean, promptText: string | undefined, guard: Guard, signal: AbortSignal, sessionId?: string): Promise<void> {
    guard()
    if (signal.aborted || this.closed || !this.nativeSessionId) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The dialog connection ended.')
    if (sessionId !== this.nativeSessionId && !this.childSessions.has(sessionId ?? '')) throw new DesktopBrowserError('STALE_DIALOG', 'The dialog frame ended.')
    this.closeNativeDialogUi()
    // This narrow control command must remain available while a page action is
    // waiting on the dialog. It never changes the running action's guard.
    await this.contents.debugger.sendCommand('Page.handleJavaScriptDialog', {
      accept, ...(promptText === undefined ? {} : { promptText }),
    }, sessionId)
  }

  private closeNativeDialogUi(): void {
    const owner = BrowserWindow.fromWebContents(this.contents)
    if (process.versions.electron?.split('.')[0] !== '42'
      || this.contents.listenerCount('-cancel-dialogs') === 0
      || !owner || owner.isDestroyed() || this.contents.isOffscreen()) {
      throw new DesktopBrowserError('DIALOG_CONTROL_UNAVAILABLE',
        'This browser host cannot safely close the native page dialog. Respond in the browser window.',
        409, { outcome: 'not_started', retryable: false, recovery: 'manual_dialog_response' })
    }
    // Electron 42 leaves its native dialog open when CDP resolves the page's
    // callback. Its internal cancellation hook closes the UI without choosing
    // a result. Run it before CDP resumes JavaScript, which may open a new dialog.
    this.contents.emit('-cancel-dialogs')
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.contents.debugger.removeListener('message', this.onDebuggerMessage)
    this.contents.debugger.removeListener('detach', this.onDebuggerDetach)
    this.contents.removeListener('destroyed', this.onDestroyed)
    if (!this.contents.isDestroyed() && this.contents.debugger.isAttached()) {
      for (const sessionId of this.childSessions) {
        void this.contents.debugger.sendCommand('Runtime.runIfWaitingForDebugger', {}, sessionId).catch(() => {})
      }
      if (this.nativeSessionId) {
        void this.contents.debugger.sendCommand('Target.detachFromTarget', { sessionId: this.nativeSessionId }).catch(() => {})
      }
      if (this.ownedAttachment) this.contents.debugger.detach()
    }
    this.childSessions.clear()
    queueMicrotask(() => this.onclose?.('The attached renderer connection ended.'))
  }
}

type Anchor = { generation: number; element: ElementHandle<Element> }
const MAX_REFS = 160
const ACTION_TIMEOUT_MS = 10_000
const MOUSE_MOTION_BUDGET_MS = 800
type ViewportState = { width: number; height: number; deviceScaleFactor: number; scrollX: number; scrollY: number; revision: number; surfaceWidth?: number; surfaceHeight?: number }
export type BrowserDialogState = { id: string; type: string; message: string; defaultValue?: string; openedAt: string; documentEpoch: number }
type VisualObservation = { observationId: string; imageId: string; documentEpoch: number; generation: number; viewport: ViewportState;
  capturedAt: number; imageWidth: number; imageHeight: number; dataBase64: string }
type ActionResult = { action: DesktopBrowserRequest['action']; performed: boolean; execution?: Record<string, unknown>; browserState?: ReturnType<BrowserPlaywrightDriver['browserState']> }

function staleObservation(reason: DesktopBrowserObservationReason, message: string): DesktopBrowserError {
  return new DesktopBrowserError('STALE_OBSERVATION', message, 409,
    { observationReason: reason, outcome: 'not_started', retryable: false, recovery: 'observe' })
}

// Runs in an isolated world so page scripts cannot replace the revision ledger.
function viewportObservation(): ViewportState {
  const root = globalThis as typeof globalThis & { __opensquillaObservation?: { revision: number; observer: MutationObserver } }
  const decoration = (node: Node) => node instanceof Element && node.id === '__opensquilla-browser-pointer'
  const changed = (records: MutationRecord[]) => records.some(record => !decoration(record.target)
    && (record.type !== 'childList' || [...record.addedNodes, ...record.removedNodes].some(node => !decoration(node))))
  if (!root.__opensquillaObservation) {
    const state = { revision: 0, observer: null as unknown as MutationObserver }
    state.observer = new MutationObserver(records => {
      if (changed(records)) state.revision++
    })
    state.observer.observe(document, { childList: true, subtree: true, attributes: true, characterData: true })
    root.__opensquillaObservation = state
  }
  if (changed(root.__opensquillaObservation.observer.takeRecords())) root.__opensquillaObservation.revision++
  return { width: innerWidth, height: innerHeight, deviceScaleFactor: devicePixelRatio,
    scrollX, scrollY, revision: root.__opensquillaObservation.revision }
}

/** Browser actions reuse Playwright while ownership and navigation policy stay in the workbench. */
export class BrowserPlaywrightDriver {
  private browser: Browser | undefined
  private transport: RendererTransport | undefined
  private page: Page | undefined
  private mousePositionPage: Page | undefined
  private connecting: Promise<Page> | undefined
  private readonly anchors = new Map<string, Anchor>()
  private invalidationEpoch = 0
  private disposed = false
  private documentEpoch = 0
  private motionSequence = 0
  private dialogState: BrowserDialogState | undefined
  private dialogSessionId: string | undefined
  private readonly dialogWaiters = new Set<(dialog: BrowserDialogState) => void>()
  private readonly runningActions = new Set<Promise<unknown>>()
  private latestVisual: VisualObservation | undefined
  private resolvingDialog = false
  readonly pointer: BrowserPointerController

  constructor(private readonly contents: WebContents, private readonly pointerVisible: () => boolean = () => true,
    pointer?: BrowserPointerController) {
    this.pointer = pointer ?? new BrowserPointerController(contents, pointerVisible)
  }

  private async connect(): Promise<Page> {
    if (this.disposed || this.contents.isDestroyed()) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The browser page was closed.', 404)
    if (this.page && !this.page.isClosed() && this.browser?.isConnected()) return this.page
    if (this.connecting) return await this.connecting
    this.connecting = (async () => {
      const transport = new RendererTransport(this.contents)
      transport.onDialogOpening = (params, sessionId) => { this.dialogSessionId = sessionId; this.onDialog(params) }
      transport.onDialogClosed = sessionId => {
        if (this.dialogSessionId === sessionId) { this.dialogState = undefined; this.dialogSessionId = undefined; this.latestVisual = undefined }
      }
      this.transport = transport
      try {
        await transport.initialize()
        if (this.disposed) throw new Error('Browser driver was disposed during connection.')
        const browser = await chromium.connectOverCDP(transport, { noDefaults: true, timeout: ACTION_TIMEOUT_MS })
        this.browser = browser
        const pages = browser.contexts().flatMap(context => context.pages())
        if (pages.length !== 1) throw new Error('Expected exactly one authorized renderer.')
        const page = pages[0]!
        page.setDefaultTimeout(ACTION_TIMEOUT_MS)
        this.page = page
        page.on('framenavigated', frame => { if (frame === page.mainFrame()) this.documentEpoch++ })
        browser.on('disconnected', () => { this.invalidate(); if (this.browser === browser) this.page = undefined })
        if (this.disposed) { await browser.close(); throw new Error('Browser driver was disposed during connection.') }
        return page
      } catch (error) { transport.close(); throw error }
      finally { this.connecting = undefined }
    })()
    return await this.connecting
  }

  private async run<T>(assertCurrent: Guard, signal: AbortSignal, work: (page: Page) => Promise<T>): Promise<T> {
    const guard = () => {
      if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The browser operation ended; inspect before retrying.', 504)
      assertCurrent()
    }
    guard()
    const backgroundThrottling = this.contents.getBackgroundThrottling()
    // Playwright checks stable geometry across animation frames. A hidden
    // window can suspend those frames; keep only this operation's page awake.
    if (backgroundThrottling) this.contents.setBackgroundThrottling(false)
    const abort = () => { this.transport?.close(); this.page = undefined; this.invalidate() }
    signal.addEventListener('abort', abort, { once: true })
    try {
      const page = await this.connect()
      guard()
      this.transport!.guard = guard
      const result = await work(page)
      guard()
      return result
    } catch (error) {
      guard()
      if (error instanceof DesktopBrowserError) throw error
      throw new DesktopBrowserError('ACTION_UNAVAILABLE', error instanceof Error ? error.message.slice(0, 1000) : 'The browser action could not complete.')
    } finally {
      signal.removeEventListener('abort', abort)
      if (this.transport?.guard === guard) this.transport.guard = () => {}
      if (backgroundThrottling && !this.contents.isDestroyed()) this.contents.setBackgroundThrottling(true)
    }
  }

  invalidate(): void {
    this.invalidationEpoch++
    this.latestVisual = undefined
    for (const { element } of this.anchors.values()) void element.dispose().catch(() => {})
    this.anchors.clear()
  }

  get pendingDialog(): BrowserDialogState | undefined { return this.dialogState ? { ...this.dialogState } : undefined }

  async initialize(assertCurrent: Guard, signal: AbortSignal): Promise<void> {
    await this.run(assertCurrent, signal, async () => {})
  }

  waitForPendingDialog(signal: AbortSignal): Promise<BrowserDialogState> {
    if (signal.aborted) return Promise.reject(new DesktopBrowserError('TIMEOUT', 'Dialog observation was cancelled.'))
    if (this.dialogState) return Promise.resolve({ ...this.dialogState })
    return new Promise((resolve, reject) => {
      const cleanup = () => { this.dialogWaiters.delete(notify); signal.removeEventListener('abort', abort) }
      const notify = (dialog: BrowserDialogState) => { cleanup(); resolve({ ...dialog }) }
      const abort = () => { cleanup(); reject(new DesktopBrowserError('TIMEOUT', 'Dialog observation was cancelled.')) }
      this.dialogWaiters.add(notify)
      signal.addEventListener('abort', abort, { once: true })
    })
  }

  browserState() { return { capabilities: { jsPrompt: false }, dialogs: { pending: this.dialogState ? [{ ...this.dialogState }] : [] } } }

  private onDialog(dialog: Record<string, unknown>): void {
    this.latestVisual = undefined
    this.dialogState = { id: `dialog-${randomUUID()}`, type: String(dialog.type), message: String(dialog.message ?? '').slice(0, 16_384),
      ...(dialog.type === 'prompt' ? { defaultValue: String(dialog.defaultPrompt ?? '').slice(0, 16_384) } : {}),
      openedAt: new Date().toISOString(), documentEpoch: this.documentEpoch }
    for (const notify of this.dialogWaiters) notify(this.dialogState)
  }

  async waitForIdle(): Promise<void> { await Promise.allSettled([...this.runningActions]) }

  async handleDialog(request: DesktopBrowserRequest, assertCurrent: Guard, signal: AbortSignal): Promise<Record<string, unknown>> {
    assertCurrent()
    if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The dialog response was cancelled.')
    const pending = this.dialogState
    if (!pending || pending.id !== request.dialogId) throw new DesktopBrowserError('STALE_DIALOG', 'This dialog is no longer pending. Observe the page again.')
    if (this.resolvingDialog) throw new DesktopBrowserError('DIALOG_RESOLVING', 'A response to this dialog is already in progress.')
    if (typeof request.accept !== 'boolean') throw new DesktopBrowserError('INVALID_REQUEST', 'A dialog response must specify accept.')
    if (request.promptText !== undefined && (pending.type !== 'prompt' || !request.accept)) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Prompt text requires accepting a prompt dialog.')
    }
    this.resolvingDialog = true
    try {
      if (!this.transport) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The dialog connection ended.')
      await this.transport.respondToDialog(request.accept, request.promptText, () => {
        assertCurrent()
        if (this.dialogState?.id !== pending.id || this.documentEpoch !== pending.documentEpoch) {
          throw new DesktopBrowserError('STALE_DIALOG', 'The pending dialog changed before its response.')
        }
      }, signal, this.dialogSessionId)
      if (this.dialogState?.id === pending.id) this.dialogState = undefined
      this.latestVisual = undefined
      return { performed: true, dialogId: pending.id, browserState: this.browserState() }
    } finally { this.resolvingDialog = false }
  }

  private async viewport(): Promise<ViewportState> {
    const state = await this.contents.executeJavaScriptInIsolatedWorld(1005, [{ code: `(${viewportObservation.toString()})()` }]) as ViewportState
    const owner = BrowserWindow.fromWebContents(this.contents)
    const view = owner?.contentView.children.find(view => view instanceof WebContentsView && view.webContents === this.contents)
    const bounds = view?.getBounds()
    return { ...state, ...(bounds ? { surfaceWidth: bounds.width, surfaceHeight: bounds.height } : {}) }
  }

  private sameViewport(a: ViewportState, b: ViewportState): boolean {
    return !this.viewportChangeReason(a, b) && a.revision === b.revision
  }

  private viewportChangeReason(a: ViewportState, b: ViewportState): 'viewport_changed' | 'scroll_changed' | undefined {
    if (a.width !== b.width || a.height !== b.height || a.deviceScaleFactor !== b.deviceScaleFactor
      || a.surfaceWidth !== b.surfaceWidth || a.surfaceHeight !== b.surfaceHeight) return 'viewport_changed'
    if (a.scrollX !== b.scrollX || a.scrollY !== b.scrollY) return 'scroll_changed'
    return undefined
  }

  private blockedObservation(): Record<string, unknown> {
    return { observation: { observationId: `observation-${randomUUID()}`, documentEpoch: this.documentEpoch,
      capturedAt: new Date().toISOString(), consistency: 'blocked', text: '', refs: [],
      imageStatus: 'unavailable', browserState: this.browserState() } }
  }

  async observe(generation: number, assertCurrent: Guard, signal: AbortSignal,
    mode: 'auto' | 'dom' | 'hybrid' = 'auto'): Promise<Record<string, unknown>> {
    assertCurrent()
    if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The observation was cancelled.')
    if (this.dialogState) return this.blockedObservation()
    let interrupted = false
    let notify!: (dialog: BrowserDialogState) => void
    const blocked = new Promise<Record<string, unknown>>(resolve => {
      notify = () => { interrupted = true; resolve(this.blockedObservation()) }
      this.dialogWaiters.add(notify)
    })
    const guard = () => {
      assertCurrent()
      if (interrupted) throw new DesktopBrowserError('DIALOG_BLOCKED', 'A dialog interrupted observation.')
    }
    const work = (async () => {
      // Establish the page listener before reading its DOM or pixels.
      await this.run(guard, signal, async () => {})
      const epoch = this.documentEpoch
      const before = await this.viewport()
      const snapshot = await this.snapshot(generation, guard, signal)
      let screenshot: Awaited<ReturnType<BrowserPlaywrightDriver['screenshot']>> | undefined
      let imageError: string | undefined
      if (mode !== 'dom') {
        try { screenshot = await this.screenshot(guard, signal) }
        catch (error) {
          guard()
          if (signal.aborted) throw error
          imageError = error instanceof DesktopBrowserError ? error.code : 'SCREENSHOT_UNAVAILABLE'
        }
      }
      const after = await this.viewport()
      guard()
      const consistent = epoch === this.documentEpoch && this.sameViewport(before, after)
      const observationId = `observation-${randomUUID()}`
      const imageId = screenshot && consistent ? `image-${randomUUID()}` : undefined
      if (imageId && screenshot) this.latestVisual = { observationId, imageId, documentEpoch: epoch, generation, viewport: after,
        capturedAt: Date.now(), imageWidth: screenshot.width, imageHeight: screenshot.height, dataBase64: screenshot.dataBase64 }
      else this.latestVisual = undefined
      return { observation: { observationId, documentEpoch: epoch, capturedAt: new Date().toISOString(),
        consistency: consistent ? 'consistent' : 'changed', consistencyScope: 'main-document-and-viewport', text: snapshot.text,
        refs: consistent ? snapshot.refs : [], truncated: snapshot.truncated, viewport: after,
        browserState: this.browserState(), imageStatus: imageId ? 'available' : mode === 'dom' ? 'omitted' : 'unavailable',
        ...(imageError ? { imageError } : {}),
        ...(imageId && screenshot ? { image: { imageId, width: screenshot.width, height: screenshot.height,
          viewportWidth: after.width, viewportHeight: after.height, coordinateSpace: 'image-pixels', mimeType: screenshot.mimeType } } : {}),
      }, ...(imageId && screenshot ? { dataBase64: screenshot.dataBase64 } : {}) }
    })()
    this.runningActions.add(work)
    void work.finally(() => this.runningActions.delete(work)).catch(() => {})
    try { return await Promise.race([work, blocked]) }
    finally { this.dialogWaiters.delete(notify) }
  }

  async batch(request: DesktopBrowserRequest, generation: number, assertCurrent: Guard, signal: AbortSignal): Promise<Record<string, unknown>> {
    const actions = request.actions ?? []
    if (actions.length < 1 || actions.length > 3) throw new DesktopBrowserError('INVALID_REQUEST', 'Use one to three browser actions.')
    if (actions.slice(0, -1).some(action => !['fill', 'select'].includes(action.action ?? ''))) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Only field updates may precede the final action in a batch.')
    }
    const results: Record<string, unknown>[] = []
    const epoch = this.documentEpoch
    const invalidation = this.invalidationEpoch
    let state = 'completed'
    for (const [index, action] of actions.entries()) {
      try {
        const result = await this.act({ ...request, ...action, operation: 'act' }, generation, assertCurrent, signal)
        results.push({ index, ...result })
        if (result.execution?.state === 'blocked') { state = 'blocked'; break }
        if (this.documentEpoch !== epoch) { state = index === actions.length - 1 ? 'completed' : 'partial'; break }
        if (this.invalidationEpoch !== invalidation) { state = index === actions.length - 1 ? 'completed' : 'partial'; break }
      } catch (error) {
        const failure = error instanceof DesktopBrowserError ? error : new DesktopBrowserError('ACTION_UNAVAILABLE', 'The browser action ended without a confirmed result.')
        const beforeInput = ['STALE_ELEMENT', 'STALE_OBSERVATION', 'IMAGE_NOT_DELIVERED', 'VISUAL_TARGET_HIDDEN', 'INVALID_REQUEST'].includes(failure.code)
        results.push({ ...failure.details, index, performed: false, code: failure.code, message: failure.message,
          outcome: failure.details.outcome ?? (beforeInput ? 'not_started' : 'unknown'), retryable: false })
        state = results.length > 1 ? 'partial' : signal.aborted ? 'cancelled' : 'failed'
        break
      }
    }
    while (results.length < actions.length) results.push({ index: results.length, state: 'not_started' })
    let observation: Record<string, unknown>
    try { observation = await this.observe(generation, assertCurrent, signal, request.observationMode ?? 'auto') }
    catch (error) { observation = { observation: { consistency: 'unavailable', imageStatus: 'unavailable',
      error: error instanceof DesktopBrowserError ? error.code : 'OBSERVATION_UNAVAILABLE', browserState: this.browserState() } } }
    return { execution: { state, actions: results }, ...observation }
  }

  async snapshot(generation: number, assertCurrent: Guard, signal: AbortSignal): Promise<{
    text: string; refs: Record<string, unknown>[]; truncated: boolean
  }> {
    return await this.run(assertCurrent, signal, async page => {
      this.invalidate()
      const epoch = this.invalidationEpoch
      const assertSnapshot = () => {
        if (epoch !== this.invalidationEpoch) throw new DesktopBrowserError('PAGE_CHANGED', 'The page changed during inspection. Request a new snapshot.')
      }
      const frames = page.frames().slice(0, 16)
      const ranked = []
      let truncated = page.frames().length > frames.length
      for (const frame of frames) {
        if (frame.isDetached() || !frame.url()) { truncated = true; continue }
        try {
          const modal = await frame.evaluate(() => Array.from(document.querySelectorAll(
            'dialog:modal,[role="dialog"][aria-modal="true"],[role="alertdialog"]',
          )).some(node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node)
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'
          }))
          ranked.push({ frame, modal })
        } catch { truncated = true }
      }
      ranked.sort((a, b) => Number(b.modal) - Number(a.modal))
      const refs: Record<string, unknown>[] = []
      const texts: string[] = []
      for (const { frame, modal } of ranked) {
        if (refs.length >= MAX_REFS) { truncated = true; break }
        let collection: Awaited<ReturnType<typeof frame.evaluateHandle>> | undefined
        try {
          const text = await frame.locator('body').ariaSnapshot({ timeout: frame === page.mainFrame() ? ACTION_TIMEOUT_MS : 1500 })
          texts.push(`${modal ? '[Active modal]\n' : ''}${frame === page.mainFrame() ? '' : `[Frame ${frame.url().slice(0, 500)}]\n`}${text}`)
          collection = await frame.evaluateHandle(limit => {
            const visible = (node: Element) => {
              const rect = node.getBoundingClientRect(), style = getComputedStyle(node)
              return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
            }
            const dialogs = Array.from(document.querySelectorAll('dialog:modal,[role="dialog"][aria-modal="true"],[role="alertdialog"]')).filter(visible)
            const root = dialogs.at(-1) ?? document
            return Array.from(root.querySelectorAll(
              'a,button,input,textarea,select,summary,[role],[contenteditable],h1,h2,h3,p,label',
            )).slice(0, 2400).filter(visible).slice(0, limit)
          }, MAX_REFS - refs.length)
          for (const [key, handle] of await collection.getProperties()) {
            if (!/^\d+$/.test(key)) { await handle.dispose(); continue }
            const element = handle.asElement() as ElementHandle<Element> | null
            if (!element) { await handle.dispose(); continue }
            const description = await element.evaluate(node => {
              const text = (value: string | null | undefined) => (value ?? '').replace(/\s+/g, ' ').trim().slice(0, 256)
              const input = node instanceof HTMLInputElement ? node : undefined
              const textarea = node instanceof HTMLTextAreaElement ? node : undefined
              const select = node instanceof HTMLSelectElement ? node : undefined
              const button = node instanceof HTMLButtonElement ? node : undefined
              const type = input?.type ?? button?.type
              const inputRoles: Record<string, string> = {
                button: 'button', submit: 'button', reset: 'button', image: 'button',
                text: 'textbox', email: 'textbox', password: 'textbox', tel: 'textbox', url: 'textbox',
                search: 'searchbox', number: 'spinbutton', range: 'slider', checkbox: 'checkbox', radio: 'radio',
              }
              const implicitRole = input ? inputRoles[input.type] ?? 'input'
                : textarea ? 'textbox' : select ? select.multiple || select.size > 1 ? 'listbox' : 'combobox'
                  : node.matches('a[href],area[href]') ? 'link'
                    : node.localName === 'summary' ? 'button' : node.localName
              const labelledBy = (node.getAttribute('aria-labelledby') ?? '').split(/\s+/).filter(Boolean)
                .map(id => node.ownerDocument.getElementById(id)?.textContent ?? '').join(' ')
              const labels = input?.labels ?? textarea?.labels ?? select?.labels ?? button?.labels
              const labelText = labels ? Array.from(labels, label => label.textContent ?? '').join(' ') : ''
              const buttonText = input && ['button', 'submit', 'reset'].includes(input.type)
                ? input.value || (input.type === 'submit' ? 'Submit' : input.type === 'reset' ? 'Reset' : '')
                : input?.type === 'image' ? input.alt : ''
              const name = [labelledBy, node.getAttribute('aria-label'), labelText, buttonText,
                (node as HTMLElement).innerText, node.getAttribute('placeholder'), node.getAttribute('title')]
                .map(text).find(Boolean) ?? ''
              const disabled = node.matches(':disabled,[aria-disabled="true"]')
              const readonly = Boolean(input?.readOnly || textarea?.readOnly || node.getAttribute('aria-readonly') === 'true')
              const fillable = textarea || (node as HTMLElement).isContentEditable || input && [
                'text', 'email', 'password', 'tel', 'url', 'search', 'number', 'date', 'datetime-local', 'time', 'month', 'week',
              ].includes(input.type)
              // The ref describes the action the control accepts, not merely
              // its HTML tag. Button values are labels; text values are state.
              const value = input && !['password', 'file'].includes(input.type) ? input.value
                : textarea?.value ?? select?.value
              return { tagName: node.localName, role: node.getAttribute('role') || implicitRole, name, disabled,
                editable: Boolean(fillable) && !disabled && !readonly && !node.closest('[inert]'),
                ...(type ? { type } : {}), ...(input || textarea ? { readonly } : {}),
                ...(value !== undefined ? { value: value.slice(0, 1024) } : {}) }
            })
            assertSnapshot()
            const ref = `e-${randomUUID()}`
            this.anchors.set(ref, { generation, element })
            refs.push({ ref, ...description, ...(modal ? { modal: true } : {}),
              ...(frame === page.mainFrame() ? {} : { frameUrl: frame.url().slice(0, 8192) }) })
          }
        } catch (error) {
          assertSnapshot()
          if (frame === page.mainFrame()) { this.invalidate(); throw error }
          truncated = true
        } finally { await collection?.dispose().catch(() => {}) }
      }
      assertSnapshot()
      const text = texts.join('\n')
      return { text: text.slice(0, 24_000), refs, truncated: truncated || refs.length === MAX_REFS || text.length > 24_000 }
    })
  }

  async act(request: DesktopBrowserRequest, generation: number, assertCurrent: Guard, signal: AbortSignal): Promise<ActionResult> {
    assertCurrent()
    if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The browser action was cancelled.')
    const blockedResult = (): ActionResult => ({ action: request.action, performed: false,
      execution: { state: 'blocked', outcome: 'unknown', retryable: false, blockerId: this.dialogState?.id }, browserState: this.browserState() })
    if (this.dialogState) return blockedResult()
    let interrupted = false
    let notify!: (dialog: BrowserDialogState) => void
    const blocked = new Promise<ActionResult>(resolve => {
      notify = () => { interrupted = true; resolve(blockedResult()) }
      this.dialogWaiters.add(notify)
    })
    const guard = () => {
      assertCurrent()
      if (interrupted) throw new DesktopBrowserError('DIALOG_BLOCKED', 'A dialog interrupted the browser action. Resolve it before continuing.')
    }
    const work = this.performAct(request, generation, guard, signal)
    this.runningActions.add(work)
    void work.finally(() => this.runningActions.delete(work)).catch(() => {})
    try { return await Promise.race([work, blocked]) }
    finally { this.dialogWaiters.delete(notify) }
  }

  private async performAct(request: DesktopBrowserRequest, generation: number, assertCurrent: Guard, signal: AbortSignal): Promise<ActionResult> {
    return await this.run(assertCurrent, signal, async page => {
      const coordinate = request.x !== undefined || request.y !== undefined
      let commitGuard: (() => Promise<void>) | undefined
      let commitFailure: DesktopBrowserError | undefined
      if (coordinate) {
        if (!this.isSurfaceVisible()) throw new DesktopBrowserError('VISUAL_TARGET_HIDDEN', 'Show this browser tab in the sidebar before using screenshot coordinates.')
        const observation = this.latestVisual
        if (!observation) throw staleObservation('observation_missing', 'No current screenshot is available. Observe the page before using coordinates.')
        if (!request.observationId || !request.imageId
          || observation.observationId !== request.observationId || observation.imageId !== request.imageId) {
          throw staleObservation('observation_mismatch', 'These coordinates reference a different observation or image. Use a new observation.')
        }
        if (observation.documentEpoch !== this.documentEpoch) {
          throw staleObservation('document_changed', 'The document changed after this screenshot. Request a new observation.')
        }
        if (observation.generation !== generation) {
          throw staleObservation('generation_changed', 'The browser page generation changed. Request a new observation.')
        }
        const viewport = await this.viewport()
        const viewportReason = this.viewportChangeReason(observation.viewport, viewport)
        if (viewportReason) {
          this.latestVisual = undefined
          throw staleObservation(viewportReason, 'The viewport or scroll position changed. Request a new observation.')
        }
        if (!['click', 'hover', 'scroll'].includes(request.action ?? '') || request.ref
          || !Number.isFinite(request.x) || !Number.isFinite(request.y)
          || request.x! < 0 || request.y! < 0 || request.x! >= observation.imageWidth || request.y! >= observation.imageHeight) {
          throw new DesktopBrowserError('INVALID_REQUEST', 'Coordinates must identify a point within the referenced screenshot.')
        }
        const x = Math.max(0, Math.floor(request.x!) - 24), y = Math.max(0, Math.floor(request.y!) - 24)
        const crop = { x, y, width: Math.min(49, observation.imageWidth - x), height: Math.min(49, observation.imageHeight - y) }
        const original = nativeImage.createFromBuffer(Buffer.from(observation.dataBase64, 'base64'))
        const originalPixels = original.crop(crop).toBitmap()
        commitGuard = async () => {
          try {
            assertCurrent()
            if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The coordinate action was cancelled.')
            if (!this.isSurfaceVisible()) throw new DesktopBrowserError('VISUAL_TARGET_HIDDEN', 'The visual target was hidden before the click.')
            for (let attempt = 0; attempt < 2; attempt++) {
              const beforeCapture = await this.viewport()
              const fresh = await this.captureScreenshot(page)
              const currentViewport = await this.viewport()
              assertCurrent()
              let changed: DesktopBrowserObservationReason | undefined = observation.documentEpoch !== this.documentEpoch
                ? 'document_changed'
                : this.viewportChangeReason(observation.viewport, beforeCapture)
                  ?? this.viewportChangeReason(observation.viewport, currentViewport)
                  ?? (fresh.width !== observation.imageWidth || fresh.height !== observation.imageHeight ? 'viewport_changed' : undefined)
              if (!changed && beforeCapture.revision !== currentViewport.revision) {
                if (attempt === 0) continue
                changed = 'validation_unstable'
              }
              if (!changed) {
                const current = nativeImage.createFromBuffer(Buffer.from(fresh.dataBase64, 'base64'))
                if (!originalPixels.equals(current.crop(crop).toBitmap())) changed = 'target_pixels_changed'
              }
              // Time and past unrelated DOM updates do not invalidate matching
              // target pixels, but each capture must be stable before input.
              if (changed) {
                this.latestVisual = undefined
                throw staleObservation(changed, 'The screenshot target changed before input. Request a new observation.')
              }
              return
            }
          } catch (error) {
            if (error instanceof DesktopBrowserError) commitFailure = error
            throw error
          }
        }
        await commitGuard()
        request = { ...request, x: request.x! * viewport.width / observation.imageWidth,
          y: request.y! * viewport.height / observation.imageHeight }
      }
      const anchor = request.ref ? this.anchors.get(request.ref) : undefined
      if (request.ref && (!anchor || anchor.generation !== generation
        || !await anchor.element.evaluate(node => node.isConnected).catch(() => false))) {
        throw new DesktopBrowserError('STALE_ELEMENT', 'The element reference expired. Request a new snapshot.')
      }
      const options = { timeout: ACTION_TIMEOUT_MS }
      this.latestVisual = undefined
      // Hidden Chromium views can defer standalone mousemove acknowledgments
      // until another input arrives. Keep their normal atomic click path.
      const animateMouse = this.isSurfaceVisible()
      const moveSteps = 1
      if (animateMouse && this.mousePositionPage !== page
        && ['click', 'hover', 'scroll'].includes(request.action ?? '')) {
        const position = this.pointer.currentPosition()
        if (position) await page.mouse.move(position.x, position.y, { steps: 1 })
        this.mousePositionPage = page
      }
      const documentEpoch = this.documentEpoch
      const motionGuard = () => {
        if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The browser operation ended; inspect before retrying.', 504)
        assertCurrent()
        if (documentEpoch !== this.documentEpoch) throw new DesktopBrowserError('PAGE_CHANGED', 'The page changed during mouse movement.')
      }
      const viewport = animateMouse ? await page.evaluate(() => ({
        width: innerWidth, height: innerHeight,
        reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      })).catch(() => undefined) : undefined
      let motionTime = 0
      const motion: MouseMotion = async (params, send) => {
        const from = this.pointer.currentPosition() ?? { x: 0, y: 0 }
        if (!viewport || viewport.reducedMotion || !this.isSurfaceVisible()
          || (typeof params.buttons === 'number' && params.buttons !== 0)
          || typeof params.x !== 'number' || typeof params.y !== 'number'
          || motionTime >= MOUSE_MOTION_BUDGET_MS) return await send(params)
        const start = performance.now()
        const points = planMouseTrajectory(from, { x: params.x, y: params.y }, viewport, this.motionSequence++)
        try {
          let result: unknown
          for (const point of points) {
            motionGuard()
            if (!this.isSurfaceVisible() || motionTime + performance.now() - start >= MOUSE_MOTION_BUDGET_MS) {
              return await send(params)
            }
            const delay = point.at - (performance.now() - start)
            if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay))
            motionGuard()
            if (!this.isSurfaceVisible()) return await send(params)
            result = await send({ ...params, x: point.x, y: point.y })
          }
          return result
        } finally { motionTime += performance.now() - start }
      }
      let pressedPoint: { x: number; y: number } | undefined
      const observer = async (params: Record<string, unknown>) => {
        if (documentEpoch !== this.documentEpoch) return
        if (typeof params.x !== 'number' || typeof params.y !== 'number') return
        this.mousePositionPage = page
        if (params.type === 'mousePressed') pressedPoint = { x: params.x, y: params.y }
        if (params.type === 'mouseReleased') pressedPoint = undefined
        const action = params.type === 'mouseReleased' ? 'click'
          : params.type === 'mousePressed' ? 'down'
          : params.type === 'mouseWheel' ? 'scroll' : 'move'
        await this.pointer.update({ x: params.x, y: params.y, action, immediate: true })
      }
      const transport = this.transport!
      transport.onMouseInput = observer
      transport.mouseGuard = motionGuard
      transport.mouseCommitGuard = commitGuard
      if (animateMouse) transport.mouseMotion = motion
      try {
        switch (request.action) {
          // Element clicks use Playwright Mouse internally, retaining its
          // enabled, stable, viewport and hit-target checks before input.
          // A nonzero delay makes Playwright await movement before pressing.
          // Hidden clicks retain its concurrent input path to wake the renderer.
          case 'click':
            if (coordinate) await page.mouse.click(request.x!, request.y!, { delay: 1 })
            else await anchor!.element.click({ ...options, ...(animateMouse ? { delay: 1 } : {}) })
            break
          case 'hover':
            if (coordinate) { await page.mouse.move(request.x!, request.y!, { steps: moveSteps }); break }
            if (animateMouse) await this.moveToElement(page, anchor!.element, moveSteps)
            transport.mouseMotion = undefined
            await anchor!.element.hover(options)
            break
          case 'fill': await anchor!.element.fill(request.text ?? '', options); break
          case 'select': await anchor!.element.selectOption(request.text ?? '', options); break
          case 'press':
            if (anchor) await anchor.element.press(request.key!, options)
            else await page.keyboard.press(request.key!)
            break
          case 'scroll': {
            if (coordinate) await page.mouse.move(request.x!, request.y!, { steps: moveSteps })
            else if (anchor) {
              if (animateMouse) await this.moveToElement(page, anchor.element, moveSteps)
              transport.mouseMotion = undefined
              await anchor.element.hover(options)
            } else {
              const center = await page.evaluate(() => ({ x: innerWidth / 2, y: innerHeight / 2 }))
              await page.mouse.move(center.x, center.y, { steps: moveSteps })
            }
            const amount = request.amount ?? 600
            await page.mouse.wheel(request.direction === 'left' ? -amount : request.direction === 'right' ? amount : 0,
              request.direction === 'up' ? -amount : request.direction === 'down' ? amount : 0)
            // Hidden views acknowledge a wheel before painting its scroll.
            // Request a frame without showing or focusing the retained page.
            if (!this.isSurfaceVisible()) await this.wakeHiddenFrame()
            break
          }
          default: throw new DesktopBrowserError('INVALID_REQUEST', 'Unsupported browser action.', 400)
        }
        if (this.pointerVisible() && documentEpoch === this.documentEpoch
          && ['fill', 'select', 'press'].includes(request.action ?? '')) {
          // Decoration failure must never turn a completed click into a retry.
          try {
            const box = anchor ? await anchor.element.boundingBox() : null
            const point = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null
            if (point) await this.pointer.update({ ...point, action: request.action ?? 'press' })
          } catch {}
        }
        return { action: request.action, performed: true }
      } catch (error) {
        if (commitFailure) throw commitFailure
        throw error
      } finally {
        if (transport.onMouseInput === observer) transport.onMouseInput = undefined
        if (transport.mouseMotion === motion) transport.mouseMotion = undefined
        if (transport.mouseGuard === motionGuard) transport.mouseGuard = undefined
        if (transport.mouseCommitGuard === commitGuard) transport.mouseCommitGuard = undefined
        if (pressedPoint && documentEpoch === this.documentEpoch) {
          await this.pointer.update({ ...pressedPoint, action: 'cancel', immediate: true })
        }
      }
    })
  }

  private isSurfaceVisible(): boolean {
    if (!this.pointerVisible() || this.contents.isDestroyed()) return false
    const owner = BrowserWindow.fromWebContents(this.contents)
    if (!owner || !owner.isVisible()) return false
    if (owner.webContents === this.contents) return true
    return owner.contentView.children.some(view => view instanceof WebContentsView
      && view.webContents === this.contents && view.getVisible())
  }

  private async wakeHiddenFrame(): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      // Wheel dispatch has already succeeded. A failed or stalled frame must
      // not turn it into a retry; the capture is read-only and never retained.
      await Promise.race([
        this.contents.capturePage(undefined, { stayHidden: true, stayAwake: true }).catch(() => {}),
        new Promise<void>(resolve => { timer = setTimeout(resolve, 250) }),
      ])
    } finally { clearTimeout(timer) }
  }

  private async moveToElement(page: Page, element: ElementHandle<Element>, steps: number): Promise<void> {
    await element.scrollIntoViewIfNeeded({ timeout: ACTION_TIMEOUT_MS })
    const box = await element.boundingBox()
    if (!box) return // The checked element action reports hidden/detached nodes.
    const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }))
    const left = Math.max(0, box.x), right = Math.min(viewport.width, box.x + box.width)
    const top = Math.max(0, box.y), bottom = Math.min(viewport.height, box.y + box.height)
    if (right > left && bottom > top) {
      await page.mouse.move((left + right) / 2, (top + bottom) / 2, { steps })
    }
  }

  private async captureScreenshot(page: Page): Promise<{ mimeType: 'image/png'; dataBase64: string; width: number; height: number }> {
    const epoch = this.invalidationEpoch
    const png = await this.pointer.pauseForScreenshot(() => page.screenshot({ type: 'png', scale: 'css', caret: 'initial', timeout: ACTION_TIMEOUT_MS }))
    if (epoch !== this.invalidationEpoch) throw new DesktopBrowserError('PAGE_CHANGED', 'The page changed during capture.')
    if (png.length < 24 || png.length > 5 * 1024 * 1024 || png.subarray(1, 4).toString() !== 'PNG') {
      throw new DesktopBrowserError('SCREENSHOT_UNAVAILABLE', 'The screenshot is empty or exceeds 5 MiB.')
    }
    return { mimeType: 'image/png', dataBase64: png.toString('base64'), width: png.readUInt32BE(16), height: png.readUInt32BE(20) }
  }

  async screenshot(assertCurrent: Guard, signal: AbortSignal): Promise<{ mimeType: 'image/png'; dataBase64: string; width: number; height: number }> {
    return await this.run(assertCurrent, signal, page => this.captureScreenshot(page))
  }

  async dispose(): Promise<void> {
    this.disposed = true
    await this.pointer.dispose()
    this.invalidate()
    this.transport?.close()
    await this.browser?.close().catch(() => {})
    this.page = undefined
  }
}
