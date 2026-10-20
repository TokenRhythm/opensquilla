import type { OfficeSelection } from '@/types/officeSelection'

export interface OfficeSelectionLabels { text: string; element: string; range: string }
export interface OfficeViewportRect { left: number; top: number; width: number; height: number }
/** Transient screen geometry. Never persist this in OfficeSelection or pageContext. */
export interface OfficeSelectionAnchor {
  rect: OfficeViewportRect
  rects: OfficeViewportRect[]
  viewport: OfficeViewportRect
  visible: boolean
}
export interface OfficeSelectionCommit { selection: OfficeSelection; anchor: OfficeSelectionAnchor | null }
export interface OfficeHitTarget {
  kind: keyof OfficeSelectionLabels
  /** CSS pixels in browser viewport coordinates; never a write-back locator. */
  rect: { left: number; top: number; width: number; height: number }
  rects?: OfficeViewportRect[]
  selection?: OfficeSelection
}
export interface OfficeSelectionInteractionOptions {
  container: HTMLElement
  labels?: OfficeSelectionLabels
  hitTest(clientX: number, clientY: number): Promise<OfficeHitTarget | null>
  readNativeSelection(): OfficeSelection | null
  clearNativeSelection(): void
  onSelection(value: OfficeSelection | null): void
  onSelectionCommit?(value: OfficeSelection): void
  onSelectionCommitDetail?(value: OfficeSelectionCommit): void
  onAnchorChange?(anchor: OfficeSelectionAnchor | null): void
  resolveAnchor?(selection: OfficeSelection, hit?: OfficeHitTarget): Promise<OfficeSelectionAnchor | null>
}

/** Owns only annotation feedback. Normal text copying and sheet navigation remain native. */
export function createOfficeSelectionInteraction(options: OfficeSelectionInteractionOptions) {
  const { container } = options
  const document = container.ownerDocument
  const window = document.defaultView!
  const originalPosition = container.style.position
  const adjustedPosition = window.getComputedStyle(container).position === 'static'
    || !window.getComputedStyle(container).position
  if (adjustedPosition) container.style.position = 'relative'
  const layer = document.createElement('div')
  layer.dataset.officeAnnotationLayer = ''
  layer.setAttribute('aria-hidden', 'true')
  layer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5;'
  const hover = document.createElement('div')
  hover.dataset.officeHover = ''
  hover.style.cssText = 'position:absolute;display:none;box-sizing:border-box;border:1px dashed var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent);pointer-events:none;'
  const label = document.createElement('span')
  label.style.cssText = 'position:absolute;width:max-content;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 5px;border-radius:var(--radius-sm);background:var(--accent);color:var(--accent-foreground);font:600 11px/1.4 sans-serif;pointer-events:none;'
  hover.append(label)
  const selected = document.createElement('div')
  selected.dataset.officeSelected = ''
  selected.style.cssText = 'position:absolute;display:none;box-sizing:border-box;border:2px solid var(--accent);pointer-events:none;'
  layer.append(hover, selected)
  container.append(layer)
  let enabled = false, locked = false, destroyed = false, generation = 0, selectionGeneration = 0, frame = 0, busy = false
  let pointer: { x: number; y: number } | null = null
  let press: { x: number; y: number } | null = null
  let current: OfficeSelection | null = null
  let committed = false, anchorGeneration = 0
  type Gesture = { kind: 'pointer'; pointerId: number; committed: boolean; released: boolean; hit: Promise<OfficeHitTarget | null> }
    | { kind: 'keyboard'; key: string; committed: boolean; released: boolean }
  let gesture: Gesture | null = null
  let commitFrame = 0

  function hideHover() { hover.style.display = 'none'; label.textContent = '' }
  function invalidateHover() {
    generation += 1
    pointer = null
    if (frame) window.cancelAnimationFrame(frame)
    frame = 0
    hideHover()
  }
  function clearSelection() {
    selectionGeneration += 1
    cancelGesture()
    invalidateHover()
    press = null
    selected.style.display = 'none'
    current = null
    committed = false
    anchorGeneration += 1
    options.onAnchorChange?.(null)
    const selection = document.getSelection()
    if (selection && ((selection.anchorNode && container.contains(selection.anchorNode))
      || (selection.focusNode && container.contains(selection.focusNode)))) selection.removeAllRanges()
    options.clearNativeSelection()
    options.onSelection(null)
  }
  function cancelGesture() {
    gesture = null
    if (commitFrame) window.cancelAnimationFrame(commitFrame)
    commitFrame = 0
  }
  async function commitSelection(owner: Gesture, value: OfficeSelection, hit?: OfficeHitTarget) {
    if (!enabled || destroyed || gesture !== owner || owner.committed) return
    // Mark first: opening the input can synchronously clear or destroy this preview.
    owner.committed = true
    current = value
    committed = true
    options.onSelection(value)
    const run = ++anchorGeneration
    const resolving = options.resolveAnchor?.(value, hit).catch(() => null)
    if (enabled && !destroyed && gesture === owner) options.onSelectionCommit?.(value)
    const anchor = await resolving ?? null
    if (enabled && !destroyed && gesture === owner && run === anchorGeneration) {
      if (anchor?.visible) draw(selected, { kind: value.kind, rect: anchor.rect })
      options.onAnchorChange?.(anchor)
      options.onSelectionCommitDetail?.({ selection: value, anchor })
    }
  }
  async function refreshAnchor() {
    if ((!enabled && !locked) || destroyed || !current || !committed) return
    const value = current, run = ++anchorGeneration
    const anchor = await options.resolveAnchor?.(value).catch(() => null) ?? null
    if (destroyed || (!enabled && !locked) || current !== value || run !== anchorGeneration) return
    if (anchor?.visible) draw(selected, { kind: value.kind, rect: anchor.rect })
    else selected.style.display = 'none'
    options.onAnchorChange?.(anchor)
  }
  function scheduleNativeCommit(owner: Gesture) {
    if (commitFrame || owner.committed || gesture !== owner) return
    // Selection notifications (notably XLSX) use RAF too. Read the public getter
    // after that frame; never commit an intermediate selectionchange payload.
    commitFrame = window.requestAnimationFrame(() => {
      commitFrame = window.requestAnimationFrame(() => { void (async () => {
        commitFrame = 0
        if (!enabled || destroyed || gesture !== owner || owner.committed) return
        try {
          // A preserved native range is not a new choice when the gesture began
          // on page whitespace, sheet chrome, or a scrollbar.
          if (owner.kind === 'pointer' && !(await owner.hit)) return
          if (!enabled || destroyed || gesture !== owner || owner.committed) return
          const value = options.readNativeSelection()
          if (value && value.kind !== 'element') void commitSelection(owner, value)
        } catch { /* A selection lost during navigation is not an annotation. */ }
      })() })
    })
  }
  function invalidateFeedback() {
    selectionGeneration += 1
    invalidateHover()
    selected.style.display = 'none'
  }
  function draw(element: HTMLElement, hit: OfficeHitTarget) {
    const host = container.getBoundingClientRect(), rect = hit.rect
    const left = Math.max(host.left, rect.left), top = Math.max(host.top, rect.top)
    const right = Math.min(host.right, rect.left + rect.width), bottom = Math.min(host.bottom, rect.top + rect.height)
    if (![left, top, right, bottom].every(Number.isFinite) || right <= left || bottom <= top) {
      element.style.display = 'none'
      return
    }
    Object.assign(element.style, { display: 'block', left: `${left - host.left + container.scrollLeft}px`,
      top: `${top - host.top + container.scrollTop}px`, width: `${right - left}px`, height: `${bottom - top}px` })
    if (element === hover) {
      const labelWidth = Math.min(240, Math.max(0, host.width - 12))
      Object.assign(label.style, { maxWidth: `${labelWidth}px`,
        left: `${Math.max(host.left + 2, Math.min(left, host.right - labelWidth - 10)) - left}px`,
        top: `${Math.max(host.top + 2, top - 24) - top}px` })
    }
  }
  async function flushHover() {
    frame = 0
    if (!enabled || destroyed || busy || !pointer) return
    const point = pointer, run = generation
    pointer = null
    busy = true
    try {
      const hit = await options.hitTest(point.x, point.y)
      if (!destroyed && enabled && run === generation) {
        if (hit) { draw(hover, hit); label.textContent = options.labels?.[hit.kind] || '' }
        else hideHover()
      }
    } catch { if (run === generation) hideHover() }
    finally {
      busy = false
      if (enabled && !destroyed && pointer && !frame) frame = window.requestAnimationFrame(() => { void flushHover() })
    }
  }
  function onPointerMove(event: PointerEvent) {
    if (!enabled) return
    generation += 1
    if (event.buttons) { pointer = null; hideHover(); return }
    pointer = { x: event.clientX, y: event.clientY }
    if (!frame && !busy) frame = window.requestAnimationFrame(() => { void flushHover() })
  }
  function onPointerDown(event: PointerEvent) {
    if (!enabled || event.button !== 0) return
    selectionGeneration += 1
    cancelGesture()
    press = null
    if (isChromePointer(event)) return
    gesture = { kind: 'pointer', pointerId: event.pointerId, committed: false, released: false,
      hit: options.hitTest(event.clientX, event.clientY).catch(() => null) }
    invalidateHover()
    selected.style.display = 'none'
    current = null
    committed = false
    anchorGeneration += 1
    options.onAnchorChange?.(null)
    options.onSelection(null)
    press = { x: event.clientX, y: event.clientY }
  }
  function isChromePointer(event: PointerEvent) {
    const target = event.target
    if (!(target instanceof HTMLElement)) return false
    if (target.closest('button,input,textarea,select,[role="tab"],[role="scrollbar"]')) return true
    for (let element: HTMLElement | null = target; element && container.contains(element); element = element.parentElement) {
      const rect = element.getBoundingClientRect()
      if (element.clientWidth > 0 && element.offsetWidth > element.clientWidth
        && event.clientX >= rect.left + element.clientLeft + element.clientWidth) return true
      if (element.clientHeight > 0 && element.offsetHeight > element.clientHeight
        && event.clientY >= rect.top + element.clientTop + element.clientHeight) return true
    }
    return false
  }
  function onPointerUp(event: PointerEvent) {
    if (!enabled || event.button !== 0 || gesture?.kind !== 'pointer'
      || gesture.pointerId !== event.pointerId || gesture.released) return
    gesture.released = true
    scheduleNativeCommit(gesture)
  }
  function onPointerCancel(event: PointerEvent) {
    if (gesture?.kind === 'pointer' && gesture.pointerId === event.pointerId) clearSelection()
  }
  function onKeyDown(event: KeyboardEvent) {
    const key = event.key.toLowerCase()
    const selecting = (event.shiftKey && /^(arrow(left|right|up|down)|home|end|pageup|pagedown)$/.test(key))
      || ((event.ctrlKey || event.metaKey) && key === 'a')
    if (!enabled || !selecting || event.isComposing) return
    if (event.repeat && gesture?.kind === 'keyboard' && gesture.key === key && !gesture.released) return
    selectionGeneration += 1
    cancelGesture()
    invalidateHover()
    selected.style.display = 'none'
    committed = false
    anchorGeneration += 1
    gesture = { kind: 'keyboard', key, committed: false, released: false }
  }
  function onKeyUp(event: KeyboardEvent) {
    if (!enabled || gesture?.kind !== 'keyboard' || gesture.key !== event.key.toLowerCase() || gesture.released) return
    gesture.released = true
    scheduleNativeCommit(gesture)
  }
  async function onClick(event: MouseEvent) {
    if (!enabled || event.button !== 0) return
    const start = press
    const owner = gesture
    press = null
    const native = document.getSelection()
    // Dragging text belongs to the native selection callback, not the object picker.
    if (!start || owner?.kind !== 'pointer' || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4
      || (native && !native.isCollapsed && native.anchorNode && container.contains(native.anchorNode))) return
    const run = selectionGeneration
    try {
      const hit = await owner.hit
      if (!enabled || destroyed || run !== selectionGeneration || gesture !== owner || owner.committed || !hit?.selection) return
      draw(selected, hit)
      void commitSelection(owner, hit.selection, hit)
    } catch { /* A missed object leaves the current selection empty. */ }
  }
  function onScroll(event: Event) {
    const target = event.target
    if ((enabled || locked) && (target === window || (target instanceof Node
      && (container.contains(target) || target.contains(container))))) {
      invalidateFeedback()
      void refreshAnchor()
    }
  }
  function onLeave() { invalidateHover() }
  container.addEventListener('pointermove', onPointerMove)
  container.addEventListener('pointerdown', onPointerDown, true)
  container.addEventListener('click', onClick)
  container.addEventListener('pointerleave', onLeave)
  container.addEventListener('keydown', onKeyDown, true)
  container.addEventListener('keyup', onKeyUp)
  window.addEventListener('pointerup', onPointerUp, true)
  window.addEventListener('pointercancel', onPointerCancel, true)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', onResize)
  function onResize() { invalidateFeedback(); void refreshAnchor() }
  return {
    get enabled() { return enabled && !destroyed },
    get locked() { return locked && !destroyed },
    get tracking() { return (enabled || locked) && !destroyed },
    setSelectionMode(value: boolean, preserveCommitted = false) {
      if (destroyed || (enabled === value && locked === preserveCommitted)) return
      enabled = value
      locked = preserveCommitted
      if (locked) {
        // Stop picking without disposing the target being edited. Geometry and
        // highlighting continue to track its viewport while the input owns focus.
        selectionGeneration += 1
        cancelGesture()
        invalidateHover()
        press = null
        void refreshAnchor()
      } else clearSelection()
    },
    clearSelection,
    clearTransientSelection: clearSelection,
    refreshAnchor,
    showSelection(value: OfficeSelection, anchor: OfficeSelectionAnchor | null) {
      if (destroyed) return
      cancelGesture()
      current = value
      committed = true
      anchorGeneration += 1
      if (anchor?.visible) draw(selected, { kind: value.kind, rect: anchor.rect })
      else selected.style.display = 'none'
      options.onSelection(value)
      options.onAnchorChange?.(anchor)
    },
    invalidateFeedback,
    nativeSelection(value: OfficeSelection | null) {
      if (!enabled || destroyed) return
      if (!committed && (value || current?.kind !== 'element')) {
        current = value
        selected.style.display = 'none'
        options.onSelection(value)
      }
    },
    destroy() {
      if (destroyed) return
      enabled = false
      locked = false
      clearSelection()
      destroyed = true
      container.removeEventListener('pointermove', onPointerMove)
      container.removeEventListener('pointerdown', onPointerDown, true)
      container.removeEventListener('click', onClick)
      container.removeEventListener('pointerleave', onLeave)
      container.removeEventListener('keydown', onKeyDown, true)
      container.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('pointerup', onPointerUp, true)
      window.removeEventListener('pointercancel', onPointerCancel, true)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
      layer.remove()
      if (adjustedPosition) container.style.position = originalPosition
    },
  }
}
