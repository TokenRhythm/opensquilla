export async function copyTextWithFallback(text: string): Promise<void> {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // writeText can reject on focus loss, permission denial, or insecure
      // contexts; fall through to the legacy textarea path before giving up.
    }
  }

  const previousFocus = document.activeElement
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  try {
    textarea.select()
    const ok = document.execCommand('copy')
    if (!ok) throw new Error('Copy failed')
  } finally {
    // Chromium focuses the selected textarea. Removing it alone leaves focus
    // on body, breaking keyboard navigation after a menu action.
    const restoreFocus = document.activeElement === textarea
    textarea.remove()
    if (restoreFocus && previousFocus instanceof HTMLElement && previousFocus.isConnected) {
      previousFocus.focus({ preventScroll: true })
    }
  }
}

// Extract the filename from a Content-Disposition header value. Prefers the
// RFC 5987 `filename*=charset''percent-encoded` form (it carries non-ASCII
// names), then falls back to the plain quoted/unquoted `filename=` parameter.
// Returns null when the header is absent or carries no usable filename, and
// strips any path segments so a hostile header cannot suggest a traversal.
export function filenameFromContentDisposition(header: string | null): string | null {
  if (!header) return null
  let name: string | null = null
  const extended = header.match(/filename\*\s*=\s*[^']*'[^']*'([^;]+)/i)
  if (extended) {
    try {
      name = decodeURIComponent(extended[1].trim())
    } catch {
      name = null
    }
  }
  if (!name) {
    const plain = header.match(/filename\s*=\s*(?:"([^"]*)"|([^;]+))/i)
    if (plain) name = (plain[1] ?? plain[2] ?? '').trim()
  }
  if (!name) return null
  const basename = name.split(/[/\\]/).pop() ?? ''
  return basename && basename !== '.' && basename !== '..' ? basename : null
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export function downloadText(filename: string, mime: string, content: string): void {
  downloadBlob(new Blob([content], { type: mime }), filename)
}

// True on Apple platforms (macOS, iPadOS, iOS). Keyboard shortcuts use this to
// bind their primary chord to the Cmd key and leave Ctrl to macOS' system-wide
// emacs-style text-editing bindings (e.g. Ctrl+K = kill-to-end-of-line) rather
// than stealing it. userAgent is matched instead of the deprecated
// navigator.platform; "Macintosh" satisfies the /Mac/ test.
export function isMacPlatform(): boolean {
  return typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/i.test(navigator.userAgent)
}

// Whether this browser can put an image on the clipboard. The async Clipboard
// API plus the ClipboardItem constructor are both required and only exist in a
// secure context (localhost counts); older Firefox/Safari lack ClipboardItem.
// Evaluated once at call sites so the Copy affordance can be hidden when false.
export function shareCopyImageSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.clipboard &&
    typeof navigator.clipboard.write === 'function' &&
    typeof ClipboardItem !== 'undefined'
  )
}

// Copy a PNG (or any image) blob to the clipboard. Returns false instead of
// throwing when the API is missing or the write is rejected (permission, focus
// loss, transient failure), so callers can fall back to a download with a toast.
export async function copyImageToClipboard(blob: Blob): Promise<boolean> {
  if (!shareCopyImageSupported()) return false
  try {
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })])
    return true
  } catch {
    return false
  }
}
