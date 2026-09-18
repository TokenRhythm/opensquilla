import { ref } from 'vue'

export type ToastTone = 'info' | 'ok' | 'warn' | 'danger'

export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastItem {
  id: number
  message: string
  tone: ToastTone
  action?: ToastAction
  /**
   * Optional logical identity for notifications that can be emitted by more
   * than one completion path. Matching notifications are refreshed in place
   * instead of stacking two copies on top of each other.
   */
  dedupeKey?: string
}

const TOAST_DURATION_MS = 5000
const MAX_TOASTS = 4

// Module-level singleton so any composable or component can raise a toast
// without prop drilling; ToastHost renders the shared queue.
const toasts = ref<ToastItem[]>([])
const timers = new Map<number, ReturnType<typeof setTimeout>>()
let nextId = 0

function dismissToast(id: number) {
  const timer = timers.get(id)
  if (timer) {
    clearTimeout(timer)
    timers.delete(id)
  }
  toasts.value = toasts.value.filter(toast => toast.id !== id)
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
      const timer = timers.get(existing.id)
      if (timer) clearTimeout(timer)
      toasts.value = toasts.value.map(toast => toast.id === existing.id
        ? { ...toast, message: text, tone, action: options.action, dedupeKey }
        : toast)
      timers.set(existing.id, setTimeout(() => dismissToast(existing.id), options.duration ?? TOAST_DURATION_MS))
      return
    }
  }
  while (toasts.value.length >= MAX_TOASTS) {
    dismissToast(toasts.value[0].id)
  }
  const id = ++nextId
  toasts.value = [...toasts.value, {
    id,
    message: text,
    tone,
    action: options.action,
    dedupeKey,
  }]
  timers.set(id, setTimeout(() => dismissToast(id), options.duration ?? TOAST_DURATION_MS))
}

export function useToasts() {
  return { toasts, pushToast, dismissToast }
}
