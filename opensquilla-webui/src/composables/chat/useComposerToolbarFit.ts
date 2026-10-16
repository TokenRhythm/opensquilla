import { onBeforeUnmount, onMounted, type Ref } from 'vue'

type ToolbarDensity = 'wide' | 'compact' | 'icons'

/** Presentation only: both conversation hosts keep the same controls and owners. */
export function useComposerToolbarFit(
  toolbar: Ref<HTMLElement | null>,
  container: Ref<HTMLElement | null>,
) {
  let resize: ResizeObserver | undefined
  let mutations: MutationObserver | undefined
  let frame: number | undefined
  let disposed = false

  function update() {
    const root = toolbar.value
    const panel = container.value
    if (!root?.parentElement || !panel || disposed) return
    const width = root.getBoundingClientRect().width
    const panelWidth = panel.getBoundingClientRect().width
    // Hidden workbench tabs have no layout. Preserve their last presentation.
    if (width <= 0 || panelWidth <= 0) return
    if (panelWidth <= 480) {
      root.dataset.composerDensity = 'icons'
      return
    }

    // Measure an inert sibling, never expand live buttons while a menu is open
    // or the pointer/focus is on them. Only trigger controls affect this budget.
    const probe = root.cloneNode(true) as HTMLElement
    probe.setAttribute('aria-hidden', 'true')
    probe.inert = true
    probe.querySelectorAll('[id]').forEach(element => element.removeAttribute('id'))
    probe.querySelectorAll('.chat-settings-anchor').forEach(anchor => {
      const trigger = anchor.querySelector(':scope > button')
      Array.from(anchor.children).forEach(child => { if (child !== trigger) child.remove() })
    })
    probe.querySelectorAll('.chat-send-tooltip').forEach(element => element.remove())
    Object.assign(probe.style, {
      position: 'absolute', visibility: 'hidden', pointerEvents: 'none',
      width: `${width}px`, boxSizing: 'border-box', left: '0', top: '0',
    })
    probe.querySelectorAll<HTMLElement>('.chat-input-actions').forEach(group => {
      Object.assign(group.style, { flex: 'none', width: 'max-content', maxWidth: 'none' })
    })
    root.parentElement.append(probe)
    try {
      const modes: ToolbarDensity[] = panelWidth <= 560 ? ['compact', 'icons'] : ['wide', 'compact', 'icons']
      for (const mode of modes) {
        probe.dataset.composerDensity = mode
        const style = getComputedStyle(probe)
        const number = (value: string) => Number.parseFloat(value) || 0
        const groups = Array.from(probe.querySelectorAll<HTMLElement>('.chat-input-actions'))
        const required = groups.reduce((total, group) => total + Math.max(group.scrollWidth, group.getBoundingClientRect().width), 0)
          + Math.max(0, groups.length - 1) * number(style.columnGap)
          + number(style.paddingLeft) + number(style.paddingRight)
        if (required <= width + 1 || mode === 'icons') {
          root.dataset.composerDensity = mode
          break
        }
      }
    } finally {
      probe.remove()
    }
  }

  function schedule() {
    if (disposed || frame !== undefined) return
    frame = requestAnimationFrame(() => {
      frame = undefined
      update()
    })
  }

  onMounted(() => {
    update()
    if (typeof ResizeObserver !== 'undefined') {
      resize = new ResizeObserver(schedule)
      // Observe the input container, not controls changed by the fit itself.
      if (container.value) resize.observe(container.value)
    }
    if (typeof MutationObserver !== 'undefined' && toolbar.value) {
      mutations = new MutationObserver(schedule)
      // Text/optional controls can change with locale, model, or run state.
      // Do not observe our own density attribute (no feedback loop).
      mutations.observe(toolbar.value, { childList: true, subtree: true, characterData: true })
    }
    // A font becoming available can change label widths without resizing the panel.
    document.fonts?.addEventListener?.('loadingdone', schedule)
  })
  onBeforeUnmount(() => {
    disposed = true
    resize?.disconnect()
    mutations?.disconnect()
    if (frame !== undefined) cancelAnimationFrame(frame)
    document.fonts?.removeEventListener?.('loadingdone', schedule)
  })
}
