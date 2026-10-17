/** A small renderer-owned cache of images already produced by a preview.
 * Never starts a render, fetch, navigation or screenshot request.
 */
export function createTabThumbnailCache() {
  const images = new Map<string, { fingerprint: string; url: string; bytes: number }>()
  let bytes = 0
  function remove(id: string) {
    const value = images.get(id)
    if (!value) return
    URL.revokeObjectURL(value.url)
    bytes -= value.bytes
    images.delete(id)
  }
  function adopt(id: string, fingerprint: string, url: string, size: number) {
    remove(id)
    images.set(id, { fingerprint, url, bytes: size })
    bytes += size
    while (images.size > 16 || bytes > 4 * 1024 * 1024) remove(images.keys().next().value!)
  }
  return {
    remove,
    /** Takes ownership of a bounded bitmap URL created by the native snapshot encoder. */
    adoptNative(id: string, fingerprint: string, url: string) { adopt(id, fingerprint, url, 1024 * 1024) },
    get(id: string, fingerprint: string): string | undefined {
      const value = images.get(id)
      return value?.fingerprint === fingerprint ? value.url : undefined
    },
    save(id: string, fingerprint: string, blob: Blob): void {
      if (!/^image\/(png|jpeg|webp)$/.test(blob.type) || blob.size > 512 * 1024) return
      adopt(id, fingerprint, URL.createObjectURL(blob), blob.size)
    },
    prune(ids: ReadonlySet<string>) {
      for (const id of images.keys()) if (!ids.has(id)) remove(id)
    },
    clear() { for (const id of images.keys()) remove(id) },
  }
}
