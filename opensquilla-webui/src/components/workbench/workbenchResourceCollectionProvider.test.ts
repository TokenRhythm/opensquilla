import { describe, expect, it, vi } from 'vitest'
import type { WorkbenchResource } from '@/types/workbenchResources'
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
  capabilities: { preview: true, download: true, edit: true, publish: false },
  relations: {},
}

function harness() {
  const calls = {
    download: vi.fn(async () => undefined),
    importDocument: vi.fn(async () => undefined),
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
  it('routes preview without importing the attachment', async () => {
    const { calls, context, definition, item } = harness()
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({ type: 'resource-preview', payload: attachment }, item)

    expect(calls.open).toHaveBeenCalledWith(attachment, item)
    expect(calls.importDocument).not.toHaveBeenCalled()
  })

  it('imports only after the explicit edit action', async () => {
    const { calls, context, definition, item, state } = harness()
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({ type: 'resource-import', payload: attachment }, item)

    expect(calls.importDocument).toHaveBeenCalledWith(attachment, item)
    expect(state.resourceBusyKey).toBe('')
  })
})
