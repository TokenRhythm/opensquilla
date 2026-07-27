import type { RouteLocationNormalized } from 'vue-router'

// vue-router's savedPosition only records window scroll, but the per-route
// scroll region here is the `<main id="content">` element (base.css makes
// .content the overflow-y:auto container, not window). This helper captures
// .content.scrollTop per route on leave and restores it on back/forward, with
// scroll-to-top as the default for fresh navigations.

const CONTENT_ID = 'content'
const offsets = new Map<string, number>()

function contentEl(): HTMLElement | null {
  return document.getElementById(CONTENT_ID)
}

/** Record the current scroll offset of the leaving route. Wire into beforeEach. */
export function captureContentScroll(from: RouteLocationNormalized): void {
  const el = contentEl()
  if (el && from.fullPath) offsets.set(from.fullPath, el.scrollTop)
}

/**
 * Restore .content's scroll on back/forward; scroll to top on a fresh nav. The
 * new view may still be mounting (the route fade is out-in), so the offset is
 * applied on the next frame. Returns false so the router leaves window alone.
 */
export function contentScrollBehavior(
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  savedPosition: { left: number; top: number } | null,
): false {
  // Same-path navigations without a saved offset are query-only state syncs
  // (drill-in tabs, editor mode, compose) — the view manages its own section
  // scrolling there, and a scheduled scroll-to-top would cancel an in-flight
  // scrollIntoView and yank the page. Do nothing at all.
  if (!savedPosition && to.path === from.path) return false
  const target = savedPosition ? (offsets.get(to.fullPath) ?? 0) : 0
  const apply = () => {
    const el = contentEl()
    if (el) el.scrollTop = target
  }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(apply)
  else apply()
  return false
}
