// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CreateWidgetOptions, EndResult, WidgetHandle } from 'generative-frame'
import { mountVisualize } from './runtime'
import type { VisualizeRuntimeOptions } from './types'
import { VisualizeCaptureError } from './png'

const driver = vi.hoisted(() => ({ create: vi.fn(), decode: vi.fn() }))
vi.mock('generative-frame', async importOriginal => ({
  ...await importOriginal<typeof import('generative-frame')>(),
  createWidget: driver.create,
}))
vi.mock('./png', async importOriginal => ({ ...await importOriginal<typeof import('./png')>(), validateCapturePng: driver.decode }))

let host: HTMLDivElement
let frame: HTMLIFrameElement
let config: CreateWidgetOptions
let options: VisualizeRuntimeOptions
let widget: Pick<WidgetHandle, 'ready' | 'iframe' | 'write' | 'end' | 'inspect' | 'screenshot' | 'dispose' | 'setTheme' | 'setContext'>

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => window.setTimeout(() => callback(0), 16))
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => window.clearTimeout(id))
  host = document.createElement('div')
  frame = document.createElement('iframe')
  document.body.append(host)
  options = { html: '<button>Explore</button>', theme: 'light', initialState: { value: 3 }, onPrompt: vi.fn(), onState: vi.fn(), onContext: vi.fn(() => true), onError: vi.fn(), onResize: vi.fn(), onDismiss: vi.fn(() => 'inline' as const), onPointerDown: vi.fn() }
  driver.decode.mockResolvedValue(new Blob(['validated pixels'], { type: 'image/png' }))
  widget = {
    ready: Promise.resolve(), iframe: frame, write: vi.fn(),
    end: vi.fn(async (): Promise<EndResult> => ({ blank: false, errorCount: 0, size: { width: 640, height: 450 } })),
    inspect: vi.fn(async () => ({ kind: 'html' as const, ended: true, code: options.html, blank: false, errors: [], console: [], size: { width: 640, height: 450 } })),
    screenshot: vi.fn(async () => ({ dataUrl: 'data:image/png;base64,cG5n', width: 640, height: 450 })),
    dispose: vi.fn(() => frame.remove()), setTheme: vi.fn(), setContext: vi.fn(),
  }
  driver.create.mockImplementation((value: CreateWidgetOptions) => { config = value; host.append(frame); return widget })
})

afterEach(() => {
  document.body.replaceChildren()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('generative-frame host adapter', () => {
  it('copies host tokens into the opaque frame and keeps historical full HTML intact', async () => {
    host.style.setProperty('--accent', 'rgb(120, 60, 20)')
    host.style.setProperty('--text', 'rgb(30, 40, 50)')
    host.style.setProperty('--font-sans', 'Example Sans')
    host.style.fontSize = '18px'
    options.html = '<!doctype html><html><head><style>.chart{padding:20px}</style></head><body><button>Explore</button></body></html>'
    const runtime = await mountVisualize(host, options)
    expect(config.tokens?.variables).toMatchObject({
      '--color-accent': 'rgb(120, 60, 20)', '--color-text': 'rgb(30, 40, 50)',
      '--font-sans': 'Example Sans', '--opensquilla-visualize-font-size': '18px',
    })
    expect(config.tokens?.variables['--color-border']).toBeTruthy()
    expect(vi.mocked(widget.write).mock.calls[0]![0]).toContain(options.html)
    expect(config.initialState).toEqual({ value: 3 })
    host.style.setProperty('--accent', 'rgb(220, 130, 70)')
    runtime.updateTheme('dark')
    expect(widget.setTheme).toHaveBeenCalledWith(expect.objectContaining({
      colorScheme: 'dark', variables: expect.objectContaining({ '--color-accent': 'rgb(220, 130, 70)' }),
    }))
    expect(driver.create).toHaveBeenCalledTimes(1)
    runtime.dispose()
  })

  it('keeps external navigation, networking, and tool calls disabled', async () => {
    const runtime = await mountVisualize(host, options)
    expect(config.opaqueOrigin).toBe(true)
    expect(config.csp).toEqual({ cdnOrigins: [], connectOrigins: [], imageOrigins: [], frameOrigins: [] })
    expect(() => config.onOpenLink?.('https://example.com')).toThrow('External links')
    expect(() => config.onCallTool?.({ name: 'write_file' })).toThrow('Direct tool calls')
    runtime.dispose()
  })

  it('changes actual height and overflow without recreating the iframe or resetting state', async () => {
    const runtime = await mountVisualize(host, options)
    expect(frame.style.height).toBe('450px')
    runtime.setPresentation({ mode: 'expanded', availableHeight: 300 })
    expect(frame.style.height).toBe('300px')
    expect(widget.setContext).toHaveBeenLastCalledWith({
      displayMode: 'fullscreen', containerDimensions: { maxHeight: 300 },
      styles: { variables: expect.objectContaining({ '--opensquilla-visualize-overflow': 'auto', '--color-text': config.tokens!.variables['--color-text'] }) },
    })
    runtime.setPresentation({ mode: 'inline' })
    expect(frame.style.height).toBe('450px')
    expect(widget.setContext).toHaveBeenLastCalledWith({
      displayMode: 'inline', containerDimensions: { maxHeight: 1200 },
      styles: { variables: expect.objectContaining({ '--opensquilla-visualize-overflow': 'hidden', '--color-text': config.tokens!.variables['--color-text'] }) },
    })
    expect(driver.create).toHaveBeenCalledTimes(1)
    expect(widget.write).toHaveBeenCalledTimes(1)
    expect(widget.dispose).not.toHaveBeenCalled()
    runtime.dispose()
  })

  it('only accepts guest dismissal while expanded and ignores requests after disposal', async () => {
    const runtime = await mountVisualize(host, options)
    expect(config.onRequestDisplayMode?.({ mode: 'fullscreen' })).toEqual({ mode: 'inline' })
    expect(config.onRequestDisplayMode?.({ mode: 'inline' })).toEqual({ mode: 'inline' })
    expect(options.onDismiss).not.toHaveBeenCalled()
    runtime.setPresentation({ mode: 'expanded', availableHeight: 500 })
    expect(config.onRequestDisplayMode?.({ mode: 'pip' })).toEqual({ mode: 'fullscreen' })
    expect(options.onDismiss).not.toHaveBeenCalled()
    vi.mocked(options.onDismiss!).mockReturnValueOnce('fullscreen') // only the menu closed
    expect(config.onRequestDisplayMode?.({ mode: 'inline' })).toEqual({ mode: 'fullscreen' })
    expect(config.onRequestDisplayMode?.({ mode: 'inline' })).toEqual({ mode: 'inline' })
    expect(options.onDismiss).toHaveBeenCalledTimes(2)
    runtime.dispose()
    config.onRequestDisplayMode?.({ mode: 'inline' })
    expect(options.onDismiss).toHaveBeenCalledTimes(2)
  })

  it('keeps the reserved pointer signal distinct from context without forwarding to the model', async () => {
    const runtime = await mountVisualize(host, options)
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:pointerdown:v1', text: 'send this' })).toThrow('Unsupported')
    expect(() => config.onUpdateModelContext?.({ structuredContent: { ignored: true } })).toThrow('Unsupported')
    expect(options.onPointerDown).not.toHaveBeenCalled()
    config.onUpdateModelContext?.({ type: 'opensquilla:pointerdown:v1' })
    expect(options.onPointerDown).toHaveBeenCalledTimes(1)
    expect(options.onPrompt).not.toHaveBeenCalled()
    expect(options.onState).not.toHaveBeenCalled()
    runtime.dispose()
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:pointerdown:v1' })).toThrow('disposed')
    expect(options.onPointerDown).toHaveBeenCalledTimes(1)
  })

  it('acknowledges bounded condition updates and clear only in the current host session', async () => {
    const runtime = await mountVisualize(host, options)
    const context = { summary: '4 nights, total difference 600', data: { nights: 4, prices: [500, 350] }, suggestedPrompt: 'Compare these options' }
    expect(config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context })).toEqual({ accepted: true })
    expect(options.onContext).toHaveBeenCalledExactlyOnceWith(context)
    expect(options.onPrompt).not.toHaveBeenCalled()
    expect(options.onState).not.toHaveBeenCalled()
    expect(config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context: null })).toEqual({ accepted: true })
    expect(options.onContext).toHaveBeenLastCalledWith(null)
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context, send: true })).toThrow('Unsupported')
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context: { summary: '', data: {} } })).toThrow('summary')
    vi.mocked(options.onContext!).mockReturnValue(false)
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context })).toThrow('session')
    runtime.dispose()
    expect(() => config.onUpdateModelContext?.({ type: 'opensquilla:context:v1', context })).toThrow('disposed')
  })

  it('bridges unhandled guest Escape after dispatch and respects generated controls', async () => {
    const runtime = await mountVisualize(host, options)
    const requestDisplayMode = vi.fn().mockResolvedValue({ mode: 'inline' })
    const updateModelContext = vi.fn().mockResolvedValue({})
    const guest = Object.assign(new EventTarget(), { opensquillaVisualize: undefined as unknown as { setContext(value: unknown): Promise<unknown> }, genframe: { context: { displayMode: 'inline' }, requestDisplayMode, updateModelContext } })
    const source = vi.mocked(widget.write).mock.calls[0]![0]
    const script = new DOMParser().parseFromString(source, 'text/html').querySelector('script')!.textContent!
    // Execute the exact guest bridge that is sent to the isolated document.
    new Function('window', script)(guest)
    await guest.opensquillaVisualize.setContext({ summary: 'current selection' })
    expect(updateModelContext).toHaveBeenLastCalledWith({ type: 'opensquilla:context:v1', context: { summary: 'current selection' } })
    updateModelContext.mockClear()
    guest.dispatchEvent(new Event('pointerdown'))
    expect(updateModelContext).toHaveBeenCalledExactlyOnceWith({ type: 'opensquilla:pointerdown:v1' })
    guest.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
    await vi.advanceTimersByTimeAsync(0)
    expect(requestDisplayMode).not.toHaveBeenCalled()
    guest.genframe.context.displayMode = 'fullscreen'
    guest.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', isComposing: true, cancelable: true }))
    await vi.advanceTimersByTimeAsync(0)
    expect(requestDisplayMode).not.toHaveBeenCalled()
    guest.addEventListener('keydown', event => event.preventDefault(), { once: true })
    guest.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
    await vi.advanceTimersByTimeAsync(0)
    expect(requestDisplayMode).not.toHaveBeenCalled()
    guest.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
    expect(requestDisplayMode).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(0)
    expect(requestDisplayMode).toHaveBeenCalledExactlyOnceWith('inline')
    guest.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
    guest.genframe.context.displayMode = 'inline'
    await vi.advanceTimersByTimeAsync(0)
    expect(requestDisplayMode).toHaveBeenCalledTimes(1)
    runtime.dispose()
  })

  it('coalesces content resize and reapplies the viewport bound after upstream writes its static height', async () => {
    const runtime = await mountVisualize(host, options)
    runtime.setPresentation({ mode: 'expanded', availableHeight: 380 })
    frame.style.height = '1200px' // upstream applySize runs before onResize
    config.onResize?.({ width: 600, height: 1400 })
    config.onResize?.({ width: 600, height: 1600 })
    await vi.advanceTimersByTimeAsync(16)
    expect(frame.style.height).toBe('380px')
    expect(options.onResize).toHaveBeenCalledExactlyOnceWith({ width: 600, height: 1600 })
    runtime.setPresentation({ mode: 'inline' })
    expect(frame.style.height).toBe('1200px')
    config.onResize?.({ width: 600, height: Number.NaN })
    await vi.advanceTimersByTimeAsync(16)
    expect(options.onResize).toHaveBeenCalledTimes(1)
    runtime.dispose()
  })

  it('retains the full theme and overflow across theme and presentation changes', async () => {
    const runtime = await mountVisualize(host, options)
    runtime.setPresentation({ mode: 'expanded', availableHeight: 300 })
    host.style.setProperty('--accent', 'rgb(210, 120, 60)')
    runtime.updateTheme('dark')
    expect(widget.setTheme).toHaveBeenLastCalledWith(expect.objectContaining({ variables: expect.objectContaining({
      '--color-accent': 'rgb(210, 120, 60)', '--opensquilla-visualize-overflow': 'auto',
    }) }))
    runtime.setPresentation({ mode: 'inline' })
    expect(widget.setContext).toHaveBeenLastCalledWith(expect.objectContaining({ styles: { variables: expect.objectContaining({
      '--color-accent': 'rgb(210, 120, 60)', '--opensquilla-visualize-overflow': 'hidden',
    }) } }))
    runtime.dispose()
  })

  it('ignores callbacks and cancels scheduled layout after disposal', async () => {
    const runtime = await mountVisualize(host, options)
    config.onResize?.({ width: 600, height: 800 })
    runtime.dispose()
    runtime.dispose()
    config.onPrompt?.('stale prompt')
    config.onWidgetState?.({ value: 999 })
    config.onError?.({ kind: 'script', message: 'stale error' })
    runtime.updateTheme('dark')
    runtime.setPresentation({ mode: 'expanded', availableHeight: 400 })
    await vi.advanceTimersByTimeAsync(16)
    expect(widget.dispose).toHaveBeenCalledTimes(1)
    expect(options.onPrompt).not.toHaveBeenCalled()
    expect(options.onState).not.toHaveBeenCalled()
    expect(options.onError).not.toHaveBeenCalled()
    expect(options.onResize).not.toHaveBeenCalled()
    expect(widget.setTheme).not.toHaveBeenCalled()
  })

  it('rejects a blank or script-failed render instead of reporting mount success', async () => {
    vi.mocked(widget.end).mockResolvedValueOnce({ blank: true, errorCount: 0, size: { width: 600, height: 0 } })
    await expect(mountVisualize(host, options)).rejects.toThrow('no visible content')
    vi.mocked(widget.end).mockImplementationOnce(async () => {
      config.onError?.({ kind: 'script', message: 'missing renderChart' })
      return { blank: false, errorCount: 1, size: { width: 600, height: 400 } }
    })
    await expect(mountVisualize(host, options)).rejects.toThrow('missing renderChart')
    expect(widget.dispose).toHaveBeenCalledTimes(2)
    expect(widget.inspect).not.toHaveBeenCalled()
  })

  it('captures the current pixels without rerendering or changing state', async () => {
    const runtime = await mountVisualize(host, options)
    const blob = await runtime.capture()
    expect(blob.type).toBe('image/png')
    expect(widget.screenshot).toHaveBeenCalledExactlyOnceWith({ scale: 1 })
    expect(driver.decode).toHaveBeenCalledWith(expect.objectContaining({ dataUrl: 'data:image/png;base64,cG5n' }), expect.any(AbortSignal), document)
    expect(driver.create).toHaveBeenCalledTimes(1)
    expect(widget.write).toHaveBeenCalledTimes(1)
    expect(widget.end).toHaveBeenCalledTimes(1)
    expect(options.onState).not.toHaveBeenCalled()
    expect(widget.dispose).not.toHaveBeenCalled()
    runtime.dispose()
  })

  it('rejects unready and oversized captures before requesting rasterization', async () => {
    const runtime = await mountVisualize(host, options)
    vi.mocked(widget.inspect).mockResolvedValueOnce({ kind: 'html', ended: true, code: '', blank: true, errors: [], console: [], size: { width: 640, height: 450 } })
    await expect(runtime.capture()).rejects.toMatchObject({ kind: 'unavailable' })
    vi.mocked(widget.inspect).mockResolvedValueOnce({ kind: 'html', ended: true, code: '', blank: false, errors: [], console: [], size: { width: 640, height: 30_000 } })
    await expect(runtime.capture()).rejects.toMatchObject({ kind: 'tooLarge' })
    expect(widget.screenshot).not.toHaveBeenCalled()
    runtime.dispose()
  })

  it.each(['cancel', 'dispose'])('discards a late screenshot after %s', async action => {
    const runtime = await mountVisualize(host, options)
    const controller = new AbortController()
    let finish!: (value: Awaited<ReturnType<WidgetHandle['screenshot']>>) => void
    vi.mocked(widget.screenshot).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const pending = runtime.capture({ signal: controller.signal })
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(0)
    if (action === 'cancel') controller.abort(); else runtime.dispose()
    await rejected
    finish({ dataUrl: 'data:image/png;base64,bGF0ZQ==', width: 640, height: 450 })
    await vi.advanceTimersByTimeAsync(0)
    expect(driver.decode).not.toHaveBeenCalled()
    runtime.dispose()
  })

  it('bounds capture duration and rejects concurrent exports without disposing the usable canvas', async () => {
    const runtime = await mountVisualize(host, options)
    vi.mocked(widget.screenshot).mockImplementationOnce(() => new Promise(() => {}))
    const pending = runtime.capture()
    const rejected = expect(pending).rejects.toMatchObject({ kind: 'timedOut' })
    await expect(runtime.capture()).rejects.toThrow('already in progress')
    await vi.advanceTimersByTimeAsync(15_000)
    await rejected
    expect(widget.dispose).not.toHaveBeenCalled()
    config.onPrompt?.('still usable')
    expect(options.onPrompt).toHaveBeenCalledWith('still usable')
    runtime.dispose()
  })

  it('propagates invalid PNG or opaque-canvas failures without claiming export success', async () => {
    const runtime = await mountVisualize(host, options)
    vi.mocked(widget.screenshot).mockRejectedValueOnce(new DOMException('The canvas is tainted', 'SecurityError'))
    await expect(runtime.capture()).rejects.toMatchObject({ name: 'SecurityError' })
    driver.decode.mockRejectedValueOnce(new VisualizeCaptureError('blank'))
    await expect(runtime.capture()).rejects.toMatchObject({ kind: 'blank' })
    runtime.dispose()
    await expect(runtime.capture()).rejects.toMatchObject({ name: 'AbortError' })
  })

  function visibleHost() {
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ width: 640, height: 120 } as DOMRect)
    vi.spyOn(frame, 'getBoundingClientRect').mockReturnValue({ width: 640, height: 120 } as DOMRect)
  }

  it('re-measures an unlaid-out guest after host layout without rendering the code twice', async () => {
    visibleHost()
    vi.mocked(widget.end).mockResolvedValue({ blank: true, errorCount: 0, size: { width: 0, height: 0 } })
    const mounting = mountVisualize(host, options)
    await vi.advanceTimersByTimeAsync(0)
    expect(widget.inspect).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(16)
    const runtime = await mounting
    expect(widget.inspect).toHaveBeenCalledTimes(1)
    expect(driver.create).toHaveBeenCalledTimes(1)
    expect(widget.write).toHaveBeenCalledTimes(1)
    expect(widget.end).toHaveBeenCalledTimes(1)
    expect(frame.style.height).toBe('450px')
    expect(config.initialState).toEqual({ value: 3 })
    runtime.dispose()
  })

  it('still rejects content that remains blank after the one layout inspection', async () => {
    visibleHost()
    vi.mocked(widget.end).mockResolvedValue({ blank: true, errorCount: 0, size: { width: 0, height: 0 } })
    vi.mocked(widget.inspect).mockResolvedValue({ kind: 'html', ended: true, code: '', blank: true, errors: [], console: [], size: { width: 640, height: 16 } })
    const rejected = expect(mountVisualize(host, options)).rejects.toThrow('no visible content')
    await vi.advanceTimersByTimeAsync(16)
    await rejected
    expect(widget.inspect).toHaveBeenCalledTimes(1)
    expect(widget.dispose).toHaveBeenCalledTimes(1)
  })

  it('does not re-inspect a laid-out blank document', async () => {
    visibleHost()
    vi.mocked(widget.end).mockResolvedValue({ blank: true, errorCount: 0, size: { width: 640, height: 16 } })
    await expect(mountVisualize(host, options)).rejects.toThrow('no visible content')
    expect(widget.inspect).not.toHaveBeenCalled()
  })

  it('cancels a pending layout barrier without inspecting a disposed guest', async () => {
    visibleHost()
    const controller = new AbortController()
    vi.mocked(widget.end).mockResolvedValue({ blank: true, errorCount: 0, size: { width: 0, height: 0 } })
    const rejected = expect(mountVisualize(host, { ...options, signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()
    await rejected
    await vi.advanceTimersByTimeAsync(16)
    expect(widget.inspect).not.toHaveBeenCalled()
    expect(widget.dispose).toHaveBeenCalledTimes(1)
  })

  it('shares the original render deadline with layout and inspection', async () => {
    visibleHost()
    vi.mocked(widget.end).mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({ blank: true, errorCount: 0, size: { width: 0, height: 0 } }), 14_000)))
    vi.mocked(widget.inspect).mockImplementation(() => new Promise(() => {}))
    const rejected = expect(mountVisualize(host, options)).rejects.toThrow('render timed out')
    await vi.advanceTimersByTimeAsync(14_016)
    expect(widget.inspect).toHaveBeenCalledTimes(1)
    expect(widget.dispose).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(984)
    await rejected
    expect(widget.dispose).toHaveBeenCalledTimes(1)
  })

  it('bounds a render that never finishes and disposes its frame', async () => {
    vi.mocked(widget.end).mockImplementation(() => new Promise(() => {}))
    const mounting = mountVisualize(host, options)
    const rejected = expect(mounting).rejects.toThrow('render timed out')
    await vi.advanceTimersByTimeAsync(15_000)
    await rejected
    expect(widget.dispose).toHaveBeenCalledTimes(1)
  })

  it('does not write generated code when the frame never connects', async () => {
    widget = { ...widget, ready: new Promise(() => {}) }
    const mounting = mountVisualize(host, options)
    const rejected = expect(mounting).rejects.toThrow('connection timed out')
    await vi.advanceTimersByTimeAsync(15_000)
    await rejected
    expect(widget.write).not.toHaveBeenCalled()
    expect(widget.dispose).toHaveBeenCalledTimes(1)
  })

  it('cancels during render promptly even when the upstream promise does not settle', async () => {
    const controller = new AbortController()
    vi.mocked(widget.end).mockImplementation(() => new Promise(() => {}))
    const mounting = mountVisualize(host, { ...options, signal: controller.signal })
    const rejected = expect(mounting).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()
    await rejected
    expect(widget.dispose).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(widget.dispose).toHaveBeenCalledTimes(1)
    expect(options.onError).not.toHaveBeenCalled()
  })
})
