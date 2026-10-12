import type { DiagramPalette } from './types'

// Resolve inherited theme variables to actual colors (including nested share
// export themes). Mermaid accepts hex colors, not CSS var() or color-mix().
function color(root: HTMLElement, token: string, fallback: string, background = fallback): string {
  const probe = document.createElement('span')
  probe.style.color = `var(${token}, ${fallback})`
  probe.style.display = 'none'
  root.appendChild(probe)
  const value = getComputedStyle(probe).color
  probe.remove()
  // CSS Color 4 and alpha colors need real compositing, particularly subtle
  // borders in dark themes. Parsing just RGB would turn those borders white.
  try {
    const canvas = document.createElement('canvas')
    canvas.width = canvas.height = 1
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (context) {
      context.fillStyle = background
      context.fillRect(0, 0, 1, 1)
      context.fillStyle = value || fallback
      context.fillRect(0, 0, 1, 1)
      const pixel = context.getImageData(0, 0, 1, 1).data
      return `#${Array.from(pixel).slice(0, 3).map(channel => channel.toString(16).padStart(2, '0')).join('')}`
    }
  } catch { /* DOM-only tests have no canvas; production browsers do. */ }
  const rgb = value.match(/^rgba?\(\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)/)
  if (rgb) return `#${rgb.slice(1, 4).map(channel => Math.round(Number(channel)).toString(16).padStart(2, '0')).join('')}`
  if (/^#[\da-f]{6}$/i.test(value)) return value
  return fallback
}

export function diagramPalette(root: HTMLElement): DiagramPalette {
  const background = color(root, '--bg', '#ffffff')
  const channels = [1, 3, 5].map(offset => parseInt(background.slice(offset, offset + 2), 16))
  return {
    background,
    text: color(root, '--text', '#242424', background),
    muted: color(root, '--text-muted', '#666666', background),
    border: color(root, '--border', '#cccccc', background),
    accent: color(root, '--accent', '#555555', background),
    dark: channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722 < 128,
  }
}
