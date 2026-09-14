/** A logical HTML member of a preview bundle, never a URL or filesystem grant. */
export function isPreviewPagePath(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 4096
    && value === value.trim() && !/[\\:%?#\u0000-\u001f\u007f]/.test(value)
    && value.split('/').every(part => part !== '' && part !== '.' && part !== '..')
    && /\.(html?|xhtml)$/i.test(value)
}

/** Resolve navigation within a lease's site root, including prefixed remote Web URLs. */
export function previewPagePathFromUrl(
  currentUrl: string,
  lease: { launch_url: string; entrypoint: string; page_path?: string },
): string | undefined {
  try {
    const launch = new URL(lease.launch_url)
    const current = new URL(currentUrl)
    const initial = lease.page_path || lease.entrypoint
    const launchPath = decodeURIComponent(launch.pathname)
    if (current.origin !== launch.origin || !launchPath.endsWith(`/${initial}`)) return
    const prefix = launchPath.slice(0, -initial.length)
    const currentPath = decodeURIComponent(current.pathname)
    if (!currentPath.startsWith(prefix)) return
    const pagePath = currentPath.slice(prefix.length) || lease.entrypoint
    return isPreviewPagePath(pagePath) ? pagePath : undefined
  } catch {
    return undefined
  }
}
