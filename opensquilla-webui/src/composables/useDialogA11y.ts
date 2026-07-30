import { computed, nextTick, onUnmounted, ref, watch, type Ref } from 'vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'

const FOCUSABLE = [
  'button:not([disabled]):not([tabindex="-1"])',
  'a[href]:not([tabindex="-1"])',
  'input:not([disabled]):not([tabindex="-1"])',
  'textarea:not([disabled]):not([tabindex="-1"])',
  'select:not([disabled]):not([tabindex="-1"])',
  'summary:not([tabindex="-1"])',
  'iframe:not([tabindex="-1"])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

// Dialogs can nest transiently (for example, a discard confirmation opened
// from the provider catalog). Keep one module-wide LIFO stack so only the
// visually topmost dialog owns Escape and Tab. Component mount order is not a
// reliable proxy: ConfirmModal is mounted globally before Settings dialogs.
const openDialogStack: symbol[] = []
const openDialogVersion = ref(0)
const nativeSurfaceOcclusionStack: symbol[] = []
const nativeSurfaceOcclusionVersion = ref(0)

function registerOpenDialog(token: symbol) {
  const existing = openDialogStack.indexOf(token)
  if (existing >= 0) openDialogStack.splice(existing, 1)
  openDialogStack.push(token)
  openDialogVersion.value += 1
}

function unregisterOpenDialog(token: symbol): boolean {
  const existing = openDialogStack.indexOf(token)
  if (existing < 0) return false
  const wasTopmost = existing === openDialogStack.length - 1
  openDialogStack.splice(existing, 1)
  openDialogVersion.value += 1
  return wasTopmost
}

function registerNativeSurfaceOcclusion(token: symbol) {
  const existing = nativeSurfaceOcclusionStack.indexOf(token)
  if (existing >= 0) nativeSurfaceOcclusionStack.splice(existing, 1)
  nativeSurfaceOcclusionStack.push(token)
  nativeSurfaceOcclusionVersion.value += 1
}

function unregisterNativeSurfaceOcclusion(token: symbol) {
  const existing = nativeSurfaceOcclusionStack.indexOf(token)
  if (existing < 0) return
  nativeSurfaceOcclusionStack.splice(existing, 1)
  nativeSurfaceOcclusionVersion.value += 1
}

/** True while any modal/drawer layer has registered global keyboard ownership. */
export function hasOpenDialogLayer(): boolean {
  return openDialogStack.length > 0
}

/** Reactive counterpart used by native surfaces that must disappear whenever
 * a DOM modal owns the window. The returned computed contains no dialog
 * identity or content; it only exposes whether the shared stack is non-empty. */
export function useOpenDialogLayerState() {
  return computed(() => {
    void openDialogVersion.value
    return openDialogStack.length > 0
  })
}

/** True while a DOM layer that must paint above WebContentsView is open. */
export function useNativeSurfaceOcclusionState() {
  return computed(() => {
    void nativeSurfaceOcclusionVersion.value
    return nativeSurfaceOcclusionStack.length > 0
  })
}

/**
 * Register a component that already owns its own focus/Escape implementation in
 * the same LIFO stack as useDialogA11y. The returned ref lets that implementation
 * act only while it is visually topmost.
 */
export function useDialogLayer(
  isOpen: Ref<boolean>,
  options: { occludesNativeSurface?: boolean } = {},
) {
  const token = Symbol('dialog-layer')
  const occludesNativeSurface = options.occludesNativeSurface !== false
  const isTopmost = computed(() => {
    // The stack is intentionally a small module-local array, but custom layer
    // ownership is reactive: opening a layer above an already-evaluated one
    // must invalidate its cached computed value immediately.
    void openDialogVersion.value
    return isOpen.value && openDialogStack[openDialogStack.length - 1] === token
  })

  watch(isOpen, (open) => {
    if (open) {
      registerOpenDialog(token)
      if (occludesNativeSurface) registerNativeSurfaceOcclusion(token)
    } else {
      unregisterOpenDialog(token)
      unregisterNativeSurfaceOcclusion(token)
    }
  }, { immediate: true, flush: 'sync' })

  onUnmounted(() => {
    unregisterOpenDialog(token)
    unregisterNativeSurfaceOcclusion(token)
  })
  return isTopmost
}

interface DialogA11yOptions {
  // Element to focus when the dialog opens. Defaults to the first focusable
  // inside the dialog — pass an explicit ref for confirm dialogs so a
  // destructive primary button is not auto-focused.
  initialFocus?: Ref<HTMLElement | null>
  // Native WebContentsView surfaces sit above renderer DOM. Most dialogs must
  // hide them while open; the mobile Workbench itself is the one exception.
  occludesNativeSurface?: boolean
}

/**
 * Modal-dialog accessibility for an open/close-driven overlay: traps Tab focus
 * inside `rootRef`, closes on Escape, moves focus into the dialog on open, and
 * restores focus to the invoking element on close. Mirrors the pattern already
 * used by SettingsDialog and SessionInspectDrawer.
 */
export function useDialogA11y(
  rootRef: Ref<HTMLElement | null>,
  isOpen: Ref<boolean>,
  onClose: () => void,
  options: DialogA11yOptions = {},
) {
  const dialogToken = Symbol('dialog-a11y')
  const occludesNativeSurface = options.occludesNativeSurface !== false
  let invokerEl: HTMLElement | null = null

  function onKeydown(event: KeyboardEvent) {
    if (event.defaultPrevented) return
    if (!isOpen.value) return
    if (openDialogStack[openDialogStack.length - 1] !== dialogToken) return
    if (event.key === 'Escape') {
      event.stopPropagation()
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const root = rootRef.value
    if (!root) return
    const focusables = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    const active = document.activeElement as HTMLElement | null
    const inside = !!active && root.contains(active)
    if (event.shiftKey && (!inside || active === first)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (!inside || active === last)) {
      event.preventDefault()
      first.focus()
    }
  }

  useDocumentEvent('keydown', onKeydown)

  watch(isOpen, (open, wasOpen) => {
    if (open && !wasOpen) {
      invokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
      registerOpenDialog(dialogToken)
      if (occludesNativeSurface) registerNativeSurfaceOcclusion(dialogToken)
      void nextTick(() => {
        const target = options.initialFocus?.value
          ?? rootRef.value?.querySelector<HTMLElement>(FOCUSABLE)
          ?? null
        target?.focus()
      })
    } else if (!open && wasOpen) {
      const wasTopmost = unregisterOpenDialog(dialogToken)
      unregisterNativeSurfaceOcclusion(dialogToken)
      if (wasTopmost && invokerEl && document.contains(invokerEl)) invokerEl.focus()
      invokerEl = null
    }
  }, { immediate: true })

  onUnmounted(() => {
    unregisterOpenDialog(dialogToken)
    unregisterNativeSurfaceOcclusion(dialogToken)
    invokerEl = null
  })
}
