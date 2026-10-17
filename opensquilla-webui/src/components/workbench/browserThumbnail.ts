import type { NativeWorkbenchScreenshot } from '@/platform/types'

/** A bounded, memory-only snapshot of an already loaded native page. */
export async function createBrowserThumbnail(screenshot: NativeWorkbenchScreenshot): Promise<string | null> {
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined'
    || screenshot.dataBase64.length > 12 * 1024 * 1024
    || screenshot.width <= 0 || screenshot.height <= 0) return null
  const bytes = Uint8Array.from(atob(screenshot.dataBase64), character => character.charCodeAt(0))
  const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
  try {
    const ratio = Math.min(1, 1280 / bitmap.width, 800 / bitmap.height)
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(bitmap.width * ratio))
    canvas.height = Math.max(1, Math.round(bitmap.height * ratio))
    const context = canvas.getContext('2d')
    if (!context) return null
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.75))
    return blob && blob.size <= 1024 * 1024 ? URL.createObjectURL(blob) : null
  } finally {
    bitmap.close()
  }
}
