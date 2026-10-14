import type { InjectionKey } from 'vue'

export interface VisualizeReceipt {
  callId: string
  /** Stable identity shared by show_widget and later edit_widget receipts. */
  widgetId?: string
  /** `show` publishes the initial code; `edit` advances the same widget. */
  action?: 'show' | 'edit'
  /** Monotonic widget code version, independent of the Artifact revision id. */
  version?: number
  /** Internal canvas artifacts are persisted for recovery but are not Files entries. */
  visibility?: 'internal' | 'user'
  documentId: string
  revisionId: string
  artifactId: string
  name: string
  mime: string
  sha256: string
  size: number
  marker: string
}
export type VisualizeJson = null | boolean | number | string | VisualizeJson[] | { [key: string]: VisualizeJson }
export interface VisualizeContext {
  summary: string
  data?: VisualizeJson
  suggestedPrompt?: string
}
export interface VisualizeRuntimeOptions {
  signal?: AbortSignal
  html: string
  initialState: unknown
  theme: 'light' | 'dark'
  onPrompt(text: string): void
  onState(state: unknown): void
  /** Host-only context update. False means its owning session is no longer current. */
  onContext?(context: VisualizeContext | null): boolean
  onError(error: unknown): void
  /** Dismiss host UI in order and return the actual remaining display mode. */
  onDismiss?(): 'inline' | 'fullscreen'
  /** A pointer entered the guest; only dismiss the owning host menu. */
  onPointerDown?(): void
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
  /** Capture the current rendered state without rerunning generated code. */
  capture(options?: { signal?: AbortSignal }): Promise<Blob>
  updateTheme(theme: 'light' | 'dark'): void
  setPresentation(presentation: VisualizePresentation): void
}
export type VisualizeMountOptions = VisualizeRuntimeOptions
export interface VisualizeConversation {
  enabled(sessionKey: string): boolean
  draft(sessionKey: string, receipt: VisualizeReceipt, text: string): boolean
  /** Explicit host click attaches conditions; guest updates never invoke it. */
  attach(sessionKey: string, receipt: VisualizeReceipt, context: VisualizeContext): boolean
}
export const VISUALIZE_CONVERSATION_KEY: InjectionKey<VisualizeConversation> = Symbol('VisualizeConversation')
