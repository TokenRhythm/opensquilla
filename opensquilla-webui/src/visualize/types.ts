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
}
export interface VisualizeRuntime {
  dispose(): void
  updateTheme(theme: 'light' | 'dark'): void
}
export type VisualizeMountOptions = VisualizeRuntimeOptions
export interface VisualizeConversation {
  enabled(sessionKey: string): boolean
  draft(sessionKey: string, receipt: VisualizeReceipt, text: string): boolean
}
export const VISUALIZE_CONVERSATION_KEY: InjectionKey<VisualizeConversation> = Symbol('VisualizeConversation')
