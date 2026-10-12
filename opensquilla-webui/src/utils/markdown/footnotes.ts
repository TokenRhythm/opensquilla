/** Resolve footnote links inside one mounted Markdown body, never document IDs. */
export function bindMarkdownFootnotes(root: HTMLElement): () => void {
  function activate(event: Event): void {
    if (event instanceof KeyboardEvent && event.key !== 'Enter' && event.key !== ' ') return
    const target = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-markdown-footnote-ref], [data-markdown-footnote-back]') : null
    if (!target || !root.contains(target)) return
    const number = target.dataset.markdownFootnoteRef || target.dataset.markdownFootnoteBack || ''
    if (!/^\d+$/.test(number)) return
    const attribute = target.hasAttribute('data-markdown-footnote-ref') ? 'data-markdown-footnote' : 'data-markdown-footnote-ref'
    const destination = root.querySelector<HTMLElement>(`[${attribute}="${number}"]`)
    if (!destination) return
    event.preventDefault()
    destination.tabIndex = destination.tagName === 'LI' ? -1 : 0
    destination.focus({ preventScroll: true })
    destination.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  }
  root.addEventListener('click', activate)
  root.addEventListener('keydown', activate)
  return () => { root.removeEventListener('click', activate); root.removeEventListener('keydown', activate) }
}
