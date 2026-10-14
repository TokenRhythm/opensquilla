import { createWidget, defaultThemeTokens, type ThemeTokens, type WidgetHandle } from 'generative-frame'
import type { VisualizePresentation, VisualizeRuntime, VisualizeRuntimeOptions, VisualizeSize } from './types'
import { abortableClipboardPreparation, assertClipboardPreparationActive } from '@/utils/imageClipboard'
import { checkCaptureLayout, validateCapturePng, VisualizeCaptureError, VISUALIZE_CAPTURE_TIMEOUT_MS } from './png'

export const VISUALIZE_RUNTIME_LABEL = 'A · generative-frame 0.0.5'
const MIN_HEIGHT = 120
const MAX_HEIGHT = 1200
const MOUNT_PHASE_TIMEOUT = 15_000
const bridge = `<script>
window.opensquillaVisualize={ready:Promise.resolve(),getState:()=>window.genframe.state,setState:value=>window.genframe.setState(value),sendPrompt:text=>window.genframe.sendPrompt(String(text))};
window.addEventListener('keydown',event=>{
  if(event.key!=='Escape'||event.isComposing||window.genframe.context.displayMode!=='fullscreen')return;
  // Let generated controls consume Escape before asking the host to collapse.
  setTimeout(()=>{
    if(!event.defaultPrevented&&window.genframe.context.displayMode==='fullscreen'){
      window.genframe.requestDisplayMode('inline').catch(()=>{});
    }
  },0);
},true);
window.addEventListener('pointerdown',()=>{
  window.genframe.updateModelContext({type:'opensquilla:pointerdown:v1'}).catch(()=>{});
},true);
</script>`

// Resolve host values before crossing the opaque-origin boundary. Sending
// var(--text) itself would leave it undefined in the generated document.
const HOST_TOKENS: Record<string, string> = {
  '--color-background': '--bg',
  '--color-surface': '--bg-surface',
  '--color-surface-muted': '--bg-surface-2',
  '--color-text': '--text',
  '--color-text-muted': '--text-muted',
  '--color-text-subtle': '--text-dim',
  '--color-border': '--border',
  '--color-border-strong': '--border-strong',
  '--color-accent': '--accent',
  '--color-accent-text': '--accent-foreground',
  '--color-danger': '--danger',
  '--color-success': '--ok',
  '--color-warning': '--warn',
  '--color-info': '--info',
  '--font-sans': '--font-sans',
  '--font-mono': '--font-mono',
  '--radius-sm': '--radius-sm',
  '--radius-md': '--radius-md',
  '--radius-lg': '--radius-lg',
  ...Object.fromEntries(Array.from({ length: 6 }, (_, index) => [`--chart-${index + 1}`, `--chart-${index + 1}`])),
}

function hostTheme(container: HTMLElement, theme: 'light' | 'dark'): ThemeTokens {
  const variables = { ...defaultThemeTokens(theme).variables }
  const style = container.ownerDocument.defaultView?.getComputedStyle(container)
  if (style) {
    for (const [target, source] of Object.entries(HOST_TOKENS)) {
      const value = style.getPropertyValue(source).trim()
      if (value) variables[target] = value
    }
    if (style.fontSize) variables['--opensquilla-visualize-font-size'] = style.fontSize
  }
  return { colorScheme: theme, variables }
}

function waitForPhase<T>(promise: Promise<T>, signal: AbortSignal | undefined, phase: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => finish(() => reject(new DOMException('Aborted', 'AbortError')))
    const timer = setTimeout(() => finish(() => reject(new Error(`The canvas ${phase} timed out`))), MOUNT_PHASE_TIMEOUT)
    const finish = (settle: () => void) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      settle()
    }
    signal?.addEventListener('abort', abort, { once: true })
    // Handle late upstream rejection even after cancellation and disposal.
    promise.then(value => finish(() => resolve(value)), error => finish(() => reject(error)))
    if (signal?.aborted) abort()
  })
}

function validSize(size: VisualizeSize): boolean {
  return Boolean(size && Number.isFinite(size.width) && Number.isFinite(size.height) && size.width >= 0 && size.height >= 0)
}

function renderingError(error: unknown): Error {
  if (error instanceof Error) return error
  const message = (error as { message?: unknown } | null)?.message
  return new Error(typeof message === 'string' ? message.slice(0, 500) : 'The canvas reported a rendering error')
}

export async function mountVisualize(container: HTMLElement, options: VisualizeRuntimeOptions): Promise<VisualizeRuntime> {
  if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  if (new TextEncoder().encode(options.html).length > 1024 * 1024) throw new Error('The canvas exceeds the HTML size limit')
  let disposed = false
  let widget: WidgetHandle | undefined
  let presentation: VisualizePresentation = { mode: 'inline' }
  let contentSize: VisualizeSize = { width: 0, height: MIN_HEIGHT }
  let sizeFrame: number | undefined
  let cancelLayoutBarrier: (() => void) | undefined
  let activeCapture: AbortController | undefined
  let lastLimit = MAX_HEIGHT
  let lastMode: VisualizePresentation['mode'] = 'inline'
  let lastOverflow = 'hidden'
  let firstError: Error | undefined
  let tokens = hostTheme(container, options.theme)
  const view = container.ownerDocument.defaultView ?? window
  const heightLimit = () => presentation.mode === 'expanded' && Number.isFinite(presentation.availableHeight)
    ? Math.max(1, Math.min(MAX_HEIGHT, presentation.availableHeight!)) : MAX_HEIGHT

  const applySize = () => {
    if (disposed || !widget) return
    const limit = heightLimit()
    // 0.0.5 captures maxHeight at creation. setContext alone does not update
    // its iframe clamp, so apply the viewport height after its onResize.
    if (widget.iframe) widget.iframe.style.height = `${Math.min(Math.max(contentSize.height, MIN_HEIGHT), limit)}px`
    const overflow = contentSize.height > limit ? 'auto' : 'hidden'
    if (lastLimit !== limit || lastMode !== presentation.mode || lastOverflow !== overflow) {
      // Context-only height changes do not trigger guest remeasurement in
      // 0.0.5. Keep overflow in sync even when content width/height is unchanged.
      widget.setContext({
        displayMode: presentation.mode === 'expanded' ? 'fullscreen' : 'inline',
        containerDimensions: { maxHeight: limit },
        // The upstream context merge is shallow. Include the complete palette
        // so themechange listeners keep seeing it during presentation changes.
        styles: { variables: { ...tokens.variables, '--opensquilla-visualize-overflow': overflow } },
      })
      lastLimit = limit
      lastMode = presentation.mode
      lastOverflow = overflow
    }
  }
  const dispose = () => {
    if (disposed) return
    disposed = true
    activeCapture?.abort(new DOMException('Canvas disposed', 'AbortError'))
    options.signal?.removeEventListener('abort', dispose)
    if (sizeFrame !== undefined) view.cancelAnimationFrame(sizeFrame)
    cancelLayoutBarrier?.()
    widget?.dispose()
  }
  widget = createWidget({
    container,
    opaqueOrigin: true,
    tokens,
    context: { displayMode: 'inline' },
    csp: { cdnOrigins: [], connectOrigins: [], imageOrigins: [], frameOrigins: [] },
    initialState: options.initialState,
    minHeight: MIN_HEIGHT,
    maxHeight: MAX_HEIGHT,
    animate: false,
    // genframe measures #gf-root, not body padding. Keep spacing in that
    // measured root for fragments and historical complete documents alike.
    css: 'html{overflow-y:var(--opensquilla-visualize-overflow,hidden)!important}body{margin:0!important;padding:0!important;font-size:var(--opensquilla-visualize-font-size,16px)}#gf-root{display:flow-root;padding:8px;box-sizing:border-box}',
    onPrompt(text) { if (!disposed) options.onPrompt(text) },
    onWidgetState(state) { if (!disposed) options.onState(state) },
    onError(error) { if (!disposed) { const cause = renderingError(error); firstError ??= cause; options.onError(cause) } },
    onRequestDisplayMode({ mode }) {
      if (disposed) return { mode: 'inline' }
      // Only host controls may expand a canvas; guests can request dismissal.
      if (mode === 'inline' && presentation.mode === 'expanded' && options.onDismiss) {
        const actual = options.onDismiss()
        return { mode: actual === 'inline' ? 'inline' : 'fullscreen' }
      }
      return { mode: presentation.mode === 'expanded' ? 'fullscreen' : 'inline' }
    },
    // Use the existing private RPC port for this one host-owned UI signal.
    // No model context is accepted or forwarded by this adapter.
    onUpdateModelContext(params) {
      if (disposed) return {}
      if (!params || typeof params !== 'object' || Array.isArray(params)
        || Object.keys(params).length !== 1 || (params as { type?: unknown }).type !== 'opensquilla:pointerdown:v1') {
        throw new Error('Model context updates are unavailable in this canvas')
      }
      options.onPointerDown?.()
      return {}
    },
    onResize(size) {
      if (disposed || !validSize(size)) return
      contentSize = { width: size.width, height: size.height }
      if (sizeFrame !== undefined) return
      sizeFrame = view.requestAnimationFrame(() => {
        sizeFrame = undefined
        if (disposed) return
        applySize()
        options.onResize?.(contentSize)
      })
    },
    onOpenLink: () => { throw new Error('External links are unavailable in this canvas') },
    onCallTool: () => { throw new Error('Direct tool calls are unavailable in this canvas') },
  })
  options.signal?.addEventListener('abort', dispose, { once: true })
  try {
    await waitForPhase(widget.ready, options.signal, 'connection')
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    widget.write(bridge + options.html)
    const finishRender = async () => {
      let result = await widget!.end()
      if (disposed || options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const frameRect = widget!.iframe?.getBoundingClientRect()
      const containerRect = container.getBoundingClientRect()
      // A new iframe inside a changing modal can finish its scripts before
      // Chromium lays out its document, despite the host frame having size.
      // Re-measure once after host layout; never re-run the generated code.
      if (result.blank && !result.errorCount && !firstError
        && (result.size.width < 2 || result.size.height < 2)
        && frameRect && frameRect.width >= 2 && frameRect.height >= 2
        && containerRect.width >= 2 && containerRect.height >= 2) {
        await new Promise<void>((resolve, reject) => {
          const frame = view.requestAnimationFrame(() => {
            cancelLayoutBarrier = undefined
            if (disposed || options.signal?.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
            widget!.iframe?.getBoundingClientRect()
            resolve()
          })
          cancelLayoutBarrier = () => {
            cancelLayoutBarrier = undefined
            view.cancelAnimationFrame(frame)
            reject(new DOMException('Aborted', 'AbortError'))
          }
        })
        if (disposed || options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
        const inspection = await widget!.inspect()
        result = { size: inspection.size, blank: inspection.blank, errorCount: inspection.errors.length }
      }
      return result
    }
    // end, the layout barrier and inspection share this single phase budget.
    const result = await waitForPhase(finishRender(), options.signal, 'render')
    if (result.blank) {
      const frameRect = widget.iframe?.getBoundingClientRect()
      const containerRect = container.getBoundingClientRect()
      throw new Error(`The canvas produced no visible content (root=${result.size.width}x${result.size.height}; iframe=${frameRect?.width ?? 0}x${frameRect?.height ?? 0}; container=${containerRect.width}x${containerRect.height}; iframeHeight=${widget.iframe?.style.height ?? ''}; mode=${presentation.mode}; errors=${result.errorCount})`)
    }
    if (result.errorCount) throw firstError ?? new Error('The canvas reported a rendering error')
    if (validSize(result.size)) contentSize = result.size
    applySize()
  } catch (error) { dispose(); throw error }
  return {
    dispose,
    async capture(request = {}) {
      if (disposed) throw new DOMException('Canvas disposed', 'AbortError')
      if (activeCapture) throw new Error('A canvas capture is already in progress')
      const controller = new AbortController()
      activeCapture = controller
      const abort = () => controller.abort(request.signal?.reason ?? new DOMException('Cancelled', 'AbortError'))
      request.signal?.addEventListener('abort', abort, { once: true })
      if (request.signal?.aborted) abort()
      const timer = setTimeout(() => controller.abort(new VisualizeCaptureError('timedOut')), VISUALIZE_CAPTURE_TIMEOUT_MS)
      try {
        assertClipboardPreparationActive(controller.signal)
        const inspection = await abortableClipboardPreparation(widget!.inspect(), controller.signal)
        assertClipboardPreparationActive(controller.signal)
        if (!inspection.ended || inspection.blank || inspection.errors.length) throw new VisualizeCaptureError('unavailable')
        checkCaptureLayout(inspection.size.width, inspection.size.height)
        const screenshot = await abortableClipboardPreparation(widget!.screenshot({ scale: 1 }), controller.signal)
        assertClipboardPreparationActive(controller.signal)
        const blob = await validateCapturePng(screenshot, controller.signal, container.ownerDocument)
        assertClipboardPreparationActive(controller.signal)
        return blob
      } finally {
        clearTimeout(timer)
        request.signal?.removeEventListener('abort', abort)
        if (activeCapture === controller) activeCapture = undefined
      }
    },
    updateTheme(theme) {
      if (disposed) return
      tokens = hostTheme(container, theme)
      widget!.setTheme({ ...tokens, variables: { ...tokens.variables, '--opensquilla-visualize-overflow': lastOverflow } })
    },
    setPresentation(next) { if (!disposed) { presentation = { ...next }; applySize() } },
  }
}
