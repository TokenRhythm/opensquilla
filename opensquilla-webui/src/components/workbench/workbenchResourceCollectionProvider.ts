import type { WorkbenchResource } from '@/types/workbenchResources'
import {
  resourcesFromWorkbenchItem,
  workbenchResourceKey,
} from '@/workbench/workbenchResourceItems'
import { workbenchResourceUnavailableReasonKey } from '@/workbench/resourceCapabilityPresentation'
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
  importDocument(resource: WorkbenchResource, item: WorkbenchItem): Promise<void>
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

class WorkbenchResourceCollectionRuntime implements WorkbenchPanelRuntime {
  constructor(
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: WorkbenchResourceCollectionOptions,
  ) {}

  async handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    const resource = resourceFromEvent(event)
    if (!resource) return
    if (event.type === 'resource-preview') {
      await this.options.open(resource, item)
      return
    }
    if (![
      'resource-download',
      'resource-import',
      'resource-publish',
    ].includes(event.type)) return

    const busyKey = workbenchResourceKey(resource.resource)
    this.context.updateRenderState({ resourceBusyKey: busyKey })
    try {
      if (event.type === 'resource-download') {
        await this.options.download(resource, item)
      } else if (event.type === 'resource-import') {
        await this.options.importDocument(resource, item)
      } else {
        await this.options.publish(resource, item)
      }
    } catch (error) {
      this.options.pushError(error instanceof Error
        ? error.message
        : this.options.t('workbench.resources.actionFailed'))
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
      editLabel: (resource: WorkbenchResource) => options.t(
        'workbench.resources.edit',
        { name: resource.name },
      ),
      emptyLabel: options.t('workbench.resources.empty'),
      groupLabels: {
        attachment: options.t('workbench.resources.groups.attachments'),
        document: options.t('workbench.resources.groups.documents'),
        deliverable: options.t('workbench.resources.groups.deliverables'),
        url: options.t('workbench.resources.groups.urls'),
      },
      label: options.t('workbench.resources.title'),
      previewLabel: (resource: WorkbenchResource) => options.t(
        'workbench.resources.preview',
        { name: resource.name },
      ),
      publishLabel: (resource: WorkbenchResource) => options.t(
        'workbench.resources.publish',
        { name: resource.name },
      ),
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
