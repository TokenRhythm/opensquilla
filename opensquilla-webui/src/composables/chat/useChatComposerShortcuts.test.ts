import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { useChatComposerShortcuts } from './useChatComposerShortcuts'
import type { ChatSlashCommand } from './useChatSlashCommands'
import type { ChatMessage, ChatPendingItem } from '@/types/chat'

// The composable gates the Alt+Arrow queue chords on caret position via
// `e.target instanceof HTMLTextAreaElement`. The unit env is `node` (no DOM), so
// register a minimal stand-in as the global the composable checks against.
class FakeTextArea {
  value = ''
  selectionStart = 0
  selectionEnd = 0

  setSelectionRange(start: number, end: number) {
    this.selectionStart = start
    this.selectionEnd = end
  }
}

beforeEach(() => {
  ;(globalThis as unknown as { HTMLTextAreaElement: unknown }).HTMLTextAreaElement = FakeTextArea
})
afterEach(() => {
  delete (globalThis as unknown as { HTMLTextAreaElement?: unknown }).HTMLTextAreaElement
})

function field(value: string, caret: 'start' | 'end' | 'middle'): FakeTextArea
function field(value: string, caret: number, end?: number): FakeTextArea
function field(value: string, caret: 'start' | 'end' | 'middle' | number, end?: number): FakeTextArea {
  const ta = new FakeTextArea()
  ta.value = value
  const pos = typeof caret === 'number'
    ? caret
    : caret === 'start' ? 0 : caret === 'end' ? value.length : Math.floor(value.length / 2)
  ta.selectionStart = pos
  ta.selectionEnd = typeof end === 'number' ? end : pos
  return ta
}

function harness(over: {
  inputText?: string
  pendingQueue?: ChatPendingItem[]
  canQueueMore?: boolean
  safari?: boolean
  slashOpen?: boolean
  filteredSlashCmds?: ChatSlashCommand[]
} = {}) {
  const inputText = ref(over.inputText ?? '')
  const spies = {
    popPendingTail: vi.fn(() => true),
    enqueuePendingInput: vi.fn(() => true),
    sendCurrentInput: vi.fn(),
    autoResizeTextarea: vi.fn(),
    handleSlashInput: vi.fn(),
    closeSlashMenu: vi.fn(),
    completeSlashCmd: vi.fn(),
    activateSlashCmd: vi.fn(),
  }
  const api = useChatComposerShortcuts({
    inputText,
    composing: ref(false),
    messages: ref<ChatMessage[]>([]),
    pendingQueue: ref<ChatPendingItem[]>(over.pendingQueue ?? []),
    canQueueMore: ref(over.canQueueMore ?? true),
    slashOpen: ref(over.slashOpen ?? false),
    slashIdx: ref(0),
    filteredSlashCmds: ref(over.filteredSlashCmds ?? []),
    isStreaming: ref(false),
    isSafariWebKit: () => over.safari ?? false,
    ...spies,
  })
  return { api, inputText, spies }
}

function keydown(opts: {
  key: string
  altKey?: boolean
  shiftKey?: boolean
  isComposing?: boolean
  keyCode?: number
  target: unknown
}): KeyboardEvent {
  return {
    key: opts.key,
    altKey: opts.altKey ?? false,
    shiftKey: opts.shiftKey ?? false,
    isComposing: opts.isComposing ?? false,
    keyCode: opts.keyCode ?? 0,
    target: opts.target,
    preventDefault: vi.fn(),
  } as unknown as KeyboardEvent
}

function inputEvent(inputType: string, target: unknown): InputEvent {
  return {
    inputType,
    target,
  } as unknown as InputEvent
}

const QUEUE = [{ id: 'q1', text: 'queued' }] as unknown as ChatPendingItem[]

describe('useChatComposerShortcuts', () => {
  describe('Slash completion safety', () => {
    const coding = {
      name: '/coding',
      cmd: '/coding',
      label: '/coding',
      desc: 'Toggle Coding mode',
      aliases: [],
      execution: { action: 'coding.mode' },
    }

    it('uses Tab only to complete the active candidate', () => {
      const { api, spies } = harness({
        inputText: '/co',
        slashOpen: true,
        filteredSlashCmds: [coding],
      })
      const e = keydown({ key: 'Tab', target: field('/co', 'end') })

      api.onTextareaKeydown(e)

      expect(spies.completeSlashCmd).toHaveBeenCalledWith(coding)
      expect(spies.activateSlashCmd).not.toHaveBeenCalled()
      expect(e.preventDefault).toHaveBeenCalled()
    })

    it('routes Enter through exact-aware activation instead of executing directly', () => {
      const { api, spies } = harness({
        inputText: '/co',
        slashOpen: true,
        filteredSlashCmds: [coding],
      })
      const e = keydown({ key: 'Enter', target: field('/co', 'end') })

      api.onTextareaKeydown(e)

      expect(spies.activateSlashCmd).toHaveBeenCalledWith(coding)
      expect(spies.completeSlashCmd).not.toHaveBeenCalled()
      expect(spies.sendCurrentInput).not.toHaveBeenCalled()
      expect(e.preventDefault).toHaveBeenCalled()
    })
  })

  describe('IME composition guard', () => {
    it('does not send on Enter while the IME is composing (isComposing)', () => {
      const { api, spies } = harness({ inputText: '你好' })
      api.onTextareaKeydown(keydown({ key: 'Enter', isComposing: true, target: field('你好', 'end') }))
      expect(spies.sendCurrentInput).not.toHaveBeenCalled()
    })

    it('does not send on Enter during legacy keyCode 229 composition', () => {
      const { api, spies } = harness({ inputText: '你好' })
      api.onTextareaKeydown(keydown({ key: 'Enter', keyCode: 229, target: field('你好', 'end') }))
      expect(spies.sendCurrentInput).not.toHaveBeenCalled()
    })

    it('sends on a plain Enter when not composing', () => {
      const { api, spies } = harness({ inputText: 'hi' })
      const e = keydown({ key: 'Enter', target: field('hi', 'end') })
      api.onTextareaKeydown(e)
      expect(spies.sendCurrentInput).toHaveBeenCalledOnce()
      expect(e.preventDefault).toHaveBeenCalled()
    })
  })

  describe('Alt+Arrow queue chords are caret-gated (preserve macOS paragraph nav)', () => {
    it('enqueues on Alt+ArrowDown only when the caret is at the end', () => {
      const { api, spies } = harness({ inputText: 'line1\nline2', canQueueMore: true })

      const atEnd = keydown({ key: 'ArrowDown', altKey: true, target: field('line1\nline2', 'end') })
      api.onTextareaKeydown(atEnd)
      expect(spies.enqueuePendingInput).toHaveBeenCalledWith('line1\nline2')
      expect(atEnd.preventDefault).toHaveBeenCalled()

      spies.enqueuePendingInput.mockClear()
      const midDraft = keydown({ key: 'ArrowDown', altKey: true, target: field('line1\nline2', 'middle') })
      api.onTextareaKeydown(midDraft)
      expect(spies.enqueuePendingInput).not.toHaveBeenCalled()
      expect(midDraft.preventDefault).not.toHaveBeenCalled() // native Option+ArrowDown paragraph move survives
    })

    it('pops the queue on Alt+ArrowUp only when the caret is at the start', () => {
      const { api, spies } = harness({ pendingQueue: QUEUE })

      const atStart = keydown({ key: 'ArrowUp', altKey: true, target: field('', 'start') })
      api.onTextareaKeydown(atStart)
      expect(spies.popPendingTail).toHaveBeenCalledOnce()
      expect(atStart.preventDefault).toHaveBeenCalled()

      spies.popPendingTail.mockClear()
      const midDraft = keydown({ key: 'ArrowUp', altKey: true, target: field('line1\nline2', 'middle') })
      api.onTextareaKeydown(midDraft)
      expect(spies.popPendingTail).not.toHaveBeenCalled()
      expect(midDraft.preventDefault).not.toHaveBeenCalled() // native Option+ArrowUp paragraph move survives
    })
  })

  describe('Safari textarea undo guard', () => {
    it('repairs Safari historyUndo when undoing a deletion clears the whole draft', () => {
      const { api, inputText } = harness({ inputText: 'hello world', safari: true })
      const ta = field('hello world', 6, 'hello world'.length)

      api.onTextareaBeforeInput(inputEvent('deleteContentBackward', ta))
      ta.value = 'hello '
      ta.setSelectionRange(6, 6)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('deleteContentBackward', ta))

      api.onTextareaBeforeInput(inputEvent('historyUndo', ta))
      ta.value = ''
      ta.setSelectionRange(0, 0)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('historyUndo', ta))

      expect(ta.value).toBe('hello world')
      expect(inputText.value).toBe('hello world')
      expect(ta.selectionStart).toBe(6)
      expect(ta.selectionEnd).toBe('hello world'.length)
    })

    it('keeps the first value in a repeated-delete group', () => {
      const { api, inputText } = harness({ inputText: 'hello world', safari: true })
      const ta = field('hello world', 'end')

      api.onTextareaBeforeInput(inputEvent('deleteContentBackward', ta))
      ta.value = 'hello worl'
      ta.setSelectionRange(10, 10)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('deleteContentBackward', ta))

      api.onTextareaBeforeInput(inputEvent('deleteContentBackward', ta))
      ta.value = 'hello wor'
      ta.setSelectionRange(9, 9)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('deleteContentBackward', ta))

      api.onTextareaBeforeInput(inputEvent('historyUndo', ta))
      ta.value = ''
      ta.setSelectionRange(0, 0)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('historyUndo', ta))

      expect(ta.value).toBe('hello world')
      expect(inputText.value).toBe('hello world')
      expect(ta.selectionStart).toBe('hello world'.length)
      expect(ta.selectionEnd).toBe('hello world'.length)
    })

    it('does not resurrect a stale deletion snapshot after an unrelated draft change', () => {
      const { api, inputText } = harness({ inputText: 'hello world', safari: true })
      const ta = field('hello world', 'end')

      api.onTextareaBeforeInput(inputEvent('deleteContentBackward', ta))
      ta.value = 'hello worl'
      ta.setSelectionRange(10, 10)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('deleteContentBackward', ta))

      ta.value = 'new draft'
      ta.setSelectionRange('new draft'.length, 'new draft'.length)
      inputText.value = ta.value

      api.onTextareaBeforeInput(inputEvent('historyUndo', ta))
      ta.value = ''
      ta.setSelectionRange(0, 0)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('historyUndo', ta))

      expect(ta.value).toBe('')
      expect(inputText.value).toBe('')
    })

    it('leaves non-Safari native undo untouched', () => {
      const { api, inputText } = harness({ inputText: 'hello world', safari: false })
      const ta = field('hello world', 6, 'hello world'.length)

      api.onTextareaBeforeInput(inputEvent('deleteContentBackward', ta))
      ta.value = 'hello '
      ta.setSelectionRange(6, 6)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('deleteContentBackward', ta))

      api.onTextareaBeforeInput(inputEvent('historyUndo', ta))
      ta.value = ''
      ta.setSelectionRange(0, 0)
      inputText.value = ta.value
      api.onTextareaInput(inputEvent('historyUndo', ta))

      expect(ta.value).toBe('')
      expect(inputText.value).toBe('')
    })
  })
})
