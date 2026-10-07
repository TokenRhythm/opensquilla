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
  const code = pre.querySelector('code')
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

export function decorateCodeBlocks(root: HTMLElement | null, labels: CodeBlockChromeLabels): void {
  if (!root) return
  for (const pre of root.querySelectorAll<HTMLPreElement>('pre')) {
    if (hasCodeCopyButton(pre)) continue
    const text = codeText(pre)
    if (!text) continue

    pre.classList.add('code-block')
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'code-copy-btn'
    setCopyButtonState(button, labels, 'idle')
    button.addEventListener('click', async event => {
      event.preventDefault()
      event.stopPropagation()
      try {
        await copyTextWithFallback(codeText(pre))
        setCopyButtonState(button, labels, 'copied')
        button.classList.add('is-copied')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCopyButtonState(button, labels, 'idle')
          button.classList.remove('is-copied')
        }, 1600)
      } catch {
        setCopyButtonState(button, labels, 'error')
        button.classList.add('is-error')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCopyButtonState(button, labels, 'idle')
          button.classList.remove('is-error')
        }, 1600)
      }
    })
    pre.appendChild(button)
  }
}
