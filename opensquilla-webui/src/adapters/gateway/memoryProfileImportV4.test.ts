import { describe, expect, it, vi } from 'vitest'

import type { MemoryImportPreview, MemoryImportRecent } from '@/modules/memoryProfileImport'
import { createV4MemoryProfileImport } from './memoryProfileImportV4'

function harness(responses: Record<string, unknown>) {
  const request = vi.fn(async (method: string) => {
    const value = responses[method]
    if (value instanceof Error) throw value
    return value
  })
  const markUnsupported = vi.fn()
  return {
    request,
    markUnsupported,
    transport: {
      request,
      ready: vi.fn(async () => undefined),
      supports: vi.fn(() => true),
      markUnsupported,
    },
  }
}

function job() {
  return {
    schemaVersion: 1,
    jobId: 'job-1',
    batchId: 'batch-1',
    status: 'analyzing',
    stage: 'model',
    provider: 'provider-a',
    model: 'model-a',
    startedAt: '2026-09-01T00:00:00Z',
    canRetry: false,
  }
}

function preview(): MemoryImportPreview {
  return {
    schemaVersion: 1,
    previewId: 'preview-1',
    batchId: 'batch-1',
    candidateHash: 'sha256:abc',
    provider: 'provider-a',
    model: 'model-a',
    summary: ['one change'],
    decisionCounts: { applied: 1, duplicate: 0, unresolved: 0 },
    files: [{
      target: 'MEMORY',
      displayName: 'MEMORY.md',
      relativePath: 'MEMORY.md',
      status: 'modified',
      additions: 1,
      deletions: 0,
      diff: '+memory',
    }],
  }
}

describe('v4 MemoryProfileImport Adapter', () => {
  it('normalizes compatibility aliases and owns start request details', async () => {
    const h = harness({
      'memory.import.info': {
        schemaVersion: 1,
        available: true,
        provider: 'provider-a',
        model: 'model-a',
        isLoopback: true,
        maxRawBytes: 42,
      },
      'memory.import.start': job(),
    })
    const adapter = createV4MemoryProfileImport(
      h.transport as Parameters<typeof createV4MemoryProfileImport>[0],
    )

    await expect(adapter.info()).resolves.toMatchObject({ isLocal: true, maxInputBytes: 42 })
    await expect(adapter.start({
      rawText: 'synthetic export',
      locale: 'zh-CN',
      exportPromptVersion: 'v1',
      clientRequestId: 'request-1',
      expected: { provider: 'provider-a', model: 'model-a', isLocal: true },
    })).resolves.toMatchObject({ jobId: 'job-1', stage: 'model' })

    expect(h.request).toHaveBeenLastCalledWith('memory.import.start', {
      schemaVersion: 1,
      agentId: 'main',
      rawText: 'synthetic export',
      uiLocale: 'zh-CN',
      exportPromptVersion: 'v1',
      clientRequestId: 'request-1',
      expectedProvider: 'provider-a',
      expectedModel: 'model-a',
      expectedIsLocal: true,
    }, expect.any(Object))
  })

  it('projects apply and both undo outcomes into domain results', async () => {
    const importPreview = preview()
    const recent: MemoryImportRecent = {
      receiptId: 'receipt-1',
      batchId: 'batch-1',
      appliedAt: '2026-09-01T00:00:00Z',
      summary: ['one change'],
      provider: 'provider-a',
      model: 'model-a',
      status: 'applied',
      indexStatus: 'ready',
      fileCount: 1,
      targets: ['MEMORY'],
    }
    const h = harness({
      'memory.import.apply': {
        schemaVersion: 1,
        recentImport: recent,
      },
      'memory.import.undo': {
        schemaVersion: 1,
        status: 'reviewRequired',
        preview: importPreview,
      },
    })
    const adapter = createV4MemoryProfileImport(
      h.transport as Parameters<typeof createV4MemoryProfileImport>[0],
    )

    await expect(adapter.apply({
      preview: importPreview,
      idempotencyKey: 'apply-1',
      kind: 'import',
    })).resolves.toEqual(recent)
    await expect(adapter.undo({
      recent,
      clientRequestId: 'undo-1',
      expected: { provider: 'provider-a', model: 'model-a', isLocal: true },
    })).resolves.toEqual({ kind: 'review-required', preview: importPreview })
  })

  it('marks a missing legacy Gateway capability unsupported', async () => {
    const missing = Object.assign(new Error('Method not found'), { code: 'METHOD_NOT_FOUND' })
    const h = harness({ 'memory.import.info': missing })
    const adapter = createV4MemoryProfileImport(
      h.transport as Parameters<typeof createV4MemoryProfileImport>[0],
    )

    await expect(adapter.info()).rejects.toMatchObject({
      name: 'MemoryProfileImportError',
      kind: 'unsupported',
      code: 'METHOD_NOT_FOUND',
    })
    expect(h.markUnsupported).toHaveBeenCalledWith('memory.import.info')
  })
})
