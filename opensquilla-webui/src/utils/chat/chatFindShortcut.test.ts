// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { isChatFindShortcut } from './chatFindShortcut'

describe('chat find platform shortcut', () => {
  it.each([
    ['Windows Ctrl+F', false, { ctrlKey: true }, true],
    ['Windows Meta+F', false, { metaKey: true }, false],
    ['macOS Cmd+F', true, { metaKey: true }, true],
    ['macOS text-editing Ctrl+F', true, { ctrlKey: true }, false],
    ['Windows Ctrl+Meta+F', false, { ctrlKey: true, metaKey: true }, false],
    ['macOS Ctrl+Meta+F', true, { ctrlKey: true, metaKey: true }, false],
    ['Windows Ctrl+Shift+F', false, { ctrlKey: true, shiftKey: true }, false],
    ['macOS Cmd+Shift+F', true, { metaKey: true, shiftKey: true }, false],
    ['Windows Ctrl+Alt+F', false, { ctrlKey: true, altKey: true }, false],
    ['macOS Cmd+Alt+F', true, { metaKey: true, altKey: true }, false],
    ['plain F on Windows', false, {}, false],
    ['plain F on macOS', true, {}, false],
  ] as const)('%s only opens chat find for its primary modifier', (_label, mac, modifiers, expected) => {
    const event = new KeyboardEvent('keydown', { key: 'f', ...modifiers, cancelable: true })
    const editor = document.createElement('textarea')
    let opened = 0
    editor.addEventListener('keydown', received => {
      if (!isChatFindShortcut(received, mac)) return
      received.preventDefault()
      opened++
    })
    editor.dispatchEvent(event)
    expect(opened).toBe(expected ? 1 : 0)
    expect(event.defaultPrevented).toBe(expected)
  })

  it('keeps matching case-insensitive without accepting another key', () => {
    expect(isChatFindShortcut(new KeyboardEvent('keydown', { key: 'F', ctrlKey: true }), false)).toBe(true)
    expect(isChatFindShortcut(new KeyboardEvent('keydown', { key: 'g', ctrlKey: true }), false)).toBe(false)
  })
})
