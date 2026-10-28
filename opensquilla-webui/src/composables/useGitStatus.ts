import { computed, onScopeDispose, ref, watch, type ComputedRef, type Ref } from 'vue'
import type { GitAccess, GitStatusPayload } from '@/modules/git'

type ReadRef<T> = Ref<T> | ComputedRef<T>

export interface UseGitStatusOptions {
  access: ReadRef<GitAccess | null>
  /** Active project workspace, or null for the session default working directory. */
  workspaceId: ReadRef<string | null>
  /** Optional visibility gate: a hidden panel never reads Git on its own. */
  active?: ReadRef<boolean>
}

export interface GitStatusHandle {
  status: Ref<GitStatusPayload | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  /** Compact label for the topbar branch button. */
  branchLabel: ComputedRef<string>
  /** Number of changed entries; not a line count. */
  dirtyCount: ComputedRef<number>
  /** Read the current repository again, for example after a checkout. */
  refresh: () => Promise<void>
}

function failureMessage(cause: unknown): string {
  if (cause instanceof Error && cause.message) return cause.message
  return typeof cause === 'string' && cause ? cause : 'git.status failed'
}

/**
 * Read-only Git status for one workspace. Reads happen on workspace change and
 * on explicit `refresh()` only; nothing polls.
 */
export function useGitStatus(options: UseGitStatusOptions): GitStatusHandle {
  const status = ref<GitStatusPayload | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /**
   * Monotonic fence. A response that belongs to a superseded read is dropped
   * instead of being applied, so switching projects cannot cross repositories.
   */
  let serial = 0
  let controller: AbortController | null = null
  /** Scope of the last read that started; drives clearing stale repository data. */
  let loadedScope: string | null | undefined

  function cancel(): void {
    serial += 1
    controller?.abort()
    controller = null
  }

  async function read(): Promise<void> {
    cancel()
    const current = serial
    const workspaceId = options.workspaceId.value ?? null
    // Different repository: drop the previous file list before the first await,
    // so neither a slow response nor a failure can show another repo's files.
    if (loadedScope !== workspaceId) {
      loadedScope = workspaceId
      status.value = null
      error.value = null
    }
    const access = options.access.value
    if (!access || options.active?.value === false) {
      loading.value = false
      return
    }
    const abort = new AbortController()
    controller = abort
    loading.value = true
    error.value = null
    try {
      const payload = await access.status({ workspaceId, signal: abort.signal })
      if (current !== serial) return
      status.value = payload
      error.value = null
    } catch (cause) {
      if (current !== serial) return
      // A failed read leaves this workspace unknown, never the previous one.
      status.value = null
      error.value = failureMessage(cause)
    } finally {
      if (current === serial) {
        loading.value = false
        if (controller === abort) controller = null
      }
    }
  }

  watch(
    [
      () => options.workspaceId.value,
      () => options.access.value,
      () => options.active?.value ?? true,
    ],
    () => { void read() },
    { immediate: true },
  )

  onScopeDispose(cancel)

  const branchLabel = computed(() => {
    const current = status.value
    if (!current) return ''
    if (current.detached) return current.headOid ? `HEAD@${current.headOid.slice(0, 7)}` : 'HEAD'
    if (current.unborn && !current.branch) return ''
    return current.branch ?? ''
  })

  const dirtyCount = computed(() => status.value?.files.length ?? 0)

  return {
    status,
    loading,
    error,
    branchLabel,
    dirtyCount,
    refresh: read,
  }
}
