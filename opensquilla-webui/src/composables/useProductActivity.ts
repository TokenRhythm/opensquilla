import { onMounted, onUnmounted, watch } from 'vue'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { ProductActivityError, type ProductActivity } from '@/modules/productActivity'
import { getPlatform } from '@/platform'
import { onReadinessInvalidated } from './setup/useReadinessSummary'

const RETRY_INTERVAL_MS = 60_000

/** Observe use, not idle time. No input content, targets, or identity leave this boundary. */
export function useProductActivity(
  access: Pick<GatewayAccess, 'isAvailable' | 'isLocalOwner' | 'subscriptionEpoch'>,
  activity: ProductActivity,
): void {
  let mounted = false
  let generation = 0
  let recordedDay = ''
  let attemptedDay = ''
  let retryAfter = 0
  let unsupported = false
  let pending: AbortController | null = null
  let pendingNextDayActivity = false

  function reset() {
    generation += 1
    pending?.abort()
    pending = null
    recordedDay = ''
    attemptedDay = ''
    retryAfter = 0
    unsupported = false
    pendingNextDayActivity = false
  }

  async function observe() {
    if (
      !mounted || !access.isAvailable || !access.isLocalOwner
      || document.visibilityState !== 'visible' || !document.hasFocus()
      || unsupported
    ) return
    const now = Date.now()
    const day = new Date(now).toISOString().slice(0, 10)
    if (pending) {
      if (attemptedDay !== day) pendingNextDayActivity = true
      return
    }
    if (recordedDay === day || (attemptedDay === day && now < retryAfter)) return
    const expectedGeneration = generation
    const controller = new AbortController()
    pending = controller
    attemptedDay = day
    retryAfter = now + RETRY_INTERVAL_MS
    try {
      const recorded = await activity.recordActive(
        getPlatform().capabilities.isDesktop ? 'desktop' : 'web',
        { signal: controller.signal },
      )
      if (generation === expectedGeneration && recorded) recordedDay = day
    } catch (error) {
      if (generation === expectedGeneration && error instanceof ProductActivityError) {
        unsupported = error.code === 'unsupported'
      }
    } finally {
      if (pending === controller) {
        pending = null
        if (pendingNextDayActivity) {
          pendingNextDayActivity = false
          void observe()
        }
      }
    }
  }

  function onInteraction(event: Event) {
    if (event.isTrusted) void observe()
  }
  function onForeground() {
    void observe()
  }

  const stopConnectionWatch = watch(
    () => [access.isAvailable, access.isLocalOwner, access.subscriptionEpoch] as const,
    () => {
      reset()
      void observe()
    },
    { flush: 'sync' },
  )
  const stopSettingsWatch = onReadinessInvalidated(() => {
    // A new policy/profile may have a different analytics identity. Let the
    // next real foreground activity ask the Gateway instead of retaining it.
    reset()
  })

  onMounted(() => {
    mounted = true
    document.addEventListener('visibilitychange', onForeground)
    window.addEventListener('focus', onForeground)
    document.addEventListener('pointerdown', onInteraction, { passive: true, capture: true })
    document.addEventListener('keydown', onInteraction, { passive: true, capture: true })
    void observe()
  })
  onUnmounted(() => {
    mounted = false
    reset()
    stopConnectionWatch()
    stopSettingsWatch()
    document.removeEventListener('visibilitychange', onForeground)
    window.removeEventListener('focus', onForeground)
    document.removeEventListener('pointerdown', onInteraction, true)
    document.removeEventListener('keydown', onInteraction, true)
  })
}
