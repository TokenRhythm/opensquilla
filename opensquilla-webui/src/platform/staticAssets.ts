/** Read a same-origin UI asset without treating it as a Gateway API call. */
export async function readStaticJson<T>(path: string): Promise<T | null> {
  let resolved: URL
  try {
    resolved = new URL(path, globalThis.location?.href)
  } catch {
    return null
  }
  if (
    !globalThis.location?.origin
    || resolved.origin !== globalThis.location.origin
    || /^\/api(?:\/|$)/.test(resolved.pathname)
  ) return null
  const response = await fetch(path, { cache: 'no-cache' })
  if (!response.ok) return null
  return await response.json() as T
}
