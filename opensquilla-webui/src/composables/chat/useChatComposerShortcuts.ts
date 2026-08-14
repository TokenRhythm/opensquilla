import { ref, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatPendingItem,
} from '@/types/chat'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'

export interface UseChatComposerShortcutsOptions {
  inputText: Ref<string>
  composing: Ref<boolean>
  messages: Ref<ChatMessage[]>
  pendingQueue: Ref<ChatPendingItem[]>
  canQueueMore: Ref<boolean>
  slashOpen: Ref<boolean>
  slashIdx: Ref<number>
  filteredSlashCmds: Ref<ChatSlashCommand[]>
  isStreaming: Ref<boolean>
  autoResizeTextarea: () => void
  handleSlashInput: () => void
  closeSlashMenu: () => void
  completeSlashCmd: (cmd: ChatSlashCommand) => void
  activateSlashCmd: (cmd: ChatSlashCommand) => void
  popPendingTail: () => boolean
  enqueuePendingInput: (text: string) => boolean | Promise<boolean>
  sendCurrentInput: () => void
  isSafariWebKit?: () => boolean
}

interface TextareaSnapshot {
  value: string
  selectionStart: number
  selectionEnd: number
  afterValue: string
  afterSelectionStart: number
  afterSelectionEnd: number
}

const DELETE_INPUT_TYPES = new Set([
  'deleteByCut',
  'deleteByDrag',
  'deleteContent',
  'deleteContentBackward',
  'deleteContentForward',
  'deleteHardLineBackward',
  'deleteHardLineForward',
  'deleteSoftLineBackward',
  'deleteSoftLineForward',
  'deleteWordBackward',
  'deleteWordForward',
])

export function useChatComposerShortcuts(options: UseChatComposerShortcutsOptions) {
  const inputHistoryIdx = ref<number | null>(null)
  const inputHistoryDraft = ref('')
  let deleteUndoSnapshot: TextareaSnapshot | null = null
  let pendingUndoRepair: TextareaSnapshot | null = null

  function resetInputHistory() {
    inputHistoryIdx.value = null
    inputHistoryDraft.value = ''
    clearTextareaUndoState()
  }

  function onTextareaBeforeInput(event: InputEvent) {
    updateTextareaUndoStateBeforeInput(event)
  }

  function onTextareaInput(event?: Event) {
    updateTextareaUndoStateAfterInput(event)
    options.autoResizeTextarea()
    options.handleSlashInput()
  }

  function onTextareaKeydown(e: KeyboardEvent) {
    if (options.composing.value || e.isComposing || e.keyCode === 229) return

    // Caret position gates the Alt+Arrow queue chords below: claim them only at
    // the text boundaries so macOS' native Option+ArrowUp/Down ("move by
    // paragraph") still works mid-draft. The natural flow is unaffected — the
    // caret sits at the end after typing (push) and at the start of an empty or
    // short draft (pop) — and on Win/Linux, where Alt+Arrow has no native
    // binding, those boundaries are reached in the same common cases.
    const field = e.target instanceof HTMLTextAreaElement ? e.target : null
    const caretAtStart = !!field && field.selectionStart === 0 && field.selectionEnd === 0
    const caretAtEnd = !!field
      && field.selectionStart === field.value.length
      && field.selectionEnd === field.value.length

    if (options.slashOpen.value) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        options.slashIdx.value = Math.min(options.slashIdx.value + 1, options.filteredSlashCmds.value.length - 1)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        options.slashIdx.value = Math.max(options.slashIdx.value - 1, 0)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        const candidate = options.filteredSlashCmds.value[options.slashIdx.value]
        if (!candidate) return
        e.preventDefault()
        clearTextareaUndoState()
        if (e.key === 'Tab') {
          options.completeSlashCmd(candidate)
        } else {
          options.activateSlashCmd(candidate)
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        options.closeSlashMenu()
        return
      }
    }

    if (e.key === 'Escape' && !options.isStreaming.value && options.pendingQueue.value.length === 0 && options.inputText.value) {
      e.preventDefault()
      clearTextareaUndoState()
      options.inputText.value = ''
      options.autoResizeTextarea()
      return
    }

    if (e.key === 'ArrowUp' && e.altKey && caretAtStart && options.pendingQueue.value.length > 0) {
      e.preventDefault()
      clearTextareaUndoState()
      options.popPendingTail()
      return
    }

    if (e.key === 'ArrowDown' && e.altKey && caretAtEnd && options.inputText.value && options.canQueueMore.value) {
      e.preventDefault()
      clearTextareaUndoState()
      options.enqueuePendingInput(options.inputText.value)
      return
    }

    if (e.key === 'ArrowUp' && !e.altKey && !e.shiftKey && (!options.inputText.value || inputHistoryIdx.value !== null)) {
      if (cycleHistory(-1)) { e.preventDefault(); return }
    }
    if (e.key === 'ArrowDown' && !e.altKey && !e.shiftKey && inputHistoryIdx.value !== null) {
      if (cycleHistory(1)) { e.preventDefault(); return }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      clearTextareaUndoState()
      options.sendCurrentInput()
    }
  }

  function updateTextareaUndoStateBeforeInput(event: InputEvent) {
    if (!shouldUseSafariUndoGuard()) return
    const inputType = inputTypeOf(event)
    const field = textareaFromEvent(event)
    if (!field) {
      clearTextareaUndoState()
      return
    }

    if (DELETE_INPUT_TYPES.has(inputType)) {
      pendingUndoRepair = null
      if (!deleteUndoSnapshot || field.value !== deleteUndoSnapshot.afterValue) {
        deleteUndoSnapshot = snapshotTextarea(field)
      }
      return
    }

    if (inputType === 'historyUndo') {
      pendingUndoRepair = deleteUndoSnapshot && field.value === deleteUndoSnapshot.afterValue
        ? { ...deleteUndoSnapshot }
        : null
      if (!pendingUndoRepair) clearTextareaUndoState()
      return
    }

    clearTextareaUndoState()
  }

  function updateTextareaUndoStateAfterInput(event?: Event) {
    if (!shouldUseSafariUndoGuard()) return
    const inputType = inputTypeOf(event)
    const field = textareaFromEvent(event)

    if (pendingUndoRepair && field && inputType === 'historyUndo') {
      const snap = pendingUndoRepair
      pendingUndoRepair = null
      if (field.value === '' && snap.value !== '' && field.value !== snap.value) {
        restoreTextarea(field, snap)
        clearTextareaUndoState()
        return
      }
      clearTextareaUndoState()
      return
    }

    if (field && DELETE_INPUT_TYPES.has(inputType) && deleteUndoSnapshot) {
      deleteUndoSnapshot.afterValue = field.value
      deleteUndoSnapshot.afterSelectionStart = field.selectionStart
      deleteUndoSnapshot.afterSelectionEnd = field.selectionEnd
      return
    }

    if (inputType) clearTextareaUndoState()
  }

  function shouldUseSafariUndoGuard(): boolean {
    if (options.isSafariWebKit) return options.isSafariWebKit()
    if (typeof navigator === 'undefined') return false
    const ua = navigator.userAgent || ''
    const vendor = navigator.vendor || ''
    const appleWebKit = /AppleWebKit/i.test(ua)
    const safari = /Safari/i.test(ua)
    const excluded = /Chrome|CriOS|FxiOS|Edg|OPR|DuckDuckGo/i.test(ua)
    return appleWebKit && safari && !excluded && /Apple/i.test(vendor)
  }

  function inputTypeOf(event?: Event): string {
    const inputType = (event as { inputType?: unknown } | undefined)?.inputType
    return typeof inputType === 'string' ? inputType : ''
  }

  function textareaFromEvent(event?: Event): HTMLTextAreaElement | null {
    const target = event?.target
    return target instanceof HTMLTextAreaElement ? target : null
  }

  function snapshotTextarea(field: HTMLTextAreaElement): TextareaSnapshot {
    return {
      value: field.value,
      selectionStart: field.selectionStart,
      selectionEnd: field.selectionEnd,
      afterValue: field.value,
      afterSelectionStart: field.selectionStart,
      afterSelectionEnd: field.selectionEnd,
    }
  }

  function restoreTextarea(field: HTMLTextAreaElement, snap: TextareaSnapshot) {
    field.value = snap.value
    options.inputText.value = snap.value
    if (typeof field.setSelectionRange === 'function') {
      field.setSelectionRange(snap.selectionStart, snap.selectionEnd)
    }
  }

  function clearTextareaUndoState() {
    deleteUndoSnapshot = null
    pendingUndoRepair = null
  }

  function cycleHistory(dir: number): boolean {
    const history = options.messages.value
      .filter(message => message.role === 'user' && typeof message.text === 'string')
      .map(message => message.text)
    if (history.length === 0) return false

    if (dir < 0) {
      clearTextareaUndoState()
      if (inputHistoryIdx.value === null) {
        inputHistoryDraft.value = options.inputText.value || ''
        inputHistoryIdx.value = history.length - 1
      } else {
        inputHistoryIdx.value = Math.max(0, inputHistoryIdx.value - 1)
      }
      options.inputText.value = history[inputHistoryIdx.value]
      options.autoResizeTextarea()
      return true
    }

    if (inputHistoryIdx.value === null) return false
    clearTextareaUndoState()
    const next = inputHistoryIdx.value + 1
    if (next >= history.length) {
      inputHistoryIdx.value = null
      options.inputText.value = inputHistoryDraft.value
      inputHistoryDraft.value = ''
    } else {
      inputHistoryIdx.value = next
      options.inputText.value = history[next]
    }
    options.autoResizeTextarea()
    return true
  }

  return {
    onTextareaBeforeInput,
    onTextareaInput,
    onTextareaKeydown,
    resetInputHistory,
  }
}
