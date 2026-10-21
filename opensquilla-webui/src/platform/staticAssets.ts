/** Read a build-generated static resource without Gateway credentials.
 * Callers must select URLs from a bundled manifest, never document/user input.
 * This is the static-asset transport boundary, separate from domain HTTP APIs.
 */
export async function readBundledStaticAsset(source: string, signal: AbortSignal): Promise<Uint8Array> {
  signal.throwIfAborted()
  const url = new URL(source, location.href)
  const inline = url.protocol === 'data:'
  if (!inline) {
    const own = new URL(location.href)
    if (url.protocol !== own.protocol || url.host !== own.host
      || !/(?:^|\/)(?:assets|node_modules|@fs)\//.test(url.pathname)
      || /(?:^|\/)api(?:\/|$)/.test(url.pathname)
      || url.username || url.password) throw new Error('Not a bundled static resource')
  }
  const response = await fetch(url.href, { signal, credentials: 'omit', redirect: 'error' })
  if (!response.ok) throw new Error('Static resource unavailable')
  const data = new Uint8Array(await response.arrayBuffer())
  signal.throwIfAborted()
  return data
}
