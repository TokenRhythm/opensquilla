import type { ITheme } from '@xterm/xterm'

type Rgb = readonly [number, number, number]

function rgb(value: string): Rgb | null {
  const hex = value.trim().match(/^#([\da-f]{3}|[\da-f]{6})$/i)?.[1]
  if (hex) {
    const full = hex.length === 3 ? [...hex].map(char => char + char).join('') : hex
    return [0, 2, 4].map(offset => Number.parseInt(full.slice(offset, offset + 2), 16)) as unknown as Rgb
  }
  const match = value.trim().match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*1)?\s*\)$/i)
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null
}

function color(value: Rgb): string {
  return `#${value.map(channel => Math.round(channel).toString(16).padStart(2, '0')).join('')}`
}

function mix(first: string, second: string, amount: number): string {
  const a = rgb(first)!
  const b = rgb(second)!
  return color(a.map((channel, i) => channel * (1 - amount) + b[i]! * amount) as unknown as Rgb)
}

/**
 * ANSI colors must follow the app's reading colors. xterm's default palette is
 * intended for a dark terminal and its bright yellow is unreadable on paper.
 * Keep the complete palette explicit, including cursor and selected text.
 */
export function terminalThemeFromTokens(read: (token: string) => string): ITheme {
  const token = (name: string, fallback: string) => {
    const parsed = rgb(read(name))
    return parsed ? color(parsed) : fallback
  }
  const background = token('--bg', '#18181a')
  const foreground = token('--text', '#f5f5f7')
  const muted = token('--text-muted', foreground)
  const accent = token('--accent', foreground)
  const red = token('--danger', foreground)
  const green = token('--ok', foreground)
  const yellow = token('--warn', foreground)
  const blue = token('--info', foreground)
  const magenta = token('--queued', foreground)
  const cyan = token('--syntax-attr', blue)
  const [r, g, b] = rgb(background)!
  const isLight = 0.2126 * r + 0.7152 * g + 0.0722 * b > 127
  return {
    background,
    foreground,
    cursor: foreground,
    cursorAccent: background,
    selectionBackground: mix(background, accent, 0.22),
    selectionInactiveBackground: mix(background, foreground, 0.12),
    selectionForeground: foreground,
    black: isLight ? foreground : background,
    red,
    green,
    yellow,
    blue,
    magenta,
    cyan,
    white: isLight ? background : foreground,
    brightBlack: muted,
    brightRed: mix(red, foreground, 0.15),
    brightGreen: mix(green, foreground, 0.15),
    brightYellow: mix(yellow, foreground, 0.15),
    brightBlue: mix(blue, foreground, 0.15),
    brightMagenta: mix(magenta, foreground, 0.15),
    brightCyan: mix(cyan, foreground, 0.15),
    brightWhite: foreground,
  }
}

export function readTerminalTheme(element: HTMLElement): ITheme {
  const style = getComputedStyle(element.closest('[data-theme]') ?? document.documentElement)
  return terminalThemeFromTokens(name => style.getPropertyValue(name).trim())
}
