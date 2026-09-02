import { computed, ref, watch, type Ref } from 'vue'

import {
  isCronSessionKey,
  type SessionDirectory,
  type SessionItem,
} from '@/modules/sessionDirectory'

const LOOKUP_PAGE_SIZE = 200

export interface UseChatSessionInteractivityOptions {
  sessionKey: Readonly<Ref<string>>
  directory: SessionDirectory
  knownSessions?: Readonly<Ref<readonly SessionItem[]>>
  resolveEnabled?: Readonly<Ref<boolean>>
}

/**
 * Resolves the selected session's authoritative mutation policy. Canonical
 * Cron keys remain the compatibility fallback; noncanonical legacy runs are
 * blocked from the first lookup tick until their directory row is known.
 */
export function useChatSessionInteractivity(options: UseChatSessionInteractivityOptions) {
  const authority = ref<SessionItem | null>(null)
  const resolvingKey = ref('')
  const unresolvedKey = ref('')
  let generation = 0
  let controller: AbortController | null = null

  function knownSession(key: string): SessionItem | null {
    return options.knownSessions?.value.find(item => item.key === key) || null
  }

  async function resolve(key: string, attempt: number, signal: AbortSignal): Promise<void> {
    let cursor: string | undefined
    const seenCursors = new Set<string>()
    const markUnresolved = () => {
      if (!signal.aborted && attempt === generation) unresolvedKey.value = key
    }
    try {
      while (!signal.aborted && attempt === generation) {
        const page = await options.directory.listPage({
          limit: LOOKUP_PAGE_SIZE,
          cursor,
          signal,
        })
        if (signal.aborted || attempt !== generation) return
        const match = page.items.find(item => item.key === key)
        if (match) {
          authority.value = match
          unresolvedKey.value = ''
          return
        }
        const next = page.nextCursor || ''
        if (!page.hasMore || !next || next === cursor || seenCursors.has(next)) {
          markUnresolved()
          return
        }
        seenCursors.add(next)
        cursor = next
      }
    } catch (error) {
      if (!signal.aborted && attempt === generation) {
        markUnresolved()
        console.warn(
          'Selected session policy lookup failed:',
          error instanceof Error ? error.message : error,
        )
      }
    } finally {
      if (attempt === generation) resolvingKey.value = ''
    }
  }

  function select(key: string): void {
    generation += 1
    controller?.abort()
    controller = null
    authority.value = null
    resolvingKey.value = ''
    unresolvedKey.value = ''
    if (!key || isCronSessionKey(key)) return
    const known = knownSession(key)
    if (known) {
      authority.value = known
      return
    }
    if (options.resolveEnabled?.value === false) return
    const attempt = generation
    controller = new AbortController()
    resolvingKey.value = key
    void resolve(key, attempt, controller.signal)
  }

  const stopSessionWatch = watch(options.sessionKey, select, { immediate: true })
  const stopKnownSessionsWatch = options.knownSessions
    ? watch(options.knownSessions, () => {
        const key = options.sessionKey.value
        const known = knownSession(key)
        if (!key || !known || authority.value === known) return
        generation += 1
        controller?.abort()
        controller = null
        resolvingKey.value = ''
        unresolvedKey.value = ''
        authority.value = known
      })
    : () => {}
  const stopResolveEnabledWatch = options.resolveEnabled
    ? watch(options.resolveEnabled, enabled => {
        if (enabled) select(options.sessionKey.value)
      })
    : () => {}

  const isCronSession = computed(() => {
    const key = options.sessionKey.value
    if (isCronSessionKey(key)) return true
    return authority.value?.key === key
      && authority.value.sessionKindAuthoritative === true
      && authority.value.sessionKind === 'cron'
  })
  const isNoninteractiveSession = computed(() => (
    authority.value?.key === options.sessionKey.value
    && authority.value.interactive === false
  ))
  const policyPending = computed(() => (
    Boolean(options.sessionKey.value)
    && resolvingKey.value === options.sessionKey.value
  ))
  const policyUnavailable = computed(() => (
    Boolean(options.sessionKey.value)
    && unresolvedKey.value === options.sessionKey.value
  ))
  const turnActionsBlocked = computed(() => (
    isCronSession.value
    || isNoninteractiveSession.value
    || policyPending.value
    || policyUnavailable.value
  ))

  function dispose(): void {
    generation += 1
    controller?.abort()
    controller = null
    resolvingKey.value = ''
    unresolvedKey.value = ''
    stopSessionWatch()
    stopKnownSessionsWatch()
    stopResolveEnabledWatch()
  }

  return {
    isCronSession,
    isNoninteractiveSession,
    policyPending,
    policyUnavailable,
    turnActionsBlocked,
    dispose,
  }
}
