import type { WorkbenchResource } from '@/types/workbenchResources'
import {
  artifactPayloadFromWorkbenchResource,
  canonicalWorkbenchResources,
  resourcesFromWorkbenchItem,
  workbenchResourceKey,
} from '@/workbench/workbenchResourceItems'
import { workbenchResourceUnavailableReasonKey } from '@/workbench/resourceCapabilityPresentation'
import {
  artifactProductReasonCode,
  classifyArtifactProductError,
} from '@/utils/artifactProductErrors'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import type {
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelDefinition,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
} from '@/workbench/types'
import WorkbenchResourceCollectionPanel from './WorkbenchResourceCollectionPanel.vue'

type Translate = (key: string, params?: Record<string, unknown>) => string

interface CollectionOwner {
  state: Record<string, unknown>
  readers: Set<WorkbenchRuntimeContext>
  busy: boolean
}

export interface WorkbenchResourceCollectionOptions {
  resources?(item: WorkbenchItem): readonly WorkbenchResource[]
  load?(item: WorkbenchItem): Promise<unknown>
  retain?(item: WorkbenchItem): () => void
  download(resource: WorkbenchResource, item: WorkbenchItem): Promise<void>
  open(resource: WorkbenchResource, item: WorkbenchItem): Promise<void> | void
  publish(resource: WorkbenchResource, item: WorkbenchItem): Promise<void>
  pushError(message: string): void
  t: Translate
}

function resourceFromEvent(event: WorkbenchComponentEvent): WorkbenchResource | null {
  return event.payload && typeof event.payload === 'object'
    ? event.payload as WorkbenchResource
    : null
}

function productErrorMessage(
  error: unknown,
  options: WorkbenchResourceCollectionOptions,
): string {
  const classified = classifyArtifactProductError(error)
  const reasonCode = artifactProductReasonCode(error)
  if (classified.code === 'RESOURCE_UNSUPPORTED' && reasonCode) {
    return options.t(workbenchResourceUnavailableReasonKey(reasonCode))
  }
  const translated = options.t(classified.messageKey)
  return translated === classified.messageKey ? classified.fallbackMessage : translated
}

function primaryActionIsDownload(resource: WorkbenchResource): boolean {
  return !resource.capabilities.preview
    && resource.capabilities.download
    && artifactWorkbenchPreviewKind(artifactPayloadFromWorkbenchResource(resource)) !== 'html'
}

class WorkbenchResourceCollectionRuntime implements WorkbenchPanelRuntime {
  private releaseMetadata?: () => void
  private readingInventory = false
  constructor(
    item: WorkbenchItem,
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: WorkbenchResourceCollectionOptions,
    private readonly owner: CollectionOwner,
    private readonly release: (reason: string) => void,
  ) {
    this.releaseMetadata = options.retain?.(item)
    owner.readers.add(context)
    context.updateRenderState({ ...owner.state, collectionTop: context.getViewState?.().top ?? 0 })
  }
  private updateOwner(patch: Record<string, unknown>) {
    Object.assign(this.owner.state, patch)
    for (const context of this.owner.readers) context.updateRenderState(patch)
  }
  dispose(reason: string) {
    this.releaseMetadata?.()
    this.owner.readers.delete(this.context)
    this.release(reason)
  }
  activate(item: WorkbenchItem) { this.restoreInventory(item) }
  resume(item: WorkbenchItem) { this.restoreInventory(item) }
  private restoreInventory(item: WorkbenchItem) {
    if (!this.options.load || this.readingInventory) return
    this.readingInventory = true
    this.context.updateRenderState({ inventoryLoading: true, inventoryError: '' })
    void this.options.load(item).catch(error => {
      if (this.owner.readers.has(this.context)) this.context.updateRenderState({ inventoryError: productErrorMessage(error, this.options) })
    }).finally(() => {
      this.readingInventory = false
      if (this.owner.readers.has(this.context)) this.context.updateRenderState({ inventoryLoading: false })
    })
  }

  handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    if (event.type === 'collection-refresh') { this.restoreInventory(item); return }
    // The original owner finishes the action; navigation must not wait for a
    // slow publication/import response before releasing this reader.
    void this.runOperation(event, item)
  }
  private async runOperation(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    if (event.type === 'collection-reading-state') {
      const top = Number(event.payload)
      if (Number.isFinite(top)) this.context.saveViewState?.({ top: Math.max(0, Math.min(top, 10_000_000)) })
      return
    }
    const resource = resourceFromEvent(event)
    if (!resource || this.owner.busy) return
    if (event.type === 'resource-open') {
      this.owner.busy = true
      const busyKey = workbenchResourceKey(resource.resource)
      this.updateOwner({
        resourceBusyKey: busyKey,
        resourceOpenErrorKey: '',
        resourceOpenErrorMessage: '',
      })
      try {
        await this.options.open(resource, item)
      } catch (error) {
        this.updateOwner({
          resourceOpenErrorKey: busyKey,
          resourceOpenErrorMessage: productErrorMessage(error, this.options),
        })
      } finally {
        this.owner.busy = false
        this.updateOwner({ resourceBusyKey: '' })
      }
      return
    }
    if (![
      'resource-download',
      'resource-publish',
    ].includes(event.type)) return

    const busyKey = workbenchResourceKey(resource.resource)
    this.owner.busy = true
    this.updateOwner({ resourceBusyKey: busyKey })
    try {
      if (event.type === 'resource-download') {
        await this.options.download(resource, item)
      } else {
        await this.options.publish(resource, item)
      }
    } catch (error) {
      this.options.pushError(productErrorMessage(error, this.options))
    } finally {
      this.owner.busy = false
      this.updateOwner({ resourceBusyKey: '' })
    }
  }
}

export function createWorkbenchResourceCollectionDefinition(
  options: WorkbenchResourceCollectionOptions,
): WorkbenchPanelDefinition {
  // The descriptor owns operations. Readers may be released on every switch
  // without cancelling publication or creating a second import/open request.
  const owners = new Map<string, CollectionOwner>()
  return {
    kind: 'resource-collection',
    component: WorkbenchResourceCollectionPanel,
    supports: item => item.kind === 'resource-collection',
    beforeClose: item => {
      const owner = owners.get(item.id)
      if (owner?.busy) return false
      owners.delete(item.id)
      return true
    },
    getHeader: item => ({
      title: options.t('workbench.resources.title'),
      subtitle: options.t('workbench.resources.count', {
        count: options.resources
          ? canonicalWorkbenchResources(options.resources(item)).length
          : Number(item.payload.resourceCount || 0),
      }),
      icon: 'folder',
    }),
    getProps: (item, state) => ({
      busyKey: String(state.runtimeState.resourceBusyKey || ''),
      readingTop: Number(state.runtimeState.collectionTop || 0),
      loading: state.runtimeState.inventoryLoading === true,
      error: String(state.runtimeState.inventoryError || ''),
      downloadLabel: (resource: WorkbenchResource) => options.t(
        'workbench.resources.download',
        { name: resource.name },
      ),
      emptyLabel: options.t('workbench.resources.empty'),
      groupLabels: {
        files: options.t('workbench.resources.groups.files'),
        links: options.t('workbench.resources.groups.links'),
      },
      label: options.t('workbench.resources.title'),
      openErrorKey: String(state.runtimeState.resourceOpenErrorKey || ''),
      openErrorMessage: String(state.runtimeState.resourceOpenErrorMessage || ''),
      openLabel: (resource: WorkbenchResource) => options.t(
        primaryActionIsDownload(resource)
          ? 'workbench.resources.download'
          : 'workbench.resources.open',
        { name: resource.name },
      ),
      preparingLabel: options.t('workbench.resources.preparing'),
      versionLabel: (resource: WorkbenchResource) => resource.resource.type === 'document'
        ? options.t('workbench.resources.currentWorkingFile')
        : resource.resource.type === 'deliverable' ? options.t('workbench.resources.publishedVersion') : '',
      retryLabel: options.t('workbench.resources.retry'),
      resources: canonicalWorkbenchResources(options.resources?.(item) ?? resourcesFromWorkbenchItem(item)),
      unavailableReason: (resource: WorkbenchResource) => options.t(
        workbenchResourceUnavailableReasonKey(resource.capabilities.reasonCode),
      ),
    }),
    createRuntime: (item, context) => {
      let owner = owners.get(item.id)
      if (!owner) {
        owner = { state: {}, readers: new Set(), busy: false }
        owners.set(item.id, owner)
      }
      return new WorkbenchResourceCollectionRuntime(item, context, options, owner, reason => {
        if (reason === 'closed' || reason === 'store-reset' || reason === 'scope-changed') owners.delete(item.id)
      })
    },
  }
}
