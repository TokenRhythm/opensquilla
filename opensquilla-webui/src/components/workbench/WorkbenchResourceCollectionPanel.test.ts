// @vitest-environment happy-dom

import { createApp } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

function mount(
  resources: WorkbenchResource[],
  extraProps: Record<string, unknown> = {},
) {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp(WorkbenchResourceCollectionPanel, {
    downloadLabel: (item: WorkbenchResource) => `Download ${item.name}`,
    emptyLabel: 'Empty',
    groupLabels: {
      files: 'Files',
      links: 'Links',
    },
    label: 'Workbench',
    openLabel: (item: WorkbenchResource) => `Open ${item.name}`,
    preparingLabel: 'Preparing editor…',
    retryLabel: 'Retry',
    resources,
    unavailableReason: (item: WorkbenchResource) => (
      item.capabilities.reasonCode === 'office_adapter_not_available'
        ? 'Office editing is not available yet.'
        : 'Editing is not available for this resource.'
    ),
    ...extraProps,
  })
  app.mount(element)
  return { app, element }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('WorkbenchResourceCollectionPanel', () => {
  it('uses one row action and does not expose a separate import pencil', () => {
    const legacySummaryOnly = resource('attachment')
    legacySummaryOnly.capabilities.manualEdit = false
    legacySummaryOnly.capabilities.edit = true
    const onWorkbenchEvent = vi.fn()
    const mounted = mount([legacySummaryOnly], { onWorkbenchEvent })

    const open = mounted.element.querySelector<HTMLButtonElement>(
      '[aria-label="Open attachment.html"]',
    )
    expect(open).not.toBeNull()
    expect(mounted.element.querySelector('[aria-label="Edit attachment.html"]')).toBeNull()
    open?.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'resource-open',
      payload: legacySummaryOnly,
    })
    mounted.app.unmount()
  })

  it('shows progress and an inline retry for a failed open', () => {
    const mounted = mount([resource('attachment')], {
      busyKey: 'attachment:attachment-1',
      openErrorKey: 'attachment:attachment-1',
      openErrorMessage: 'Gateway disconnected',
    })

    expect(mounted.element.textContent).toContain('Preparing editor…')
    expect(mounted.element.textContent).toContain('Gateway disconnected')
    expect(mounted.element.querySelector('[aria-label="Retry"]')).not.toBeNull()
    mounted.app.unmount()
  })

  it('shows only product file and link groups without leaking storage lifecycles', () => {
    const onWorkbenchEvent = vi.fn()
    const readonlyOffice = resource('attachment', 'office_adapter_not_available')
    const mounted = mount([
      resource('url', 'future_adapter_missing'),
      resource('deliverable'),
      resource('document'),
      readonlyOffice,
    ], { onWorkbenchEvent })

    expect([...mounted.element.querySelectorAll('h3')].map(item => item.textContent)).toEqual([
      'Files',
      'Links',
    ])
    expect(mounted.element.textContent).not.toMatch(/working cop|published/i)
    expect(mounted.element.querySelector('[aria-label^="Publish "]')).toBeNull()
    expect(mounted.element.textContent).toContain('Office editing is not available yet.')
    expect(mounted.element.textContent).toContain('Editing is not available for this resource.')
    expect(mounted.element.textContent).not.toContain('office_adapter_not_available')
    expect(mounted.element.textContent).not.toContain('future_adapter_missing')
    const readonlyOpen = mounted.element.querySelector<HTMLButtonElement>(
      '[aria-label="Open attachment.html"]',
    )
    expect(readonlyOpen?.disabled).toBe(false)
    readonlyOpen?.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'resource-open',
      payload: readonlyOffice,
    })
    expect(mounted.element.querySelector('[aria-label="Download attachment.html"]'))
      .not.toBeNull()
    mounted.app.unmount()
  })

  it('labels a download-only file row as Download while keeping readonly HTML as Open', () => {
    const downloadOnly = resource('attachment', 'resource_unsupported')
    downloadOnly.name = 'archive.zip'
    downloadOnly.mime = 'application/zip'
    downloadOnly.capabilities.preview = false
    downloadOnly.capabilities.download = true
    const readonlyHtml = resource('deliverable', 'bundle_not_supported')
    readonlyHtml.capabilities.preview = false
    readonlyHtml.capabilities.download = true
    const mounted = mount([downloadOnly, readonlyHtml], {
      openLabel: (item: WorkbenchResource) => (
        item === downloadOnly ? `Download ${item.name}` : `Open ${item.name}`
      ),
    })

    expect(mounted.element.querySelector('[aria-label="Download archive.zip"]')).not.toBeNull()
    expect(mounted.element.querySelector('[aria-label="Open deliverable.html"]')).not.toBeNull()
    mounted.app.unmount()
  })
})
