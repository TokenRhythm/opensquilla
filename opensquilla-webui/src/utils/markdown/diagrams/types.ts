export interface DiagramPalette {
  background: string
  text: string
  muted: string
  border: string
  accent: string
  dark: boolean
}

export type DiagramErrorCode = 'budget' | 'config' | 'timeout' | 'render' | 'busy'

export class DiagramRenderError extends Error {
  constructor(readonly code: DiagramErrorCode) {
    super(`Diagram rendering failed: ${code}`)
    this.name = 'DiagramRenderError'
  }
}

export function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException('Diagram rendering cancelled', 'AbortError')
}
