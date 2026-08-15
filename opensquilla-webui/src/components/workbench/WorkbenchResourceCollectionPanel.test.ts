// @vitest-environment happy-dom

import { createApp } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import type { WorkbenchResource } from '@/types/workbenchResources'
import { createWorkbenchResourceRef } from '@/types/workbenchResources'
import WorkbenchResourceCollectionPanel from './WorkbenchResourceCollectionPanel.vue'

function resource(type: WorkbenchResource['resource']['type'], reasonCode?: string) {
  return {
    resource: createWorkbenchResourceRef(type, `${type}-1`),
    name: `${type}.html`,
    mime: 'text/html',
    size: 64,
    capabilities: {
      preview: reasonCode === undefined,
      download: true,
      selectionContext: false,
      manualEdit: reasonCode === undefined,
      agentEdit: false,
      edit: reasonCode === undefined,
      publish: type === 'document',
      reasonCode,
    },
    relations: {},
  } satisfies WorkbenchResource
}

function mount(resources: WorkbenchResource[]) {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp(WorkbenchResourceCollectionPanel, {
    downloadLabel: (item: WorkbenchResource) => `Download ${item.name}`,
    editLabel: (item: WorkbenchResource) => `Edit ${item.name}`,
    emptyLabel: 'Empty',
    groupLabels: {
      attachment: 'Attachments',
      document: 'Working copies',
      deliverable: 'Published',
      url: 'Links',
    },
    label: 'Workbench',
    previewLabel: (item: WorkbenchResource) => `Preview ${item.name}`,
    publishLabel: (item: WorkbenchResource) => `Publish ${item.name}`,
    resources,
    unavailableReason: (item: WorkbenchResource) => (
      item.capabilities.reasonCode === 'office_adapter_not_available'
        ? 'Office editing is not available yet.'
        : 'Editing is not available for this resource.'
    ),
  })
  app.mount(element)
  return { app, element }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('WorkbenchResourceCollectionPanel', () => {
  it('uses manualEdit rather than preview or the legacy edit summary for the edit action', () => {
    const legacySummaryOnly = resource('attachment')
    legacySummaryOnly.capabilities.manualEdit = false
    legacySummaryOnly.capabilities.edit = true
    const mounted = mount([legacySummaryOnly])

    expect(mounted.element.querySelector('[aria-label="Edit attachment.html"]')).toBeNull()
    expect(mounted.element.querySelector('[aria-label="Preview attachment.html"]')).not.toBeNull()
    mounted.app.unmount()
  })

  it('orders lifecycle groups and localizes capability reasons without leaking codes', () => {
    const mounted = mount([
      resource('url', 'future_adapter_missing'),
      resource('deliverable'),
      resource('document'),
      resource('attachment', 'office_adapter_not_available'),
    ])

    expect([...mounted.element.querySelectorAll('h3')].map(item => item.textContent)).toEqual([
      'Attachments',
      'Working copies',
      'Published',
      'Links',
    ])
    expect(mounted.element.textContent).toContain('Office editing is not available yet.')
    expect(mounted.element.textContent).toContain('Editing is not available for this resource.')
    expect(mounted.element.textContent).not.toContain('office_adapter_not_available')
    expect(mounted.element.textContent).not.toContain('future_adapter_missing')
    mounted.app.unmount()
  })
})
