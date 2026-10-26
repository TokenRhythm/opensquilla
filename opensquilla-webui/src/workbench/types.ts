import type { Component } from 'vue'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchSurfaceEvent,
} from '@/platform/types'
import type { IconName } from '@/utils/icons'

export type WorkbenchScope =
  | { type: 'session'; id: string }
  | { type: 'workspace'; id: string }
  | { type: 'app' }

export type WorkbenchHostKind = 'dom' | 'native-webcontents'

export type WorkbenchPanelKind =
  | 'artifact-preview'
  | 'resource-collection'
  | 'browser'
  | 'frontend-preview'
  | 'file'
  | 'workspace-files'
  | 'tool-file-history'
  | 'diff'
  | 'terminal'
  | 'assistant-chat'

export type WorkbenchRetention = 'dispose-on-suspend' | 'keep-alive'

/**
 * A serializable panel descriptor. Runtime-only state (DOM nodes, Blob URLs,
 * AbortControllers, WebContents handles, credentials) belongs in a panel
 * runtime and must never be put in this object.
 */
export interface WorkbenchItem {
  id: string
  kind: WorkbenchPanelKind
  title: string
  scope: WorkbenchScope
  hostKind: WorkbenchHostKind
  retention: WorkbenchRetention
  payload: Readonly<Record<string, unknown>>
}

export type WorkbenchDisposeReason =
  | 'closed'
  | 'scope-changed'
  | 'store-reset'
  | 'runtime-detached'
  | 'suspended'

export type WorkbenchLifecycleEvent =
  | { type: 'open'; item: WorkbenchItem }
  | { type: 'update'; item: WorkbenchItem }
  | { type: 'activate'; item: WorkbenchItem }
  | { type: 'resume'; item: WorkbenchItem }
  | { type: 'suspend'; item: WorkbenchItem }
  | { type: 'dispose'; item: WorkbenchItem; reason: WorkbenchDisposeReason }

export type WorkbenchLifecycleListener = (event: WorkbenchLifecycleEvent) => void

export interface WorkbenchComponentEvent {
  type: string
  payload?: unknown
}

export interface WorkbenchPanelHeader {
  title: string
  subtitle?: string
  icon?: IconName
}

export interface WorkbenchToolbarSelectOption {
  value: string
  label: string
  actionId: string
  disabled?: boolean
}

export type WorkbenchToolbarItem =
  | {
      kind: 'action'
      id: string
      label: string
      icon: IconName
      tooltip?: string
      disabled?: boolean
      pressed?: boolean
    }
  | {
      kind: 'status'
      id: string
      label: string
      text: string
      icon?: IconName
      tone?: 'neutral' | 'warning'
    }
  | {
      kind: 'select'
      id: string
      label: string
      value: string
      options: readonly WorkbenchToolbarSelectOption[]
      actionGroupLabel?: string
      actionOptions?: readonly WorkbenchToolbarSelectOption[]
    }

export interface WorkbenchRuntimeContext {
  nativeWorkbenchApi?: NativeWorkbenchApi
  getRenderState(): Readonly<Record<string, unknown>>
  updateRenderState(patch: Readonly<Record<string, unknown>>): void
  /** Small, JSON-only reading state. Survives renderer release, not tab close. */
  getViewState?(): Readonly<Record<string, unknown>>
  saveViewState?(state: Readonly<Record<string, unknown>>): void
  /**
   * Admit asynchronous foreground resource reads without blocking lifecycle
   * cancellation. Providers still abort running reads on suspend/dispose.
   * Call from activate/resume, not from an awaited createRuntime factory.
   */
  runRestore?(task: () => Promise<void>): Promise<void>
  isItemOpen(): boolean
  isItemActive?(): boolean
  setExpanded(expanded: boolean): void
  reportError(error: unknown): void
}

export interface WorkbenchBeforeCloseOptions {
  /** Flush pending work without releasing resources while the panel remains open. */
  preserveRuntime?: boolean
}

export interface WorkbenchPanelRuntime {
  /** Persist pending user work before the owning descriptor is removed. */
  beforeClose?(options?: WorkbenchBeforeCloseOptions): boolean | Promise<boolean>
  setComponentHandle?(handle: unknown): void | Promise<void>
  handleComponentEvent?(
    event: WorkbenchComponentEvent,
    item: WorkbenchItem,
  ): void | Promise<void>
  handleSurfaceRect?(rect: NativeSurfaceRect, item: WorkbenchItem): void | Promise<void>
  handleNativeSurfaceEvent?(
    event: NativeWorkbenchSurfaceEvent,
    item: WorkbenchItem,
  ): void | Promise<void>
  performAction?(actionId: string, item: WorkbenchItem): void | Promise<void>
  update?(item: WorkbenchItem): void | Promise<void>
  activate?(item: WorkbenchItem): void | Promise<void>
  resume?(item: WorkbenchItem): void | Promise<void>
  suspend?(item: WorkbenchItem): void | Promise<void>
  dispose?(reason: WorkbenchDisposeReason): void | Promise<void>
}

export interface WorkbenchPanelRenderState {
  active: boolean
  /** Selection is retained while collapsed; visible also reflects host presence. */
  visible?: boolean
  hostAvailable: boolean
  nativeSurface: boolean
  runtimeState: Readonly<Record<string, unknown>>
}

export interface WorkbenchPanelDefinition {
  kind: WorkbenchPanelKind
  component?: Component
  supports?(item: WorkbenchItem): boolean
  /** Check a business owner without recreating a released renderer. */
  beforeClose?(item: WorkbenchItem, options?: WorkbenchBeforeCloseOptions): boolean | Promise<boolean>
  /**
   * Produces ephemeral component inputs at render time. Credentials, Blob URLs
   * and runtime handles may be read here but must not be written to the item.
   */
  getProps?(
    item: WorkbenchItem,
    state: WorkbenchPanelRenderState,
  ): Readonly<Record<string, unknown>>
  getHeader?(
    item: WorkbenchItem,
    state: WorkbenchPanelRenderState,
  ): WorkbenchPanelHeader
  getToolbarItems?(
    item: WorkbenchItem,
    state: WorkbenchPanelRenderState,
  ): readonly WorkbenchToolbarItem[]
  createRuntime?(
    item: WorkbenchItem,
    context: WorkbenchRuntimeContext,
  ): WorkbenchPanelRuntime | Promise<WorkbenchPanelRuntime>
}

export interface NativeSurfaceRect {
  itemId: string
  x: number
  y: number
  width: number
  height: number
  visible: boolean
}
