import type { InjectionKey } from 'vue'

export interface VisualizeReceipt {
  callId: string
  documentId: string
  revisionId: string
  artifactId: string
  name: string
  mime: string
  sha256: string
  size: number
  marker: string
}
export interface VisualizeRuntimeOptions {
  signal?: AbortSignal
  html: string
  initialState: unknown
  theme: 'light' | 'dark'
  onPrompt(text: string): void
  onState(state: unknown): void
  onError(error: unknown): void
  /** Unclamped content size, before the host applies the presentation limit. */
  onResize?(size: VisualizeSize): void
}
export interface VisualizeSize { width: number; height: number }
export interface VisualizePresentation {
  mode: 'inline' | 'expanded'
  /** Space available to the iframe, excluding host controls and safe areas. */
  availableHeight?: number
}
export interface VisualizeRuntime {
  dispose(): void
  updateTheme(theme: 'light' | 'dark'): void
  setPresentation(presentation: VisualizePresentation): void
}
export type VisualizeMountOptions = VisualizeRuntimeOptions
export interface VisualizeConversation {
  enabled(sessionKey: string): boolean
  draft(sessionKey: string, receipt: VisualizeReceipt, text: string): boolean
}
export const VISUALIZE_CONVERSATION_KEY: InjectionKey<VisualizeConversation> = Symbol('VisualizeConversation')
