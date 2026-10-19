import { computed, onScopeDispose, ref, watch } from 'vue'

/** Keep transient notifications out of an explicitly protected input surface.
 * Observe only while a notice exists; no notification or recovery state lives here. */
export function useToastPlacement(options: {
  host: () => HTMLElement | null
  visible: () => boolean
  avoidSelector: () => string | undefined
  layoutKey?: () => unknown
}) {
  const offset = ref(0)
  let frame: number | null = null
  let resize: ResizeObserver | null = null
  let mutations: MutationObserver | null = null
  const observed = new Set<Element>()

  function schedule() {
    if (frame !== null) return
    frame = requestAnimationFrame(() => { frame = null; measure() })
  }

  function measure() {
    const host = options.host()
    const selector = options.avoidSelector()
    if (!host || !selector || !options.visible()) { offset.value = 0; return }
    const targets = new Set(document.querySelectorAll<HTMLElement>(selector))
    for (const old of observed) if (!targets.has(old as HTMLElement)) {
      resize?.unobserve(old)
      observed.delete(old)
    }
    for (const target of targets) if (!observed.has(target)) {
      resize?.observe(target)
      observed.add(target)
    }
    const style = getComputedStyle(host)
    const appliedOffset = Number.parseFloat(style.getPropertyValue('--toast-avoidance-offset')) || 0
    const baseBottom = Math.max(0, (Number.parseFloat(style.bottom) || 0) - appliedOffset)
    const gap = Number.parseFloat(style.rowGap) || 8
    const viewportHeight = window.innerHeight
    const bounds = host.getBoundingClientRect()
    // Use the unconstrained stack height, even if avoidance currently limits
    // its viewport. Otherwise a tall stack could oscillate between positions.
    const naturalHeight = Math.min(host.scrollHeight, viewportHeight / 2)
    const obstacles = [...targets].filter(target => getComputedStyle(target).visibility !== 'hidden')
      .map(target => target.getBoundingClientRect()).filter(rect => (
      rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < viewportHeight
      && rect.left < bounds.right && rect.right > bounds.left
    ))
    let bottom = viewportHeight - baseBottom
    // Each intersecting surface moves the stack strictly upward. A later
    // surface can intersect the new position, so examine at most one per pass.
    for (let pass = 0; pass < obstacles.length; pass += 1) {
      const obstacle = obstacles.find(rect => rect.top < bottom && rect.bottom > bottom - naturalHeight)
      if (!obstacle) break
      bottom = obstacle.top - gap
    }
    offset.value = Math.max(0, Math.ceil(viewportHeight - baseBottom - bottom))
  }

  function stop() {
    if (frame !== null) cancelAnimationFrame(frame)
    frame = null
    resize?.disconnect()
    resize = null
    mutations?.disconnect()
    mutations = null
    observed.clear()
    if (typeof window === 'undefined') return
    window.removeEventListener('resize', schedule)
    window.removeEventListener('scroll', schedule, true)
    window.removeEventListener('transitionend', schedule, true)
    window.visualViewport?.removeEventListener('resize', schedule)
    window.visualViewport?.removeEventListener('scroll', schedule)
  }

  watch([options.visible, options.avoidSelector, options.layoutKey ?? (() => null)], () => {
    stop()
    // Keep the final row at its safe position during TransitionGroup's exit;
    // empty queues need no observer, and the next notice measures afresh.
    if (!options.visible()) return
    offset.value = 0
    if (typeof window === 'undefined' || typeof document === 'undefined') return
    if (!options.avoidSelector()) return
    const host = options.host()
    if (typeof ResizeObserver !== 'undefined') {
      resize = new ResizeObserver(schedule)
      if (host) resize.observe(host)
    }
    if (typeof MutationObserver !== 'undefined') {
      mutations = new MutationObserver(records => {
        // Sidebar/workbench changes can move a fixed-width composer without
        // resizing it. Ignore our own placement styles to avoid feedback.
        const currentHost = options.host()
        if (records.some(record => !currentHost?.contains(record.target))) schedule()
      })
      mutations.observe(document.body, { childList: true, subtree: true,
        attributes: true, attributeFilter: ['class', 'style', 'hidden'] })
    }
    window.addEventListener('resize', schedule)
    window.addEventListener('scroll', schedule, true)
    window.addEventListener('transitionend', schedule, true)
    window.visualViewport?.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('scroll', schedule)
    schedule()
  }, { immediate: true, flush: 'post' })
  onScopeDispose(stop)

  return computed(() => ({ '--toast-avoidance-offset': `${offset.value}px` }))
}
