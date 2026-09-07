import type {
  SessionReadHistoryDirection,
  SessionReadHistoryPage,
  SessionReadMetadata,
  SessionReadPort,
  SessionReadPortHistoryRequest,
  SessionReadPortLease,
  SessionReadPortLive,
  SessionReadPortOpenRequest,
} from '@/modules/sessionReadLifecycle'

export interface InMemorySessionReadHistoryFixture {
  readonly direction: SessionReadHistoryDirection
  readonly cursor?: string | null
  readonly page?: SessionReadHistoryPage
  readonly error?: unknown
  readonly delayMs?: number
}

export interface InMemorySessionReadFixture {
  readonly sessionKey: string
  readonly live: SessionReadPortLive
  readonly metadata: SessionReadMetadata
  readonly latestHistory: SessionReadHistoryPage
  readonly history?: readonly InMemorySessionReadHistoryFixture[]
  readonly retryMetadata?: SessionReadMetadata
  readonly criticalDelayMs?: number
  readonly liveDelayMs?: number
  readonly metadataDelayMs?: number
  readonly historyDelayMs?: number
  readonly retryMetadataDelayMs?: number
  readonly criticalError?: unknown
  readonly liveError?: unknown
  readonly metadataError?: unknown
  readonly historyError?: unknown
  readonly retryMetadataError?: unknown
}

export interface InMemorySessionReadOpenRecord {
  readonly sessionKey: string
  readonly includeInitialHistory: boolean
  readonly resumeFrom: SessionReadPortOpenRequest['resumeFrom']
}

export interface InMemorySessionReadHistoryRecord {
  readonly sessionKey: string
  readonly direction: SessionReadHistoryDirection
  readonly cursor: string | null
  readonly limit: number
  readonly budgetMs?: number
  readonly deadlineAt?: number
  readonly signal: AbortSignal
}

function abortError(): Error {
  const error = new Error('The in-memory session read was aborted.')
  error.name = 'AbortError'
  return error
}

function throwFixture(error: unknown): never {
  throw error instanceof Error ? error : new Error(String(error))
}

function waitForDelay(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError())
  if (delayMs <= 0) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, delayMs)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

async function fixturePhase<T>(
  value: T,
  delayMs: number,
  error: unknown,
  signal: AbortSignal,
): Promise<T> {
  await waitForDelay(delayMs, signal)
  if (signal.aborted) throw abortError()
  if (error !== undefined) throwFixture(error)
  return value
}

/** Deterministic test Adapter; it owns no event source or runtime state. */
export class InMemorySessionReadPortAdapter implements SessionReadPort {
  private readonly fixtures = new Map<string, InMemorySessionReadFixture>()
  private readonly opens: InMemorySessionReadOpenRecord[] = []
  private readonly historyReads: InMemorySessionReadHistoryRecord[] = []
  private activeLeases = 0
  private closes = 0
  private metadataRetries = 0

  constructor(fixtures: readonly InMemorySessionReadFixture[]) {
    for (const fixture of fixtures) this.fixtures.set(fixture.sessionKey, fixture)
  }

  get openRecords(): readonly InMemorySessionReadOpenRecord[] {
    return Object.freeze([...this.opens])
  }

  get historyRecords(): readonly InMemorySessionReadHistoryRecord[] {
    return Object.freeze([...this.historyReads])
  }

  get activeLeaseCount(): number {
    return this.activeLeases
  }

  get closeCount(): number {
    return this.closes
  }

  get metadataRetryCount(): number {
    return this.metadataRetries
  }

  open(request: SessionReadPortOpenRequest): SessionReadPortLease {
    this.opens.push(Object.freeze({
      sessionKey: request.sessionKey,
      includeInitialHistory: request.includeInitialHistory,
      resumeFrom: Object.freeze({ ...request.resumeFrom }),
    }))
    this.activeLeases += 1
    const fixture = this.fixtures.get(request.sessionKey)
    let closed = false
    const missing = new Error(`Unknown in-memory session: ${request.sessionKey}`)

    const criticalRequestsQueued = fixturePhase(
      undefined,
      fixture?.criticalDelayMs ?? 0,
      fixture ? fixture.criticalError : missing,
      request.signal,
    )
    const live = fixturePhase(
      fixture?.live as SessionReadPortLive,
      fixture?.liveDelayMs ?? 0,
      fixture ? fixture.liveError : missing,
      request.signal,
    )
    const metadata = fixturePhase(
      fixture?.metadata as SessionReadMetadata,
      fixture?.metadataDelayMs ?? 0,
      fixture ? fixture.metadataError : missing,
      request.signal,
    )
    void criticalRequestsQueued.catch(() => {})
    void live.catch(() => {})
    void metadata.catch(() => {})

    const readHistory = async (
      historyRequest: SessionReadPortHistoryRequest,
    ): Promise<SessionReadHistoryPage> => {
      if (closed || historyRequest.signal.aborted) throw abortError()
      this.historyReads.push(Object.freeze({
        sessionKey: request.sessionKey,
        direction: historyRequest.direction,
        cursor: historyRequest.direction === 'latest' ? null : historyRequest.cursor,
        limit: historyRequest.limit,
        budgetMs: historyRequest.budgetMs,
        deadlineAt: historyRequest.deadlineAt,
        signal: historyRequest.signal,
      }))
      if (!fixture) throw missing
      const pageFixture = fixture.history?.find(candidate => (
        candidate.direction === historyRequest.direction
        && (candidate.cursor ?? null) === (
          historyRequest.direction === 'latest' ? null : historyRequest.cursor
        )
      ))
      return fixturePhase(
        pageFixture?.page ?? fixture.latestHistory,
        pageFixture?.delayMs ?? fixture.historyDelayMs ?? 0,
        pageFixture?.error ?? fixture.historyError,
        historyRequest.signal,
      )
    }

    const retryMetadata = async (): Promise<SessionReadMetadata> => {
      if (closed || request.signal.aborted) throw abortError()
      this.metadataRetries += 1
      if (!fixture) throw missing
      return fixturePhase(
        fixture.retryMetadata ?? fixture.metadata,
        fixture.retryMetadataDelayMs ?? 0,
        fixture.retryMetadataError,
        request.signal,
      )
    }

    const close = async (): Promise<void> => {
      if (closed) return
      closed = true
      this.activeLeases -= 1
      this.closes += 1
    }

    return Object.freeze({
      criticalRequestsQueued,
      live,
      metadata,
      readHistory,
      retryMetadata,
      close,
    })
  }
}
