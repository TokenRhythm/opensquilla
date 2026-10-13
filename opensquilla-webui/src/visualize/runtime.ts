import { createWidget, defaultThemeTokens } from 'generative-frame'
import type { VisualizeRuntime, VisualizeRuntimeOptions } from './types'

export const VISUALIZE_RUNTIME_LABEL = 'A · generative-frame 0.0.5'
const bridge = `<script>window.opensquillaVisualize={ready:Promise.resolve(),getState:()=>window.genframe.state,setState:value=>window.genframe.setState(value),sendPrompt:text=>window.genframe.sendPrompt(String(text))};</script>`

export async function mountVisualize(container: HTMLElement, options: VisualizeRuntimeOptions): Promise<VisualizeRuntime> {
  if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  const widget = createWidget({
    container,
    opaqueOrigin: true,
    tokens: defaultThemeTokens(options.theme),
    csp: { cdnOrigins: [], connectOrigins: [], imageOrigins: [], frameOrigins: [] },
    initialState: options.initialState,
    minHeight: 120,
    maxHeight: 1200,
    animate: false,
    // genframe measures #gf-root, not body padding. Keep all spacing inside
    // its measured root so complete HTML documents cannot clip their controls.
    css: 'body{margin:0!important;padding:0!important}#gf-root{display:flow-root;padding:16px;box-sizing:border-box}',
    onPrompt: options.onPrompt,
    onWidgetState: options.onState,
    onError: options.onError,
    onOpenLink: () => { throw new Error('External links are unavailable in this canvas') },
    onCallTool: () => { throw new Error('Direct tool calls are unavailable in this canvas') },
  })
  const abort = () => widget.dispose()
  options.signal?.addEventListener('abort', abort, { once: true })
  try {
    await widget.ready
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    widget.write(bridge + options.html)
    const result = await widget.end()
    if (result.blank) throw new Error('The canvas produced no visible content')
  } catch (error) { options.signal?.removeEventListener('abort', abort); widget.dispose(); throw error }
  return { dispose: () => { options.signal?.removeEventListener('abort', abort); widget.dispose() }, updateTheme: theme => widget.setTheme(defaultThemeTokens(theme)) }
}
