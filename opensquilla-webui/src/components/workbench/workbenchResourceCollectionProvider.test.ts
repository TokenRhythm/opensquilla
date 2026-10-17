import { describe, expect, it, vi } from 'vitest'
import type { WorkbenchResource } from '@/types/workbenchResources'
import {
  ArtifactProductFailure,
  artifactProductClientError,
} from '@/utils/artifactProductErrors'
import { createResourceCollectionWorkbenchItem } from '@/workbench/workbenchResourceItems'
import type { WorkbenchRuntimeContext } from '@/workbench/types'
import { createWorkbenchResourceCollectionDefinition } from './workbenchResourceCollectionProvider'

const attachment: WorkbenchResource = {
  resource: { type: 'attachment', id: 'att_fixture' },
  name: 'uploaded.html',
  mime: 'text/html',
  size: 64,
  sha256: 'a'.repeat(64),
  downloadUrl: '/api/v1/attachments/fixture',
  capabilities: {
    preview: true,
    download: true,
    selectionContext: false,
    manualEdit: true,
    agentEdit: false,
    edit: true,
    publish: false,
  },
  relations: {},
}

function harness() {
  const calls = {
    download: vi.fn(async () => undefined),
    open: vi.fn(async () => undefined),
    publish: vi.fn(async () => undefined),
  }
  const state: Record<string, unknown> = {}
  const definition = createWorkbenchResourceCollectionDefinition({
    ...calls,
    pushError: vi.fn(),
    t: key => key,
  })
  const item = createResourceCollectionWorkbenchItem({
    resources: [attachment],
    sessionKey: 'session-a',
    title: 'Workbench',
  })
  const context: WorkbenchRuntimeContext = {
    getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true,
    setExpanded: vi.fn(),
    reportError: vi.fn(),
  }
  return { calls, context, definition, item, state }
}

describe('Workbench resource collection provider', () => {
  it('releases inventory leases and reloads a cold collection from its small descriptor', async () => {
    const { item, context, state, calls } = harness()
    let inventory: WorkbenchResource[] = []
    let complete!: () => void
    const load = vi.fn(() => new Promise<void>(resolve => { complete = () => { inventory = [attachment]; resolve() } }))
    const release = vi.fn()
    const retain = vi.fn(() => release)
    const definition = createWorkbenchResourceCollectionDefinition({ ...calls, load, retain,
      resources: () => inventory, pushError: vi.fn(), t: key => key })
    expect(item.payload).not.toHaveProperty('resources')
    const first = await definition.createRuntime!(item, context)
    await first.activate?.(item)
    expect(state.inventoryLoading).toBe(true)
    complete(); await Promise.resolve(); await Promise.resolve()
    expect(definition.getProps?.(item, { active: true, hostAvailable: true, nativeSurface: false, runtimeState: state })).toMatchObject({ resources: [attachment] })
    await first.dispose?.('suspended')
    expect(release).toHaveBeenCalledOnce()
    inventory = []
    const second = await definition.createRuntime!(item, context)
    await second.activate?.(item)
    expect(load).toHaveBeenCalledTimes(2)
    expect(retain).toHaveBeenCalledTimes(2)
    complete(); await Promise.resolve(); await Promise.resolve()
    await second.dispose?.('closed')
  })
  it('keeps the operation owner after releasing the reader and reflects completion in the rebuilt reader', async () => {
    const { calls, context, definition, item } = harness()
    let finish!: () => void
    calls.open.mockImplementation(() => new Promise(resolve => { finish = () => resolve(undefined) }))
    const first = await definition.createRuntime!(item, context)
    const opening = first.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)
    expect(await definition.beforeClose?.(item)).toBe(false)
    await first.dispose?.('suspended')
    const restored: Record<string, unknown> = {}
    const second = await definition.createRuntime!(item, { ...context,
      getRenderState: () => restored, updateRenderState: patch => Object.assign(restored, patch),
      getViewState: () => ({ top: 120 }) })
    expect(restored.resourceBusyKey).toBe('attachment:att_fixture')
    expect(restored.collectionTop).toBe(120)
    await second.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)
    expect(calls.open).toHaveBeenCalledOnce()
    finish(); await opening
    expect(restored.resourceBusyKey).toBe('')
    expect(await definition.beforeClose?.(item)).toBe(true)
  })
  it('presents non-previewable binary files as downloads but readonly HTML as openable', () => {
    const { definition, item } = harness()
    const presentation = {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {},
    }
    const props = definition.getProps?.(item, presentation) as {
      openLabel(resource: WorkbenchResource): string
    }
    const binary = {
      ...attachment,
      name: 'archive.zip',
      mime: 'application/zip',
      capabilities: { ...attachment.capabilities, preview: false },
    }
    const readonlyHtml = {
      ...attachment,
      capabilities: { ...attachment.capabilities, preview: false },
    }

    expect(props.openLabel(binary)).toBe('workbench.resources.download')
    expect(props.openLabel(readonlyHtml)).toBe('workbench.resources.open')
  })

  it('routes the unified open action and clears its busy state', async () => {
    const { calls, context, definition, item, state } = harness()
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)

    expect(calls.open).toHaveBeenCalledWith(attachment, item)
    expect(state.resourceBusyKey).toBe('')
  })

  it('keeps a failed open inline and allows the same row to retry', async () => {
    const { calls, context, definition, item, state } = harness()
    calls.open.mockRejectedValueOnce(new ArtifactProductFailure(
      'DOCUMENT_UNAVAILABLE',
      'Gateway disconnected',
    ))
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)

    expect(state).toMatchObject({
      resourceBusyKey: '',
      resourceOpenErrorKey: 'attachment:att_fixture',
      resourceOpenErrorMessage: 'This page is temporarily unavailable. Try again.',
    })
    await runtime.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)
    expect(calls.open).toHaveBeenCalledTimes(2)
    expect(state.resourceOpenErrorKey).toBe('')
    expect(state.resourceBusyKey).toBe('')
  })

  it('preserves a localized readonly reason as the inline retry error', async () => {
    const { calls, context, item, state } = harness()
    calls.open.mockRejectedValueOnce(artifactProductClientError('RESOURCE_UNSUPPORTED', {
      reasonCode: 'html_encoding_unsupported',
    }))
    const definition = createWorkbenchResourceCollectionDefinition({
      ...calls,
      pushError: vi.fn(),
      t: key => key === 'workbench.resources.unavailableReasons.htmlEncodingUnsupported'
        ? 'This HTML is not valid UTF-8 and cannot be previewed or edited safely.'
        : key,
    })
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({ type: 'resource-open', payload: attachment }, item)

    expect(state).toMatchObject({
      resourceBusyKey: '',
      resourceOpenErrorKey: 'attachment:att_fixture',
      resourceOpenErrorMessage:
        'This HTML is not valid UTF-8 and cannot be previewed or edited safely.',
    })
  })
})
