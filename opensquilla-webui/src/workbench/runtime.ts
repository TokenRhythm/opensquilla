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
}

/**
 * Owns all non-serializable panel resources. Events for each item are queued so
 * rapid tab switching cannot resume a runtime after a later close disposed it.
 */
export class WorkbenchRuntimeManager {
  private readonly runtimes = new Map<string, WorkbenchPanelRuntime>()
  private readonly items = new Map<string, WorkbenchItem>()
  private readonly itemEpochs = new Map<string, number>()
  private readonly descriptorEpochs = new WeakMap<object, number>()
  private readonly openItemIds = new Set<string>()
  private readonly resumedItemIds = new Set<string>()
  private readonly queues = new Map<string, Promise<void>>()
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
      epoch += 1
      this.itemEpochs.set(id, epoch)
      this.renderStates.delete(id)
      this.resumedItemIds.delete(id)
    }
    if (event.type === 'dispose') {
      this.openItemIds.delete(id)
      this.resumedItemIds.delete(id)
      this.hideNativeSurfaceImmediately(event.item)
    } else {
      this.items.set(id, event.item)
      this.openItemIds.add(id)
      this.descriptorEpochs.set(toRaw(event.item), epoch)
    }
    if (event.type === 'resume') this.resumedItemIds.add(id)
    if (event.type === 'suspend') {
      this.resumedItemIds.delete(id)
      this.hideNativeSurfaceImmediately(event.item)
    }
    const eventEpoch = epoch
    this.enqueue(id, async () => {
      if (event.type === 'open') {
        await this.ensureRuntime(event.item, eventEpoch)
      } else if (event.type === 'update') {
        const runtime = await this.ensureRuntime(event.item, eventEpoch)
        await runtime?.update?.(event.item)
      } else if (event.type === 'activate') {
        const runtime = await this.ensureRuntime(event.item, eventEpoch)
        await runtime?.activate?.(event.item)
      } else if (event.type === 'resume') {
        const runtime = await this.ensureRuntime(event.item, eventEpoch)
        await runtime?.resume?.(event.item)
      } else if (event.type === 'suspend') {
        if (event.item.retention === 'dispose-on-suspend') {
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
        }
      }
    }, event.item)
  }

  setComponentHandle(item: WorkbenchItem, handle: unknown): void {
    if (!this.isCurrentDescriptor(item)) return
    this.enqueue(item.id, async () => {
      const runtime = handle === null || handle === undefined
        ? this.runtimes.get(item.id)
        : await this.ensureRuntime(item)
      await runtime?.setComponentHandle?.(handle)
    }, item)
  }

  handleComponentEvent(item: WorkbenchItem, event: WorkbenchComponentEvent): void {
    if (!this.isCurrentDescriptor(item)) return
    this.enqueue(item.id, async () => {
      const runtime = await this.ensureRuntime(item)
      await runtime?.handleComponentEvent?.(event, item)
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
    this.enqueue(item.id, async () => {
      const existing = this.runtimes.get(item.id)
      const runtime = await this.ensureRuntime(item)
      // A rect can arrive before the descriptor's open event has created its
      // runtime. In that case ensureRuntime replays the stored rect exactly
      // once; only forward here when this task did not create the runtime.
      if (runtime && runtime === existing) {
        await runtime.handleSurfaceRect?.(rect, item)
      }
    }, item)
  }

  handleNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent): void {
    const item = this.items.get(event.surfaceId)
    if (!item || !this.openItemIds.has(item.id)) return
    if (event.type === 'error' || event.type === 'crashed') {
      this.hideNativeSurfaceImmediately(item)
    }
    this.enqueue(item.id, async () => {
      const runtime = await this.ensureRuntime(item)
      await runtime?.handleNativeSurfaceEvent?.(event, item)
    }, item)
  }

  performAction(item: WorkbenchItem, actionId: string): void {
    if (!this.isCurrentDescriptor(item)) return
    this.enqueue(item.id, async () => {
      const runtime = await this.ensureRuntime(item)
      await runtime?.performAction?.(actionId, item)
    }, item)
  }

  getRenderState(itemId: string): Readonly<Record<string, unknown>> {
    return this.renderStates.get(itemId) ?? {}
  }

  async flush(itemId?: string): Promise<void> {
    if (itemId) {
      await this.queues.get(itemId)
      return
    }
    await Promise.all([...this.queues.values()])
  }

  /** Ask the live panel to persist pending work before its store item is removed. */
  async beforeClose(
    item: WorkbenchItem,
    options?: WorkbenchBeforeCloseOptions,
  ): Promise<boolean> {
    if (!this.isCurrentDescriptor(item)) return true
    await this.flush(item.id)
    const runtime = await this.ensureRuntime(item)
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
    await this.flush()
    const ids = [...this.runtimes.keys()]
    for (const id of ids) await this.disposeRuntime(id, reason)
    this.items.clear()
    this.itemEpochs.clear()
    this.surfaceRects.clear()
    this.renderStates.clear()
  }

  hasRuntime(itemId: string): boolean {
    return this.runtimes.has(itemId)
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
  ): Promise<WorkbenchPanelRuntime | null> {
    const available = expectedEpoch === undefined
      ? this.isCurrentDescriptor(item)
      : this.itemEpochs.get(item.id) === expectedEpoch
    if (!available) return null
    const existing = this.runtimes.get(item.id)
    if (existing) return existing
    const definition = this.registry.resolve(item)
    if (!definition?.createRuntime) return null
    const runtimeEpoch = this.itemEpochs.get(item.id) ?? 0
    const isCurrentRuntime = () =>
      this.openItemIds.has(item.id)
      && this.itemEpochs.get(item.id) === runtimeEpoch
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: this.scopedNativeWorkbenchApi(item, runtimeEpoch),
      getRenderState: () =>
        isCurrentRuntime() ? this.getRenderState(item.id) : {},
      updateRenderState: patch => {
        if (!isCurrentRuntime()) return
        const current = this.getRenderState(item.id)
        this.renderStates.set(item.id, { ...current, ...patch })
      },
      isItemOpen: isCurrentRuntime,
      setExpanded: expanded => {
        if (isCurrentRuntime()) this.options.setExpanded?.(expanded)
      },
      reportError: error => this.reportError(error, item),
    }
    const runtime = await definition.createRuntime(item, context)
    this.runtimes.set(item.id, runtime)
    const rect = this.surfaceRects.get(item.id)
    if (rect && isCurrentRuntime()) {
      await runtime.handleSurfaceRect?.(rect, item)
    }
    return runtime
  }

  private async disposeRuntime(itemId: string, reason: WorkbenchDisposeReason) {
    const runtime = this.runtimes.get(itemId)
    this.runtimes.delete(itemId)
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
  ): NativeWorkbenchApi | undefined {
    const nativeApi = this.options.nativeWorkbenchApi
    if (!nativeApi) return undefined
    const isCurrent = () =>
      this.openItemIds.has(item.id)
      && this.itemEpochs.get(item.id) === runtimeEpoch
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
              mayShow()
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
      ...(nativeApi.screenshot
        ? {
            screenshot: request => (
              mayShow()
                ? nativeApi.screenshot!(request)
                : Promise.resolve({
                    ok: false as const,
                    method: 'screenshot' as const,
                    code: 'unavailable',
                    message: 'Workbench surface is no longer active',
                  })
            ),
          }
        : {}),
      async createSurface(request) {
        if (!isCurrent()) return ignored()
        const result = await nativeApi.createSurface(request)
        if (!isCurrent()) {
          await nativeApi.destroySurface(request.surfaceId)
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
      destroySurface: surfaceId => nativeApi.destroySurface(surfaceId),
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
