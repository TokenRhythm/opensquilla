import type { WebContents } from 'electron'

/** Only a committed main document change or a lost renderer retires its offer. */
export function bindAccountPrimaryActivationLifecycle(
  contents: Pick<WebContents, 'on'>,
  invalidate: () => void,
): void {
  // did-navigate excludes SPA/subframe navigation and follows cancellable guards.
  contents.on('did-navigate', invalidate)
  contents.on('render-process-gone', invalidate)
  contents.on('destroyed', invalidate)
}
