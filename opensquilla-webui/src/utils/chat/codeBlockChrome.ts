import { copyTextWithFallback } from '@/utils/browser'

// Shared decoration for rendered markdown code blocks: adds the `.code-block`
// chrome (header gradient + language label positioning) and a copy button to
// every <pre> under `root`. Used by TextPart, StreamingTextPart, and the
// workbench markdown preview so a code block looks and behaves the same while
// streaming, after settling, and in artifact previews.
//
// The pass is idempotent: blocks already carrying a button are skipped, so it
// can re-run after every v-html update without duplicating controls. Like the
// other decorate* passes, it only ever createElement/textContent — no HTML
// sinks — because it operates on already-sanitized renderer output.

export interface CodeBlockChromeLabels {
  copy: string
  copied: string
  copyFailed: string
}

export function codeText(pre: HTMLPreElement): string {
  const code = Array.from(pre.children).find(child => child.tagName === 'CODE')
  return code?.textContent || ''
}

function hasCodeCopyButton(pre: HTMLPreElement): boolean {
  return Array.from(pre.children).some(child => child.classList.contains('code-copy-btn'))
}

function setCopyButtonState(
  button: HTMLButtonElement,
  labels: CodeBlockChromeLabels,
  state: 'idle' | 'copied' | 'error',
): void {
  const label = state === 'copied'
    ? labels.copied
    : state === 'error'
      ? labels.copyFailed
      : labels.copy
  button.replaceChildren(copyIconSvg(state))
  button.title = label
  button.setAttribute('aria-label', label)
  button.classList.toggle('is-copied', state === 'copied')
  button.classList.toggle('is-error', state === 'error')
}

export function copyIconSvg(state: 'idle' | 'copied' | 'error'): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('width', '15')
  svg.setAttribute('height', '15')
  svg.setAttribute('aria-hidden', 'true')
  svg.setAttribute('focusable', 'false')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')

  if (state === 'copied') {
    svg.appendChild(svgNode('polyline', { points: '20 6 9 17 4 12' }))
    return svg
  }
  if (state === 'error') {
    svg.appendChild(svgNode('path', { d: 'M18 6 6 18' }))
    svg.appendChild(svgNode('path', { d: 'm6 6 12 12' }))
    return svg
  }

  svg.appendChild(svgNode('rect', { width: '14', height: '14', x: '8', y: '8', rx: '2', ry: '2' }))
  svg.appendChild(svgNode('path', { d: 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2' }))
  return svg
}

function svgNode(tag: string, attrs: Record<string, string>): SVGElement {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag)
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value)
  return node
}

// One owner per mounted Markdown surface. Incremental decoration only scans the
// newly committed block; resets release the whole generation without revisiting
// earlier blocks on every streaming append.
export function createCodeBlockChrome() {
  const controls = new Map<HTMLButtonElement, { refresh: () => void; dispose: () => void }>()
  let labels: CodeBlockChromeLabels = { copy: '', copied: '', copyFailed: '' }
  let disposed = false

  function decorate(root: HTMLElement | null, nextLabels: CodeBlockChromeLabels, selector = 'pre'): void {
    if (disposed || !root) return
    labels = nextLabels
    for (const pre of root.querySelectorAll<HTMLPreElement>(selector)) {
      if (hasCodeCopyButton(pre) || !codeText(pre)) continue
      pre.classList.add('code-block', 'markdown-code-block')
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'code-copy-btn'
      button.dataset.shareControl = 'true'
      let state: 'idle' | 'copied' | 'error' = 'idle'
      let active = true
      let request = 0
      let timer: ReturnType<typeof setTimeout> | undefined
      const refresh = () => setCopyButtonState(button, labels, state)
      async function onClick(event: MouseEvent) {
        event.preventDefault()
        event.stopPropagation()
        const current = ++request
        clearTimeout(timer)
        let result: typeof state = 'copied'
        try { await copyTextWithFallback(codeText(pre)) }
        catch { result = 'error' }
        if (!active || current !== request || !root?.contains(button)) return
        state = result
        refresh()
        timer = setTimeout(() => {
          if (!active || current !== request || !root?.contains(button)) return
          state = 'idle'
          refresh()
          timer = undefined
        }, 1600)
      }
      refresh()
      button.addEventListener('click', onClick)
      pre.appendChild(button)
      controls.set(button, {
        refresh,
        dispose() {
          active = false
          request += 1
          clearTimeout(timer)
          button.removeEventListener('click', onClick)
          button.remove()
        },
      })
    }
  }

  function updateLabels(nextLabels: CodeBlockChromeLabels): void {
    labels = nextLabels
    for (const control of controls.values()) control.refresh()
  }

  function reset(): void {
    for (const control of controls.values()) control.dispose()
    controls.clear()
  }

  function dispose(): void {
    reset()
    disposed = true
  }

  return { decorate, updateLabels, reset, dispose }
}

// Compatibility for standalone decoration. Mounted surfaces use an explicit
// owner above so their unmount/reset can release callbacks and timers.
const standaloneOwners = new WeakMap<HTMLElement, ReturnType<typeof createCodeBlockChrome>>()
export function decorateCodeBlocks(root: HTMLElement | null, labels: CodeBlockChromeLabels): void {
  if (!root) return
  let owner = standaloneOwners.get(root)
  if (!owner) {
    owner = createCodeBlockChrome()
    standaloneOwners.set(root, owner)
  }
  owner.updateLabels(labels)
  owner.decorate(root, labels)
}
