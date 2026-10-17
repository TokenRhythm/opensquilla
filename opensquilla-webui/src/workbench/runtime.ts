import { reactive, toRaw } from 'vue'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceResult,
} from '@/platform/types'
import type { useWorkbenchStore } from './store'
import type {
  NativeSurfaceRect,
  WorkbenchBeforeCloseOptions,
  WorkbenchComponentEvent,
  WorkbenchDisposeReason,
  WorkbenchItem,
  WorkbenchLifecycleEvent,
  WorkbenchPanelDefinition,
  WorkbenchPanelKind,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
} from './types'

export class WorkbenchPanelRegistry {
  private readonly definitions = new Map<WorkbenchPanelKind, WorkbenchPanelDefinition>()

  register(
    definition: WorkbenchPanelDefinition,
    options: { replace?: boolean } = {},
  ): () => void {
    if (this.definitions.has(definition.kind) && options.replace !== true) {
      throw new Error(`Workbench panel "${definition.kind}" is already registered`)
    }
    this.definitions.set(definition.kind, definition)
    return () => {
      if (this.definitions.get(definition.kind) === definition) {
        this.definitions.delete(definition.kind)
      }
    }
  }

  resolve(item: WorkbenchItem): WorkbenchPanelDefinition | null {
    const definition = this.definitions.get(item.kind)
    if (!definition || (definition.supports && !definition.supports(item))) return null
    return definition
  }

  clear() {
    this.definitions.clear()
  }
}

export interface WorkbenchRuntimeManagerOptions {
  nativeWorkbenchApi?: NativeWorkbenchApi
  setExpanded?: (expanded: boolean) => void
  onError?: (error: unknown, item: WorkbenchItem) => void
  /** Limit migrated runRestore reads; legacy constructors are independent. */
  maxConcurrentRestores?: number
}

interface RestoreRequest {
  key: string
  valid: () => boolean
  task: () => Promise<void>
  resolve: () => void
  reject: (error: unknown) => void
}

function copyViewState(state: Readonly<Record<string, unknown>>): Readonly<Record<string, unknown>> {
  let remainingNodes = 2048
  let remainingCharacters = 32 * 1024
  const copy = (value: unknown, depth: number): unknown => {
    if (--remainingNodes < 0 || depth > 8) throw new Error('Workbench view state is too large')
    if (value === null || typeof value === 'boolean') return value
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string') {
      remainingCharacters -= value.length
      if (remainingCharacters < 0) throw new Error('Workbench view state is too large')
      return value
    }
    if (Array.isArray(value)) return value.map(entry => copy(entry, depth + 1))
    if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
      return Object.fromEntries(Object.entries(value).map(([key, entry]) => {
        remainingCharacters -= key.length
        if (remainingCharacters < 0) throw new Error('Workbench view state is too large')
        return [key, copy(entry, depth + 1)]
      }))
    }
    throw new Error('Workbench view state must contain only JSON values')
  }
  return copy(state, 0) as Readonly<Record<string, unknown>>
}

/**
 * Owns all non-serializable panel resources. Events for each item are queued so
 * rapid tab switching cannot resume a runtime after a later close disposed it.
 * Browser navigations run independently so native progress and stop/close can
 * be processed while a page response is pending.
 */
export class WorkbenchRuntimeManager {
  private readonly runtimes = new Map<string, WorkbenchPanelRuntime>()
  private readonly items = new Map<string, WorkbenchItem>()
  private readonly itemEpochs = new Map<string, number>()
  private nextItemEpoch = 0
  private readonly runtimeGenerations = new Map<string, number>()
  private nextRuntimeGeneration = 0
  private readonly activationEpochs = new Map<string, number>()
  private readonly viewStates = new Map<string, Readonly<Record<string, unknown>>>()
  private readonly componentHandles = new Map<string, unknown>()
  private readonly restoreRequests: RestoreRequest[] = []
  private runningRestores = 0
  private readonly descriptorEpochs = new WeakMap<object, number>()
  private readonly openItemIds = new Set<string>()
  private readonly resumedItemIds = new Set<string>()
  private readonly queues = new Map<string, Promise<void>>()
  private readonly browserNavigations = new Map<string, Set<Promise<void>>>()
  private readonly surfaceRects = new Map<string, NativeSurfaceRect>()
  private readonly renderStates = reactive(
    new Map<string, Readonly<Record<string, unknown>>>(),
  )

  constructor(
    private readonly registry: WorkbenchPanelRegistry,
    private readonly options: WorkbenchRuntimeManagerOptions = {},
  ) {}

  handle(event: WorkbenchLifecycleEvent): void {
    const id = event.item.id
    let epoch = this.itemEpochs.get(id) ?? 0
    if (event.type === 'open' && !this.openItemIds.has(id)) {
      // Do not reuse an epoch after disposal deletes the item's bookkeeping:
      // browser navigation/recovery can still settle after a same-id reopen.
      epoch = ++this.nextItemEpoch
      this.itemEpochs.set(id, epoch)
      this.renderStates.delete(id)
      this.viewStates.delete(id)
      this.resumedItemIds.delete(id)
    }
    if (event.type === 'dispose') {
      this.openItemIds.delete(id)
      this.resumedItemIds.delete(id)
      this.activationEpochs.set(id, (this.activationEpochs.get(id) ?? 0) + 1)
      this.hideNativeSurfaceImmediately(event.item)
    } else {
      this.items.set(id, event.item)
      this.openItemIds.add(id)
      this.descriptorEpochs.set(toRaw(event.item), epoch)
    }
    if (event.type === 'resume') this.resumedItemIds.add(id)
    if (event.type === 'suspend') {
      this.resumedItemIds.delete(id)
      this.activationEpochs.set(id, (this.activationEpochs.get(id) ?? 0) + 1)
      this.hideNativeSurfaceImmediately(event.item)
    }
    const eventEpoch = epoch
    const activationEpoch = this.activationEpochs.get(id) ?? 0
    this.drainRestores()
    this.enqueue(id, async () => {
      if (event.type === 'open') {
        // A background descriptor is not permission to load a document. Live
        // owners retain their existing creation semantics until migrated.
        if (event.item.retention === 'keep-alive') await this.ensureRuntime(event.item, eventEpoch)
      } else if (event.type === 'update') {
        // Metadata changes must not recreate a suspended document's resources.
        // Its descriptor is already recorded above for the next activation.
        // Check inside the queue so an earlier suspend has finished disposing.
        if (
          event.item.retention === 'dispose-on-suspend'
          && !this.runtimes.has(id)
        ) return
        const runtime = await this.ensureRuntime(event.item, eventEpoch)
        await runtime?.update?.(event.item)
      } else if (event.type === 'activate') {
        if (!this.mayRestore(event.item, eventEpoch, activationEpoch)) return
        const runtime = await this.ensureRuntime(event.item, eventEpoch, activationEpoch)
        if (this.mayRestore(event.item, eventEpoch, activationEpoch)) await runtime?.activate?.(event.item)
      } else if (event.type === 'resume') {
        if (!this.mayRestore(event.item, eventEpoch, activationEpoch)) return
        const runtime = await this.ensureRuntime(event.item, eventEpoch, activationEpoch)
        if (this.mayRestore(event.item, eventEpoch, activationEpoch)) await runtime?.resume?.(event.item)
      } else if (event.type === 'suspend') {
        if (event.item.retention === 'dispose-on-suspend') {
          // Checkpoint while this generation still owns its state, then revoke
          // all of its callbacks before asynchronous disposal starts.
          await this.runtimes.get(id)?.suspend?.(event.item)
          await this.disposeRuntime(event.item.id, 'suspended')
        } else {
          await this.runtimes.get(event.item.id)?.suspend?.(event.item)
        }
      } else {
        await this.disposeRuntime(event.item.id, event.reason)
        if (
          this.itemEpochs.get(event.item.id) === eventEpoch
          && !this.openItemIds.has(event.item.id)
        ) {
          this.items.delete(event.item.id)
          this.itemEpochs.delete(event.item.id)
          this.surfaceRects.delete(event.item.id)
          this.renderStates.delete(event.item.id)
          this.viewStates.delete(event.item.id)
          this.componentHandles.delete(event.item.id)
          this.activationEpochs.delete(event.item.id)
        }
      }
    }, event.item)
  }

  setComponentHandle(item: WorkbenchItem, handle: unknown): void {
    if (!this.isCurrentDescriptor(item)) return
    if (handle === null || handle === undefined) this.componentHandles.delete(item.id)
    else if (item.retention === 'keep-alive' || this.resumedItemIds.has(item.id)) {
      this.componentHandles.set(item.id, handle)
    }
    const currentInput = this.inputGuard(item)
    this.enqueue(item.id, async () => {
      if (!currentInput()) return
      const runtime = this.runtimes.get(item.id)
      await runtime?.setComponentHandle?.(handle)
    }, item)
  }

  handleComponentEvent(item: WorkbenchItem, event: WorkbenchComponentEvent): void {
    if (!this.isCurrentDescriptor(item)) return
    const currentInput = this.inputGuard(item)
    this.enqueue(item.id, async () => {
      if (!currentInput()) return
      const runtime = this.runtimes.get(item.id)
      const result = runtime?.handleComponentEvent?.(event, item)
      const action = event.type === 'browser-action' && event.payload && typeof event.payload === 'object'
        ? String((event.payload as Record<string, unknown>).action) : ''
      if (item.kind === 'browser' && ['navigate', 'reload', 'back', 'forward', 'stop'].includes(action)) {
        this.trackBrowserNavigation(item, result)
      } else await result
    }, item)
  }

  handleSurfaceRect(rect: NativeSurfaceRect): void {
    this.surfaceRects.set(rect.itemId, rect)
    if (!rect.visible) {
      const item = this.items.get(rect.itemId)
      if (item) this.hideNativeSurfaceImmediately(item, rect)
    }
    const item = this.items.get(rect.itemId)
    if (!item || !this.openItemIds.has(item.id)) return
    const currentInput = this.inputGuard(item)
    const hadRuntime = this.runtimes.has(item.id)
    this.enqueue(item.id, async () => {
      // A newly created runtime already receives the latest stored rect.
      if (!hadRuntime || !currentInput()) return
      await this.runtimes.get(item.id)?.handleSurfaceRect?.(rect, item)
    }, item)
  }

  handleNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent): void {
    const item = this.items.get(event.surfaceId)
    if (!item || !this.openItemIds.has(item.id)) return
    if (event.type === 'error' || event.type === 'crashed') {
      this.hideNativeSurfaceImmediately(item)
    }
    const currentInput = this.inputGuard(item)
    this.enqueue(item.id, async () => {
      if (!currentInput()) return
      await this.runtimes.get(item.id)?.handleNativeSurfaceEvent?.(event, item)
    }, item)
  }

  performAction(item: WorkbenchItem, actionId: string): void {
    if (!this.isCurrentDescriptor(item)) return
    this.enqueue(item.id, async () => {
      const runtime = await this.ensureRuntime(item)
      const result = runtime?.performAction?.(actionId, item)
      if (item.kind === 'browser' && ['reload', 'stop'].includes(actionId)) {
        this.trackBrowserNavigation(item, result)
      } else await result
    }, item)
  }

  getRenderState(itemId: string): Readonly<Record<string, unknown>> {
    return this.renderStates.get(itemId) ?? {}
  }

  async flush(itemId?: string): Promise<void> {
    // Navigation may emit more lifecycle/state work while it settles. Include
    // both queues without making stop/loading/close wait for a page response.
    while (true) {
      const pending = itemId
        ? [this.queues.get(itemId), ...(this.browserNavigations.get(itemId) ?? [])].filter(Boolean)
        : [...this.queues.values(), ...[...this.browserNavigations.values()].flatMap(tasks => [...tasks])]
      if (!pending.length) return
      await Promise.all(pending)
    }
  }

  /** Ask the live panel to persist pending work before its store item is removed. */
  async beforeClose(
    item: WorkbenchItem,
    options?: WorkbenchBeforeCloseOptions,
  ): Promise<boolean> {
    if (!this.isCurrentDescriptor(item)) return true
    try {
      const checkOwner = this.registry.resolve(item)?.beforeClose
      if (checkOwner && await checkOwner(item, options) === false) return false
    } catch (error) {
      this.reportError(error, item)
      return false
    }
    // Browsers have no source edits to persist; navigation must not block closing a tab.
    if (item.kind === 'browser') return true
    if (item.retention === 'dispose-on-suspend' && !this.runtimes.has(item.id)) {
      this.activationEpochs.set(item.id, (this.activationEpochs.get(item.id) ?? 0) + 1)
      this.drainRestores()
      return true
    }
    await this.flush(item.id)
    // Renderer eviction must never create a renderer merely to close it.
    // Providers with background work keep an owner outside this cold instance.
    const runtime = this.runtimes.get(item.id)
    try {
      return await runtime?.beforeClose?.(options) ?? true
    } catch (error) {
      this.reportError(error, item)
      return false
    }
  }

  async disposeAll(reason: WorkbenchDisposeReason = 'runtime-detached'): Promise<void> {
    for (const item of this.items.values()) this.hideNativeSurfaceImmediately(item)
    this.openItemIds.clear()
    this.resumedItemIds.clear()
    this.drainRestores()
    // Destroy pages before waiting for navigation: disposal cancels native
    // loads, including requests that would otherwise hold shutdown open.
    await Promise.all([...this.queues.values()])
    const ids = [...this.runtimes.keys()]
    for (const id of ids) await this.disposeRuntime(id, reason)
    await this.flush()
    this.items.clear()
    this.itemEpochs.clear()
    this.runtimeGenerations.clear()
    this.activationEpochs.clear()
    this.viewStates.clear()
    this.componentHandles.clear()
    this.surfaceRects.clear()
    this.renderStates.clear()
  }

  hasRuntime(itemId: string): boolean {
    return this.runtimes.has(itemId)
  }

  /** Diagnostic counts for focused tests and resource measurements. */
  getRestoreActivity(): { running: number; queued: number } {
    return { running: this.runningRestores, queued: this.restoreRequests.length }
  }

  private mayRestore(item: WorkbenchItem, itemEpoch: number, activationEpoch: number): boolean {
    return this.openItemIds.has(item.id)
      && this.itemEpochs.get(item.id) === itemEpoch
      && (this.activationEpochs.get(item.id) ?? 0) === activationEpoch
      && (item.retention === 'keep-alive' || this.resumedItemIds.has(item.id))
  }

  private inputGuard(item: WorkbenchItem): () => boolean {
    const generation = this.runtimeGenerations.get(item.id)
    const itemEpoch = this.itemEpochs.get(item.id)
    const activationEpoch = this.activationEpochs.get(item.id) ?? 0
    const pendingCreation = generation === undefined
      && (item.retention === 'keep-alive' || this.resumedItemIds.has(item.id))
    return () => this.isCurrentDescriptor(item)
      && this.itemEpochs.get(item.id) === itemEpoch
      && (generation === undefined
        ? pendingCreation && this.runtimeGenerations.has(item.id)
          && (this.activationEpochs.get(item.id) ?? 0) === activationEpoch
        : this.runtimeGenerations.get(item.id) === generation)
  }

  private admitRestore(key: string, valid: () => boolean, task: () => Promise<void>): Promise<void> {
    if (!valid()) return Promise.resolve()
    // A waiting view can have only one pending read. New foreground intent
    // replaces obsolete queued work rather than accumulating a replay backlog.
    const previousIndex = this.restoreRequests.findIndex(request => request.key === key)
    if (previousIndex >= 0) this.restoreRequests.splice(previousIndex, 1)[0]!.resolve()
    return new Promise<void>((resolve, reject) => {
      this.restoreRequests.push({ key, valid, task, resolve, reject })
      this.drainRestores()
    })
  }

  private drainRestores(): void {
    for (let index = this.restoreRequests.length - 1; index >= 0; index--) {
      const request = this.restoreRequests[index]!
      if (!request.valid()) {
        this.restoreRequests.splice(index, 1)
        request.resolve()
      }
    }
    const requestedLimit = this.options.maxConcurrentRestores ?? 2
    const limit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(8, Math.floor(requestedLimit))) : 2
    while (this.runningRestores < limit && this.restoreRequests.length) {
      // The most recently requested visible work wins over stale preloading.
      const request = this.restoreRequests.pop()!
      this.runningRestores++
      void Promise.resolve().then(async () => {
        if (request.valid()) await request.task()
      }).then(request.resolve, request.reject).finally(() => {
        this.runningRestores--
        this.drainRestores()
      })
    }
  }

  private trackBrowserNavigation(item: WorkbenchItem, result: void | Promise<void>) {
    if (!result) return
    const tasks = this.browserNavigations.get(item.id) ?? new Set<Promise<void>>()
    this.browserNavigations.set(item.id, tasks)
    const task = result.catch(error => this.reportError(error, item)).finally(() => {
      tasks.delete(task)
      if (!tasks.size && this.browserNavigations.get(item.id) === tasks) {
        this.browserNavigations.delete(item.id)
      }
    })
    tasks.add(task)
  }

  private enqueue(itemId: string, task: () => Promise<void>, item: WorkbenchItem) {
    const previous = this.queues.get(itemId) ?? Promise.resolve()
    const next = previous
      .catch(() => undefined)
      .then(task)
      .catch(error => this.reportError(error, item))
      .finally(() => {
        if (this.queues.get(itemId) === next) this.queues.delete(itemId)
      })
    this.queues.set(itemId, next)
  }

  private async ensureRuntime(
    item: WorkbenchItem,
    expectedEpoch?: number,
    expectedActivationEpoch?: number,
  ): Promise<WorkbenchPanelRuntime | null> {
    const available = () => this.openItemIds.has(item.id) && (expectedEpoch === undefined
      ? this.isCurrentDescriptor(item)
      : this.itemEpochs.get(item.id) === expectedEpoch)
      && (expectedActivationEpoch === undefined || this.mayRestore(item, expectedEpoch!, expectedActivationEpoch))
    if (!available()) return null
    const existing = this.runtimes.get(item.id)
    if (existing) return existing
    const definition = this.registry.resolve(item)
    if (!definition?.createRuntime) return null
    const runtimeEpoch = this.itemEpochs.get(item.id) ?? 0
    const runtimeGeneration = ++this.nextRuntimeGeneration
    this.runtimeGenerations.set(item.id, runtimeGeneration)
    const isCurrentRuntime = () =>
      this.openItemIds.has(item.id)
      && this.itemEpochs.get(item.id) === runtimeEpoch
      && this.runtimeGenerations.get(item.id) === runtimeGeneration
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: this.scopedNativeWorkbenchApi(item, runtimeEpoch, runtimeGeneration),
      getRenderState: () =>
        isCurrentRuntime() ? this.getRenderState(item.id) : {},
      updateRenderState: patch => {
        if (!isCurrentRuntime()) return
        const current = this.getRenderState(item.id)
        this.renderStates.set(item.id, { ...current, ...patch })
      },
      getViewState: () => isCurrentRuntime() ? copyViewState(this.viewStates.get(item.id) ?? {}) : {},
      saveViewState: state => {
        if (!isCurrentRuntime()) return
        try {
          this.viewStates.set(item.id, copyViewState(state))
        } catch (error) {
          this.reportError(error, item)
        }
      },
      runRestore: task => {
        const activationEpoch = this.activationEpochs.get(item.id) ?? 0
        return this.admitRestore(
          `resource:${item.id}:${runtimeGeneration}`,
          () => isCurrentRuntime() && this.resumedItemIds.has(item.id)
            && (this.activationEpochs.get(item.id) ?? 0) === activationEpoch,
          task,
        ).catch(error => {
          if (isCurrentRuntime()) this.reportError(error, item)
        })
      },
      isItemOpen: isCurrentRuntime,
      isItemActive: () => isCurrentRuntime() && this.resumedItemIds.has(item.id),
      setExpanded: expanded => {
        if (isCurrentRuntime() && this.resumedItemIds.has(item.id)) {
          this.options.setExpanded?.(expanded)
        }
      },
      reportError: error => {
        if (isCurrentRuntime()) this.reportError(error, item)
      },
    }
    let runtime: WorkbenchPanelRuntime
    try {
      runtime = await definition.createRuntime!(item, context)
    } catch (error) {
      if (this.runtimeGenerations.get(item.id) === runtimeGeneration) this.runtimeGenerations.delete(item.id)
      throw error
    }
    if (!isCurrentRuntime()) {
      await runtime.dispose?.('runtime-detached')
      return null
    }
    this.runtimes.set(item.id, runtime)
    const componentHandle = this.componentHandles.get(item.id)
    if (componentHandle !== undefined) await runtime.setComponentHandle?.(componentHandle)
    const rect = this.surfaceRects.get(item.id)
    if (rect && isCurrentRuntime()) {
      await runtime.handleSurfaceRect?.(rect, item)
    }
    return runtime
  }

  private async disposeRuntime(itemId: string, reason: WorkbenchDisposeReason) {
    const runtime = this.runtimes.get(itemId)
    this.runtimes.delete(itemId)
    this.runtimeGenerations.delete(itemId)
    this.componentHandles.delete(itemId)
    this.renderStates.delete(itemId)
    this.drainRestores()
    await runtime?.dispose?.(reason)
  }

  private hideNativeSurfaceImmediately(
    item: WorkbenchItem,
    rect = this.surfaceRects.get(item.id),
  ) {
    const nativeApi = this.options.nativeWorkbenchApi
    if (!nativeApi || item.hostKind !== 'native-webcontents') return
    const request = {
      surfaceId: item.id,
      x: rect?.x ?? 0,
      y: rect?.y ?? 0,
      width: rect?.width ?? 0,
      height: rect?.height ?? 0,
      visible: false,
    }
    void nativeApi.setSurfaceRect(request).catch(error => this.reportError(error, item))
  }

  private scopedNativeWorkbenchApi(
    item: WorkbenchItem,
    runtimeEpoch: number,
    runtimeGeneration: number,
  ): NativeWorkbenchApi | undefined {
    const nativeApi = this.options.nativeWorkbenchApi
    if (!nativeApi) return undefined
    const isCurrent = () =>
      this.openItemIds.has(item.id)
      && this.itemEpochs.get(item.id) === runtimeEpoch
      && this.runtimeGenerations.get(item.id) === runtimeGeneration
    const mayDestroy = () => {
      const currentEpoch = this.itemEpochs.get(item.id)
      const currentGeneration = this.runtimeGenerations.get(item.id)
      return (currentEpoch === undefined || currentEpoch === runtimeEpoch)
        && (currentGeneration === undefined || currentGeneration === runtimeGeneration)
    }
    const mayShow = () =>
      isCurrent()
      && this.resumedItemIds.has(item.id)
      && this.surfaceRects.get(item.id)?.visible === true
    const ignored = (): NativeWorkbenchSurfaceResult => ({
      ok: false,
      message: 'Workbench surface is no longer active',
    })
    return {
      ...(nativeApi.getCapabilities
        ? { getCapabilities: () => nativeApi.getCapabilities!() }
        : {}),
      ...(nativeApi.createArtifactPreviewLease
        && nativeApi.renewArtifactPreviewLease
        && nativeApi.revokeArtifactPreviewLease
        ? {
            createArtifactPreviewLease: request => (
              isCurrent()
                ? nativeApi.createArtifactPreviewLease!(request)
                : Promise.resolve({
                    ok: false as const,
                    status: 409,
                    code: 'WORKBENCH_SURFACE_INACTIVE',
                    message: 'Workbench surface is no longer active',
                  })
            ),
            renewArtifactPreviewLease: request => (
              isCurrent()
                ? nativeApi.renewArtifactPreviewLease!(request)
                : Promise.resolve({
                    ok: false as const,
                    status: 409,
                    code: 'WORKBENCH_SURFACE_INACTIVE',
                    message: 'Workbench surface is no longer active',
                  })
            ),
            revokeArtifactPreviewLease: request => nativeApi.revokeArtifactPreviewLease!(request),
          }
        : {}),
      ...(nativeApi.getArtifactAnnotationCapabilities
        ? {
            getArtifactAnnotationCapabilities: () => (
              isCurrent()
                ? nativeApi.getArtifactAnnotationCapabilities!()
                : Promise.resolve({
                    version: 3 as const,
                    available: false,
                    picker: false,
                    trustedOverlay: false,
                    reason: 'Workbench surface is no longer active',
                  })
            ),
          }
        : {}),
      ...(nativeApi.setArtifactAnnotationMode
        ? {
            setArtifactAnnotationMode: request => (
              (request.enabled ? mayShow() : isCurrent())
                ? nativeApi.setArtifactAnnotationMode!(request)
                : Promise.resolve(ignored())
            ),
          }
        : {}),
      ...(nativeApi.showArtifactAnnotationOverlay
        ? {
            showArtifactAnnotationOverlay: request => (
              mayShow()
                ? nativeApi.showArtifactAnnotationOverlay!(request)
                : Promise.resolve(ignored())
            ),
          }
        : {}),
      ...(nativeApi.closeArtifactAnnotationOverlay
        ? {
            closeArtifactAnnotationOverlay: request => (
              isCurrent()
                ? nativeApi.closeArtifactAnnotationOverlay!(request)
                : Promise.resolve(ignored())
            ),
          }
        : {}),
      ...(nativeApi.getWorkbenchBrowserTarget ? {
        getWorkbenchBrowserTarget: request => isCurrent()
          ? nativeApi.getWorkbenchBrowserTarget!(request)
          : Promise.reject(new Error('Workbench surface is no longer active')),
      } : {}),
      ...(nativeApi.focusWorkbenchAnnotation ? {
        focusWorkbenchAnnotation: request => mayShow()
          ? nativeApi.focusWorkbenchAnnotation!(request)
          : Promise.resolve(ignored()),
      } : {}),
      ...(nativeApi.captureWorkbenchScreenshot ? {
        captureWorkbenchScreenshot: request => mayShow()
          ? nativeApi.captureWorkbenchScreenshot!(request)
          : Promise.reject(new Error('Workbench surface is no longer active')),
      } : {}),
      async createSurface(request) {
        if (!isCurrent()) return ignored()
        const result = await nativeApi.createSurface(request)
        if (!isCurrent()) {
          // Replacement creation owns this native id now. Its create path
          // replaces the old surface; stale cleanup must not close the new one.
          if (mayDestroy()) await nativeApi.destroySurface(request.surfaceId)
          return ignored()
        }
        if (!mayShow()) {
          await nativeApi.setSurfaceRect({
            surfaceId: request.surfaceId,
            x: 0,
            y: 0,
            width: 0,
            height: 0,
            visible: false,
          })
        }
        return result
      },
      async setSurfaceRect(request) {
        if (!isCurrent()) return ignored()
        return nativeApi.setSurfaceRect({
          ...request,
          visible: request.visible && mayShow(),
        })
      },
      async activateSurface(surfaceId) {
        if (!mayShow()) return ignored()
        return nativeApi.activateSurface(surfaceId)
      },
      destroySurface: surfaceId => mayDestroy()
        ? nativeApi.destroySurface(surfaceId)
        : Promise.resolve(ignored()),
      ...(nativeApi.navigateSurface
        ? {
            async navigateSurface(request) {
              if (!isCurrent()) return ignored()
              return nativeApi.navigateSurface!(request)
            },
          }
        : {}),
      ...(nativeApi.respondToPermission
        ? {
            async respondToPermission(request) {
              if (!isCurrent()) return ignored()
              return nativeApi.respondToPermission!(request)
            },
          }
        : {}),
      onSurfaceEvent: callback => nativeApi.onSurfaceEvent(callback),
    }
  }

  private reportError(error: unknown, item: WorkbenchItem) {
    if (this.options.onError) this.options.onError(error, item)
    else console.error(`[workbench] "${item.kind}" runtime failed`, error)
  }

  private isCurrentDescriptor(item: WorkbenchItem): boolean {
    const current = this.items.get(item.id)
    if (!current || !this.openItemIds.has(item.id)) return false
    const rawItem = toRaw(item)
    return toRaw(current) === rawItem
      && this.descriptorEpochs.get(rawItem) === this.itemEpochs.get(item.id)
  }
}

type WorkbenchStore = ReturnType<typeof useWorkbenchStore>

export function attachWorkbenchRuntime(
  store: WorkbenchStore,
  manager: WorkbenchRuntimeManager,
): () => Promise<void> {
  const unsubscribe = store.onLifecycle(event => manager.handle(event))
  for (const item of store.items) manager.handle({ type: 'open', item })
  if (store.activeItem) {
    manager.handle({ type: 'activate', item: store.activeItem })
    if (store.expanded && store.hostAvailable) {
      manager.handle({ type: 'resume', item: store.activeItem })
    }
  }
  return async () => {
    unsubscribe()
    await manager.disposeAll('runtime-detached')
  }
}
