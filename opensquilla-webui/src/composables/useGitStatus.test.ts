import { effectScope, shallowRef } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useGitStatus } from './useGitStatus'
import type { GitAccess, GitStatusPayload } from '@/modules/git'

function payload(branch: string, overrides: Partial<GitStatusPayload> = {}): GitStatusPayload {
  return {
    available: true,
    workspacePath: `/repos/${branch}`,
    branch,
    detached: false,
    unborn: false,
    headOid: 'a'.repeat(40),
    ahead: 0,
    behind: 0,
    clean: true,
    files: [],
    reason: null,
    ...overrides,
  }
}

function deferredRead() {
  let resolve!: (value: GitStatusPayload) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<GitStatusPayload>((settle, fail) => { resolve = settle; reject = fail })
  return { promise, resolve, reject }
}

function mount(access: GitAccess, workspaceId: string | null) {
  const scope = effectScope()
  const state = scope.run(() => {
    const accessRef = shallowRef<GitAccess | null>(access)
    const workspaceRef = shallowRef<string | null>(workspaceId)
    return { accessRef, workspaceRef, git: useGitStatus({ access: accessRef, workspaceId: workspaceRef }) }
  })
  if (!state) throw new Error('effect scope did not run')
  return { scope, ...state }
}

describe('useGitStatus', () => {
  it('reports the branch, dirty count and HEAD label', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => payload('main', {
        clean: false,
        files: [
          { path: 'src/a.ts', status: 'modified', staged: false },
          { path: 'src/b.ts', status: 'untracked', staged: false },
        ],
      })),
      diff: vi.fn(),
    }
    const { scope, git } = mount(access, null)
    await vi.waitFor(() => expect(git.status.value?.branch).toBe('main'))
    expect(git.loading.value).toBe(false)
    expect(git.error.value).toBeNull()
    expect(git.branchLabel.value).toBe('main')
    expect(git.dirtyCount.value).toBe(2)

    const detachedPayload = payload('', { branch: null, detached: true, headOid: '0123456789abcdef' })
    const detached = mount({ status: vi.fn(async () => detachedPayload), diff: vi.fn() }, null)
    await vi.waitFor(() => expect(detached.git.status.value?.detached).toBe(true))
    expect(detached.git.branchLabel.value).toBe('HEAD@0123456')

    const unbornPayload = payload('', { branch: null, unborn: true, headOid: null, clean: false })
    const unborn = mount({ status: vi.fn(async () => unbornPayload), diff: vi.fn() }, null)
    await vi.waitFor(() => expect(unborn.git.status.value?.unborn).toBe(true))
    expect(unborn.git.branchLabel.value).toBe('')

    detached.scope.stop()
    unborn.scope.stop()
    scope.stop()
  })

  it('drops a late response for a superseded workspace', async () => {
    const first = deferredRead()
    const second = deferredRead()
    const status = vi.fn((options?: { workspaceId?: string | null }) => (
      options?.workspaceId === 'workspace-b' ? second.promise : first.promise
    ))
    const { scope, workspaceRef, git } = mount({ status } as unknown as GitAccess, 'workspace-a')
    expect(status).toHaveBeenCalledTimes(1)
    expect(status.mock.calls[0][0]).toMatchObject({ workspaceId: 'workspace-a' })

    workspaceRef.value = 'workspace-b'
    await vi.waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    // Switching repositories must not keep rendering the previous one.
    expect(git.status.value).toBeNull()

    second.resolve(payload('release'))
    await vi.waitFor(() => expect(git.status.value?.branch).toBe('release'))
    first.resolve(payload('main'))
    await Promise.resolve()
    expect(git.status.value?.branch).toBe('release')
    scope.stop()
  })

  it('clears the previous repository when the next read fails', async () => {
    let fail = false
    const status = vi.fn(async () => {
      if (fail) throw new Error('git unavailable')
      return payload('main')
    })
    const { scope, workspaceRef, git } = mount({ status } as unknown as GitAccess, 'workspace-a')
    await vi.waitFor(() => expect(git.status.value?.branch).toBe('main'))

    fail = true
    workspaceRef.value = 'workspace-b'
    await vi.waitFor(() => expect(git.error.value).toBe('git unavailable'))
    expect(git.status.value).toBeNull()
    expect(git.branchLabel.value).toBe('')
    expect(git.dirtyCount.value).toBe(0)
    expect(git.loading.value).toBe(false)
    scope.stop()
  })

  it('skips reads while inactive and reloads when reactivated', async () => {
    const status = vi.fn(async () => payload('main'))
    const scope = effectScope()
    const state = scope.run(() => {
      const accessRef = shallowRef<GitAccess | null>({ status } as unknown as GitAccess)
      const workspaceRef = shallowRef<string | null>('workspace-a')
      const activeRef = shallowRef(false)
      return { activeRef, git: useGitStatus({ access: accessRef, workspaceId: workspaceRef, active: activeRef }) }
    })
    if (!state) throw new Error('effect scope did not run')
    expect(status).not.toHaveBeenCalled()
    state.activeRef.value = true
    await vi.waitFor(() => expect(state.git.status.value?.branch).toBe('main'))
    expect(status).toHaveBeenCalledTimes(1)
    scope.stop()
  })
})
