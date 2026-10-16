import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { terminalThemeFromTokens } from './terminalTheme'

const themeDirectory = new URL('../../themes/', import.meta.url)
const themes = readdirSync(themeDirectory).filter(name => existsSync(new URL(`${name}/tokens.css`, themeDirectory)))
  .map(name => [name, readFileSync(new URL(`${name}/tokens.css`, themeDirectory), 'utf8')] as const)
const ansiKeys = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white', 'brightBlack', 'brightRed', 'brightGreen', 'brightYellow', 'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite'] as const

function luminance(hex: string) {
  const linear = [1, 3, 5].map(offset => {
    const channel = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return linear[0]! * 0.2126 + linear[1]! * 0.7152 + linear[2]! * 0.0722
}

function contrast(first: string, second: string) {
  const a = luminance(first)
  const b = luminance(second)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

describe('terminal themes', () => {
  it.each(themes)('uses readable command colors from %s', (_, css) => {
    const tokens = new Map([...css.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(match => [match[1]!, match[2]!.trim()]))
    const theme = terminalThemeFromTokens(token => tokens.get(token) ?? '')
    expect(theme.background).toBe(tokens.get('--bg')!.toLowerCase())
    expect(theme.foreground).toBe(tokens.get('--text')!.toLowerCase())
    // PowerShell uses ANSI bright yellow for the command being edited. Both
    // variants must stay readable on every app theme, including light themes.
    expect(contrast(theme.yellow!, theme.background!)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(theme.brightYellow!, theme.background!)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(theme.cursor!, theme.background!)).toBeGreaterThanOrEqual(4.5)
    for (const key of ansiKeys) expect(theme[key]).toMatch(/^#[\da-f]{6}$/)
    expect(theme.cursorAccent).toBe(theme.background)
    expect(theme.selectionBackground).not.toBe(theme.background)
    expect(theme.selectionForeground).toBe(theme.foreground)
  })

  it('normalizes computed CSS colors to the hex format understood by xterm', () => {
    const tokens: Record<string, string> = { '--bg': 'rgb(250, 250, 250)', '--text': 'rgb(38 38 38)', '--warn': '#abc' }
    const theme = terminalThemeFromTokens(token => tokens[token] ?? '')
    expect(theme.background).toBe('#fafafa')
    expect(theme.foreground).toBe('#262626')
    expect(theme.yellow).toBe('#aabbcc')
  })
})
