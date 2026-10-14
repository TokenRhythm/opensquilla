// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyTextWithFallback } from './browser'

const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
  else Reflect.deleteProperty(document, 'execCommand')
  document.body.innerHTML = ''
})

function mountControls() {
  const row = document.createElement('button')
  const other = document.createElement('input')
  document.body.append(row, other)
  row.focus()
  return { row, other }
}

function useLegacyClipboard(copy: () => boolean) {
  vi.stubGlobal('navigator', { clipboard: undefined })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(copy) })
  // happy-dom's select does not focus the element, unlike Chromium. Model
  // that browser behavior while retaining real DOM focus and removal.
  const select = HTMLTextAreaElement.prototype.select
  vi.spyOn(HTMLTextAreaElement.prototype, 'select').mockImplementation(function (this: HTMLTextAreaElement) {
    this.focus()
    select.call(this)
  })
}

describe('copyTextWithFallback focus ownership', () => {
  it('restores the row after legacy copy succeeds and removes the textarea', async () => {
    const { row } = mountControls()
    useLegacyClipboard(() => {
      expect(document.activeElement).toBeInstanceOf(HTMLTextAreaElement)
      expect((document.activeElement as HTMLTextAreaElement).value).toBe('session link')
      return true
    })
    await copyTextWithFallback('session link')
    expect(document.activeElement).toBe(row)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it.each(['returns false', 'throws'])('cleans up and restores focus when legacy copy %s', async (failure) => {
    const { row } = mountControls()
    useLegacyClipboard(() => {
      if (failure === 'throws') throw new Error('Clipboard unavailable')
      return false
    })
    await expect(copyTextWithFallback('session link')).rejects.toThrow()
    expect(document.activeElement).toBe(row)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('does not override focus moved by a copy event handler', async () => {
    const { other } = mountControls()
    useLegacyClipboard(() => { other.focus(); return true })
    await copyTextWithFallback('session link')
    expect(document.activeElement).toBe(other)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('does not focus a detached original control', async () => {
    const { row } = mountControls()
    const focus = vi.spyOn(row, 'focus')
    useLegacyClipboard(() => { row.remove(); return true })
    await copyTextWithFallback('session link')
    expect(focus).not.toHaveBeenCalled()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('does not steal focus when modern clipboard resolves after focus moved', async () => {
    const { other } = mountControls()
    let finish!: () => void
    const writeText = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const copy = copyTextWithFallback('session link')
    other.focus()
    finish()
    await copy
    expect(writeText).toHaveBeenCalledExactlyOnceWith('session link')
    expect(document.activeElement).toBe(other)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('restores the latest focused control when modern clipboard rejects into fallback', async () => {
    const { other } = mountControls()
    useLegacyClipboard(() => true)
    let reject!: (error: Error) => void
    vi.stubGlobal('navigator', { clipboard: {
      writeText: () => new Promise<void>((_, fail) => { reject = fail }),
    } })
    const copy = copyTextWithFallback('session link')
    other.focus()
    reject(new Error('Denied'))
    await copy
    expect(document.activeElement).toBe(other)
    expect(document.querySelector('textarea')).toBeNull()
  })
})
