import {
  nextTick,
  onMounted,
  onUnmounted,
  readonly,
  ref,
  watch,
  type Ref,
} from 'vue'
import {
  resolveSystemHeaderLayout,
  type SystemHeaderLayout,
} from '@/utils/headerLayout'

const COARSE_POINTER_QUERY = '(pointer: coarse)'

export interface SystemHeaderLayoutOptions {
  target: Ref<HTMLElement | null>
  active: Readonly<Ref<boolean>>
  pressureCount: Readonly<Ref<number>>
}

/**
 * Resolve the chat-only system-control layout from the full topbar width.
 *
 * This observer deliberately remains independent from the session header's
 * observer. System controls can therefore collapse without feeding their new
 * width back into their own state machine, while the route-owned session area
 * still responds naturally to the space left in the App grid.
 */
export function useSystemHeaderLayout(options: SystemHeaderLayoutOptions) {
  const layout = ref<SystemHeaderLayout>('wide')
  let resizeObserver: ResizeObserver | null = null
  let coarsePointerMedia: MediaQueryList | null = null
  let layoutFrame: number | null = null
  let hasMeasuredLayout = false

  function syncLayout() {
    if (!options.active.value) return
    const topbar = options.target.value
    if (!topbar) return

    const next = resolveSystemHeaderLayout({
      topbarWidth: topbar.getBoundingClientRect().width,
      previousLayout: hasMeasuredLayout ? layout.value : null,
      mobile: window.innerWidth <= 768,
      coarseOnly: coarsePointerMedia?.matches ?? false,
      pressureCount: options.pressureCount.value,
    })
    hasMeasuredLayout = true
    layout.value = next
  }

  function scheduleLayout() {
    if (!options.active.value || layoutFrame != null) return
    layoutFrame = window.requestAnimationFrame(() => {
      layoutFrame = null
      syncLayout()
    })
  }

  watch(options.pressureCount, scheduleLayout)
  watch(options.active, active => {
    // Non-chat topbars have different geometry and must never seed the chat
    // hysteresis history when the route changes back.
    hasMeasuredLayout = false
    if (active) void nextTick(scheduleLayout)
  })

  onMounted(() => {
    coarsePointerMedia = typeof window.matchMedia === 'function'
      ? window.matchMedia(COARSE_POINTER_QUERY)
      : null
    coarsePointerMedia?.addEventListener('change', scheduleLayout)

    if (typeof ResizeObserver !== 'undefined' && options.target.value) {
      resizeObserver = new ResizeObserver(scheduleLayout)
      resizeObserver.observe(options.target.value)
    }
    window.addEventListener('resize', scheduleLayout)
    syncLayout()
  })

  onUnmounted(() => {
    resizeObserver?.disconnect()
    resizeObserver = null
    coarsePointerMedia?.removeEventListener('change', scheduleLayout)
    coarsePointerMedia = null
    window.removeEventListener('resize', scheduleLayout)
    if (layoutFrame != null) {
      window.cancelAnimationFrame(layoutFrame)
      layoutFrame = null
    }
  })

  return readonly(layout)
}
