import { ref } from 'vue'

export type ToastTone = 'info' | 'ok' | 'warn' | 'danger'

export interface ToastItem {
  id: number
  message: string
  tone: ToastTone
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

function pushToast(message: string, options: { tone?: ToastTone; duration?: number } = {}) {
  const text = message.trim()
  if (!text) return
  while (toasts.value.length >= MAX_TOASTS) {
    dismissToast(toasts.value[0].id)
  }
  const id = ++nextId
  toasts.value = [...toasts.value, { id, message: text, tone: options.tone ?? 'info' }]
  timers.set(id, setTimeout(() => dismissToast(id), options.duration ?? TOAST_DURATION_MS))
}

export function useToasts() {
  return { toasts, pushToast, dismissToast }
}
