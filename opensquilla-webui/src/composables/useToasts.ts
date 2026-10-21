import { ref } from 'vue'

export type ToastTone = 'info' | 'ok' | 'warn' | 'danger'

export type ToastActionResult = void | boolean | {
  handled: false
  message?: string
  tone?: ToastTone
  removeAction?: boolean
}

export interface ToastAction {
  label: string
  onClick: () => ToastActionResult | Promise<ToastActionResult>
}

export interface ToastItem {
  id: number
  message: string
  tone: ToastTone
  action?: ToastAction
  busy?: boolean
  /**
   * Optional logical identity for notifications that can be emitted by more
   * than one completion path. Matching notifications are refreshed in place
   * instead of stacking two copies, without restarting their lifetime.
   */
  dedupeKey?: string
}

const TOAST_DURATION_MS = 5000
const MAX_TOASTS = 4

// Module-level singleton so any composable or component can raise a toast
// without prop drilling; ToastHost renders the shared queue.
const toasts = ref<ToastItem[]>([])
type PauseReason = 'pointer' | 'focus' | 'action'
interface ToastClock {
  remaining: number
  started: number
  timer?: ReturnType<typeof setTimeout>
  pauses: Set<PauseReason>
  revision: number
}
const timers = new Map<number, ToastClock>()
let nextId = 0

function dismissToast(id: number) {
  const timer = timers.get(id)
  if (timer?.timer !== undefined) clearTimeout(timer.timer)
  timers.delete(id)
  toasts.value = toasts.value.filter(toast => toast.id !== id)
}

function pauseToast(id: number, reason: PauseReason) {
  const clock = timers.get(id)
  if (!clock) return
  if (clock.timer !== undefined) {
    clearTimeout(clock.timer)
    clock.timer = undefined
    clock.remaining = Math.max(0, clock.remaining - (Date.now() - clock.started))
  }
  clock.pauses.add(reason)
}

function resumeToast(id: number, reason: PauseReason) {
  const clock = timers.get(id)
  if (!clock) return
  clock.pauses.delete(reason)
  if (clock.pauses.size || clock.timer !== undefined) return
  clock.started = Date.now()
  clock.timer = setTimeout(() => dismissToast(id), clock.remaining)
}

function isImportant(toast: Pick<ToastItem, 'tone' | 'action'>) {
  return toast.tone === 'danger' || toast.tone === 'warn' || Boolean(toast.action)
}

async function runToastAction(id: number, failureMessage: string) {
  const toast = toasts.value.find(item => item.id === id)
  const clock = timers.get(id)
  if (!toast?.action || toast.busy || !clock) return
  const action = toast.action
  const revision = clock.revision
  const ownsResult = () => timers.get(id) === clock && clock.revision === revision
  toast.busy = true
  pauseToast(id, 'action')
  try {
    const result = await action.onClick()
    // A new outcome may have replaced this keyed notice during the action.
    if (!ownsResult()) return
    if (result === false) return
    if (result && typeof result === 'object' && result.handled === false) {
      if (result.message) toast.message = result.message
      if (result.tone) toast.tone = result.tone
      if (result.removeAction) toast.action = undefined
      return
    }
    dismissToast(id)
  } catch {
    if (ownsResult()) {
      toast.message = failureMessage
      toast.tone = 'danger'
    }
  } finally {
    toast.busy = false
    resumeToast(id, 'action')
  }
}

function pushToast(message: string, options: {
  tone?: ToastTone
  duration?: number
  action?: ToastAction
  dedupeKey?: string
} = {}) {
  const text = message.trim()
  if (!text) return
  const tone = options.tone ?? 'info'
  const dedupeKey = options.dedupeKey?.trim()
  if (dedupeKey) {
    const existing = toasts.value.find(toast => toast.dedupeKey === dedupeKey)
    if (existing) {
      if (existing.message !== text || existing.tone !== tone || existing.action !== options.action) {
        const clock = timers.get(existing.id)
        if (clock) clock.revision += 1
        Object.assign(existing, { message: text, tone, action: options.action })
      }
      return
    }
  }
  if (toasts.value.length >= MAX_TOASTS) {
    const candidates = toasts.value.filter(toast => !toast.busy && !timers.get(toast.id)?.pauses.size)
    const victim = candidates.find(toast => !isImportant(toast))
      ?? (isImportant({ tone, action: options.action }) ? candidates[0] : undefined)
    if (!victim) return
    dismissToast(victim.id)
  }
  const id = ++nextId
  toasts.value = [...toasts.value, {
    id,
    message: text,
    tone,
    action: options.action,
    dedupeKey,
  }]
  timers.set(id, {
    remaining: Math.max(0, options.duration ?? TOAST_DURATION_MS),
    started: 0, pauses: new Set(), revision: 0,
  })
  resumeToast(id, 'action')
}

export function useToasts() {
  return { toasts, pushToast, dismissToast, pauseToast, resumeToast, runToastAction }
}
