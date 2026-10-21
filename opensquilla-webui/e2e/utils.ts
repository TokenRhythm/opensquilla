import { expect, type Locator } from '@playwright/test'

// Shared e2e helpers. One relative-luminance implementation for every spec
// that asserts on computed colors — fix parsing quirks here, once.

/** Approximate relative luminance (0..1) of a computed `rgb()`/`rgba()` color. */
export function relativeLuminance(color: string): number {
  const m = color.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/)
  if (!m) throw new Error(`Unexpected computed color: ${color}`)
  const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])]
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
}

/**
 * Read the logical composer draft from the inline contenteditable editor.
 * Playwright's toHaveValue is intentionally limited to native form controls;
 * ChatInlineEditor exposes the same logical value through its DOM getter while
 * rendering capability tokens as contenteditable=false children.
 */
export async function expectComposerValue(locator: Locator, expected: string): Promise<void> {
  await expect.poll(() => locator.evaluate(element => {
    const value = (element as HTMLElement & { value?: unknown }).value
    return typeof value === 'string' ? value : element.textContent ?? ''
  })).toBe(expected)
}
