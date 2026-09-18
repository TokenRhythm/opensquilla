const MAX_PENDING = 32

export function createPendingRequestStore<T extends object>(
  storageKey: string,
  isPending: (value: unknown) => value is T,
) {
  const fallback = new Map<string, T>()
  let memoryAuthoritative = false

  function read(): Map<string, T> {
    if (memoryAuthoritative) return fallback
    try {
      const raw: unknown = JSON.parse(sessionStorage.getItem(storageKey) || '[]')
      if (!Array.isArray(raw)) return fallback
      const entries = raw.filter((entry): entry is [string, T] => (
        Array.isArray(entry) && typeof entry[0] === 'string' && isPending(entry[1])
      )).slice(-MAX_PENDING)
      fallback.clear()
      for (const [key, value] of entries) fallback.set(key, value)
      return fallback
    } catch {
      return fallback
    }
  }

  function save(requests: Map<string, T>) {
    while (requests.size > MAX_PENDING) requests.delete(requests.keys().next().value!)
    const entries = [...requests]
    fallback.clear()
    for (const [key, value] of entries) fallback.set(key, value)
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(entries))
    } catch {
      // A failed write leaves stale storage, including requests just forgotten.
      // Memory remains authoritative until module unload; reload recovery needs
      // a successful storage write.
      memoryAuthoritative = true
    }
  }

  function recover(identity: string, create: () => T): T {
    const requests = read()
    const previous = requests.get(identity)
    if (previous) return previous
    const pending = create()
    requests.set(identity, pending)
    save(requests)
    return pending
  }

  function forgetMatching(matches: (identity: string) => boolean) {
    const requests = read()
    for (const identity of requests.keys()) {
      if (matches(identity)) requests.delete(identity)
    }
    save(requests)
  }

  return { recover, forget: (identity: string) => forgetMatching(key => key === identity), forgetMatching }
}
