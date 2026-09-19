import { describe, expect, it, vi } from 'vitest'
import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { createV4WorkspaceChanges } from './workspaceChangesV4'

/**
 * The transport request is generic, and a `vi.fn` mock keeps its own concrete
 * call signature; the cast re-states the generic contract so call assertions
 * still see every recorded argument.
 */
function transport(payload: unknown) {
  return {
    request: vi.fn(async <T = unknown>(
      _method: string,
      _params?: Record<string, unknown>,
      _options?: RpcCallOptions,
    ): Promise<T> => payload as T) as unknown as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>,
  }
}

function statusResult(overrides: Record<string, unknown> = {}) {
  return {
    available: true,
    availabilityReason: null,
    branch: 'main',
    detached: false,
    upstream: null,
    ahead: 0,
    behind: 0,
    totalCount: 1,
    truncated: false,
    addedLines: 1,
    removedLines: 1,
    entries: [
      {
        path: 'src/a.ts',
        previousPath: null,
        changeType: 'modified',
        staged: false,
        unstaged: true,
        addedLines: 1,
        removedLines: 1,
      },
    ],
    ...overrides,
  }
}

function diffResult(overrides: Record<string, unknown> = {}) {
  return {
    path: 'src/a.ts',
    staged: false,
    text: '@@ -1 +1 @@\n-a\n+b\n',
    truncated: false,
    binary: false,
    ...overrides,
  }
}

describe('createV4WorkspaceChanges', () => {
  it('projects the validated working-tree result', async () => {
    const { request } = transport(statusResult())
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.readChanges('workspace-1')).resolves.toEqual({
      available: true,
      availabilityReason: null,
      branch: 'main',
      detached: false,
      upstream: null,
      ahead: 0,
      behind: 0,
      totalCount: 1,
      truncated: false,
      addedLines: 1,
      removedLines: 1,
      entries: [
        {
          path: 'src/a.ts',
          previousPath: null,
          changeType: 'modified',
          staged: false,
          unstaged: true,
          addedLines: 1,
          removedLines: 1,
        },
      ],
    })
    expect(request).toHaveBeenCalledWith(
      'workspaces.git.status',
      { workspaceId: 'workspace-1' },
      undefined,
    )
  })

  it('keeps an unavailable workspace distinct from an empty change list', async () => {
    const { request } = transport(statusResult({
      available: false,
      availabilityReason: 'not_repository',
      branch: null,
      totalCount: 0,
      addedLines: 0,
      removedLines: 0,
      entries: [],
    }))
    const changes = createV4WorkspaceChanges({ request })

    const result = await changes.readChanges('workspace-1')

    expect(result.available).toBe(false)
    expect(result.availabilityReason).toBe('not_repository')
    expect(result.entries).toEqual([])
  })

  it('rejects a response that violates the generated Contract', async () => {
    const { request } = transport({ available: true })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.readChanges('workspace-1'))
      .rejects.toThrow('workspaces.git.status returned an invalid response')
  })

  it('sends the staged flag only when the caller sets it', async () => {
    const { request } = transport(diffResult())
    const changes = createV4WorkspaceChanges({ request })

    await changes.readDiff({ workspaceId: 'workspace-1', path: 'src/a.ts' })
    await changes.readDiff({ workspaceId: 'workspace-1', path: 'src/a.ts', staged: true })

    expect(request).toHaveBeenNthCalledWith(
      1,
      'workspaces.git.diff',
      { workspaceId: 'workspace-1', path: 'src/a.ts' },
      undefined,
    )
    expect(request).toHaveBeenNthCalledWith(
      2,
      'workspaces.git.diff',
      { workspaceId: 'workspace-1', path: 'src/a.ts', staged: true },
      undefined,
    )
  })

  it('rejects an invalid diff response instead of returning a partial model', async () => {
    const { request } = transport({ path: 'src/a.ts', text: 'x' })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.readDiff({ workspaceId: 'workspace-1', path: 'src/a.ts' }))
      .rejects.toThrow('workspaces.git.diff returned an invalid response')
  })

  it('sends a stage request and returns the validated acknowledgement', async () => {
    const { request } = transport({ staged: true, affectedPaths: ['src/a.ts'] })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.stagePaths({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts'],
      staged: true,
    })).resolves.toEqual({ staged: true, affectedPaths: ['src/a.ts'] })

    expect(request).toHaveBeenCalledWith(
      'workspaces.git.stage',
      { workspaceId: 'workspace-1', staged: true, paths: ['src/a.ts'] },
      undefined,
    )
  })

  it('refuses to send a stage request that cannot satisfy its Contract', async () => {
    const { request } = transport({ staged: true, affectedPaths: [] })
    const changes = createV4WorkspaceChanges({ request })

    // An empty path list is a caller bug, and the Contract's `minItems: 1`
    // rejects it here rather than at the Gateway.
    await expect(changes.stagePaths({
      workspaceId: 'workspace-1',
      paths: [],
      staged: true,
    })).rejects.toThrow('workspaces.git.stage received params that violate its contract')
    expect(request).not.toHaveBeenCalled()
  })

  it('rejects an invalid stage acknowledgement', async () => {
    const { request } = transport({ staged: true })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.stagePaths({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts'],
      staged: true,
    })).rejects.toThrow('workspaces.git.stage returned an invalid response')
  })

  it('sends a draft request and returns the validated draft', async () => {
    const { request } = transport({ subject: 'Add the retry budget', body: 'Why it changed.' })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.draftCommitMessage({ workspaceId: 'workspace-1' })).resolves.toEqual({
      subject: 'Add the retry budget',
      body: 'Why it changed.',
    })

    // Only the workspace is sent: the rule that shapes the message is an
    // application setting, not a field this panel owns.
    expect(request).toHaveBeenCalledWith(
      'workspaces.git.commitMessage.draft',
      { workspaceId: 'workspace-1' },
      undefined,
    )
  })

  it('rejects a draft that is missing its body field', async () => {
    const { request } = transport({ subject: 'Add the retry budget' })
    const changes = createV4WorkspaceChanges({ request })

    await expect(changes.draftCommitMessage({ workspaceId: 'workspace-1' }))
      .rejects.toThrow('workspaces.git.commitMessage.draft returned an invalid response')
  })

  it('forwards the abort signal as a rejecting call option', async () => {
    const { request } = transport(statusResult())
    const changes = createV4WorkspaceChanges({ request })
    const controller = new AbortController()

    await changes.readChanges('workspace-1', { signal: controller.signal })

    expect(request).toHaveBeenCalledWith(
      'workspaces.git.status',
      { workspaceId: 'workspace-1' },
      { signal: controller.signal, abortAction: 'reject', timeoutAction: 'reject' },
    )
  })
})
