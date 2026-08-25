import type { WorkbenchResource } from '@/types/workbenchResources'
import {
  artifactPayloadFromWorkbenchResource,
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

export interface WorkbenchResourceCollectionOptions {
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
  constructor(
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: WorkbenchResourceCollectionOptions,
  ) {}

  async handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    const resource = resourceFromEvent(event)
    if (!resource) return
    if (event.type === 'resource-open') {
      const busyKey = workbenchResourceKey(resource.resource)
      this.context.updateRenderState({
        resourceBusyKey: busyKey,
        resourceOpenErrorKey: '',
        resourceOpenErrorMessage: '',
      })
      try {
        await this.options.open(resource, item)
      } catch (error) {
        this.context.updateRenderState({
          resourceOpenErrorKey: busyKey,
          resourceOpenErrorMessage: productErrorMessage(error, this.options),
        })
      } finally {
        this.context.updateRenderState({ resourceBusyKey: '' })
      }
      return
    }
    if (![
      'resource-download',
      'resource-publish',
    ].includes(event.type)) return

    const busyKey = workbenchResourceKey(resource.resource)
    this.context.updateRenderState({ resourceBusyKey: busyKey })
    try {
      if (event.type === 'resource-download') {
        await this.options.download(resource, item)
      } else {
        await this.options.publish(resource, item)
      }
    } catch (error) {
      this.options.pushError(productErrorMessage(error, this.options))
    } finally {
      this.context.updateRenderState({ resourceBusyKey: '' })
    }
  }
}

export function createWorkbenchResourceCollectionDefinition(
  options: WorkbenchResourceCollectionOptions,
): WorkbenchPanelDefinition {
  return {
    kind: 'resource-collection',
    component: WorkbenchResourceCollectionPanel,
    supports: item => item.kind === 'resource-collection',
    getHeader: item => ({
      title: options.t('workbench.resources.title'),
      subtitle: options.t('workbench.resources.count', {
        count: resourcesFromWorkbenchItem(item).length,
      }),
      icon: 'folder',
    }),
    getProps: (item, state) => ({
      busyKey: String(state.runtimeState.resourceBusyKey || ''),
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
      retryLabel: options.t('workbench.resources.retry'),
      resources: resourcesFromWorkbenchItem(item),
      unavailableReason: (resource: WorkbenchResource) => options.t(
        workbenchResourceUnavailableReasonKey(resource.capabilities.reasonCode),
      ),
    }),
    createRuntime: (_item, context) => new WorkbenchResourceCollectionRuntime(
      context,
      options,
    ),
  }
}
