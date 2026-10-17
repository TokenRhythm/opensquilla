<template>
  <div
    ref="editorEl"
    class="chat-inline-editor"
    role="textbox"
    aria-multiline="true"
    :aria-label="ariaLabel"
    :aria-describedby="ariaDescribedby"
    :aria-disabled="disabled || undefined"
    :contenteditable="!disabled"
    :data-placeholder="placeholder"
    :data-empty="empty || undefined"
    :tabindex="disabled ? -1 : 0"
    spellcheck="true"
    @input="onInput"
    @beforeinput="onBeforeInput"
    @keydown="onKeydown"
    @compositionstart="onCompositionStart"
    @compositionend="onCompositionEnd"
    @paste="onPaste"
    @drop="onDrop"
    @copy="onCopy"
    @cut="onCut"
    @click="onClick"
    @keyup="rememberSelection"
    @pointerup="rememberSelection"
    @blur="rememberSelection"
  />
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import { editorSelection, readInlineEditor, setEditorSelection, shiftInlineOffset, type InlineSelection } from '@/utils/chat/inlineEditorDom'

const props = withDefaults(defineProps<{
  modelValue: string
  selectedSkills?: readonly SelectedSkillRef[]
  skillPositions?: Readonly<Record<string, number>>
  /** A conversation/draft identity fences late events from a previous editor. */
  draftKey?: string
  placeholder?: string
  disabled?: boolean
  ariaLabel?: string
  ariaDescribedby?: string
  maxlength?: number
}>(), { selectedSkills: () => [], draftKey: '', maxlength: 100000 })

const emit = defineEmits<{
  'update:modelValue': [value: string]
  removeSkill: [instanceId: string]
  input: [event: Event]
  beforeinput: [event: InputEvent]
  keydown: [event: KeyboardEvent]
  compositionChange: [value: boolean]
  selectionChange: [selection: InlineSelection]
  positionsChange: [positions: Record<string, number>]
}>()

const { t } = useI18n()
const editorEl = ref<HTMLDivElement | null>(null)
const empty = ref(true)
let composing = false
let composingKey = ''
let compositionBaseModel = ''
let ignoreCompositionEnd = false
let compositionEndedAt = -Infinity
let cachedSelection: InlineSelection = { start: props.modelValue.length, end: props.modelValue.length }
let positions: Record<string, number> = {}
let lastText = props.modelValue
let nativeInsertion = false

function rememberSelection(): InlineSelection {
  if (editorEl.value) cachedSelection = editorSelection(editorEl.value) || cachedSelection
  emit('selectionChange', { ...cachedSelection })
  return { ...cachedSelection }
}

function notifyPositions(next: Record<string, number>) {
  if (samePositions(next, positions)) return
  positions = next
  emit('positionsChange', { ...positions })
}

function samePositions(left: Readonly<Record<string, number>>, right: Readonly<Record<string, number>>) {
  return Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(id => left[id] === right[id])
}

function makeToken(skill: SelectedSkillRef): HTMLButtonElement {
  const token = document.createElement('button')
  token.type = 'button'
  token.className = 'chat-inline-editor__skill'
  token.dataset.skillId = skill.instanceId
  token.contentEditable = 'false'
  token.disabled = props.disabled
  token.setAttribute('aria-label', t('chat.skillPalette.remove', { name: skill.name }))
  token.title = t('chat.skillPalette.remove', { name: skill.name })
  const icon = document.createElement('span')
  icon.className = 'chat-inline-editor__skill-icon'
  icon.setAttribute('aria-hidden', 'true')
  icon.textContent = '✦'
  const name = document.createElement('span')
  name.textContent = skill.name
  token.append(icon, name)
  return token
}

function syncFromProps(reset = false) {
  const root = editorEl.value
  if (!root || composing) return
  const current = readInlineEditor(root)
  const currentIds = Object.keys(current.positions)
  const desiredIds = props.selectedSkills.map(skill => skill.instanceId)
  const text = props.modelValue.slice(0, props.maxlength)
  const focusInside = root.contains(document.activeElement)
  const previousSelection = reset ? { start: text.length, end: text.length } : rememberSelection()
  const selection = reset ? previousSelection : {
    start: shiftInlineOffset(previousSelection.start, lastText, text),
    end: shiftInlineOffset(previousSelection.end, lastText, text),
  }
  const nextPositions: Record<string, number> = {}
  for (const skill of props.selectedSkills) {
    const supplied = props.skillPositions?.[skill.instanceId]
    const currentPosition = current.positions[skill.instanceId]
    nextPositions[skill.instanceId] = Math.max(0, Math.min(text.length,
      typeof supplied === 'number' && Number.isFinite(supplied) ? supplied
        : !reset && currentPosition !== undefined ? shiftInlineOffset(currentPosition, lastText, text)
          : !reset ? selection.start : text.length))
  }
  if (!reset && current.text === text && currentIds.length === desiredIds.length && desiredIds.every(id => currentIds.includes(id))
    && samePositions(current.positions, nextPositions)) {
    root.querySelectorAll<HTMLButtonElement>('[data-skill-id]').forEach(token => { token.disabled = !!props.disabled })
    return
  }
  const fragment = document.createDocumentFragment()
  let cursor = 0
  const ordered = props.selectedSkills.map((skill, index) => ({ skill, index, at: nextPositions[skill.instanceId]! }))
    .sort((a, b) => a.at - b.at || a.index - b.index)
  for (const { skill, at } of ordered) {
    // Chromium materializes an empty text node before a first
    // contenteditable=false child as a visible leading newline. Skip empty
    // prefixes so a token-only draft starts on the first line.
    const prefix = text.slice(cursor, at)
    if (prefix) fragment.append(document.createTextNode(prefix))
    fragment.append(makeToken(skill))
    cursor = at
  }
  // A non-empty suffix provides the normal text caret. When a token is the
  // entire draft, leave the root boundary after the token as the caret anchor;
  // an empty text node here makes Chromium synthesize a leading newline before
  // the first contenteditable=false child.
  const suffix = text.slice(cursor)
  if (suffix) fragment.append(document.createTextNode(suffix))
  root.replaceChildren(fragment)
  lastText = text
  cachedSelection = selection
  empty.value = text.length === 0 && desiredIds.length === 0
  notifyPositions(nextPositions)
  if (focusInside) setEditorSelection(root, selection.start, selection.end)
  resize()
}

function publishInput(event: Event) {
  const root = editorEl.value
  if (!root || props.disabled) return
  // Native text undo or a dragged DOM fragment cannot silently re-enable an
  // instruction reference that is absent from the authoritative draft state.
  const selectedIds = new Set(props.selectedSkills.map(skill => skill.instanceId))
  root.querySelectorAll<HTMLElement>('[data-skill-id]').forEach(token => {
    if (!selectedIds.has(token.dataset.skillId || '')) token.remove()
  })
  const snapshot = readInlineEditor(root)
  const text = snapshot.text.slice(0, props.maxlength)
  lastText = text
  empty.value = snapshot.text.length === 0 && Object.keys(snapshot.positions).length === 0
  rememberSelection()
  for (const skill of props.selectedSkills) {
    if (positions[skill.instanceId] !== undefined && snapshot.positions[skill.instanceId] === undefined) emit('removeSkill', skill.instanceId)
  }
  notifyPositions(snapshot.positions)
  if (text !== props.modelValue) emit('update:modelValue', text)
  emit('input', event)
  resize()
  if (text !== snapshot.text) void nextTick(() => syncFromProps())
}

function onInput(event: Event) {
  // A non-composing paste input is already committed by the browser. This also
  // recovers hosts that missed compositionend and would otherwise strand a
  // pasted draft behind a stale IME flag (the prior textarea regression).
  if (composing && event instanceof InputEvent && event.inputType === 'insertFromPaste' && !event.isComposing) {
    composing = false
    emit('compositionChange', false)
  }
  if (composing || nativeInsertion || (event instanceof InputEvent && event.isComposing)) return
  publishInput(event)
}

function onBeforeInput(event: InputEvent) {
  if (props.disabled) { event.preventDefault(); return }
  emit('beforeinput', event)
  if (event.defaultPrevented || composing || event.isComposing || nativeInsertion) return
  if (event.inputType === 'insertParagraph' || event.inputType === 'insertLineBreak') {
    event.preventDefault()
    insertPlainText('\n')
  } else if (event.inputType?.startsWith('insert') && typeof event.data === 'string') {
    const selection = rememberSelection()
    const available = props.maxlength - (getText().length - (selection.end - selection.start))
    if (event.data.length > available) {
      event.preventDefault()
      insertPlainText(event.data.slice(0, Math.max(0, available)))
    }
  }
}

function adjacentToken(direction: 'backward' | 'forward'): HTMLElement | null {
  const root = editorEl.value
  const selection = document.getSelection()
  if (!root || !selection?.isCollapsed || !selection.rangeCount) return null
  const range = selection.getRangeAt(0)
  let node: Node | null = range.startContainer
  const offset = range.startOffset
  if (!root.contains(node)) return null
  if (node.nodeType === Node.TEXT_NODE) {
    if (direction === 'backward' && offset !== 0) return null
    if (direction === 'forward' && offset !== (node.textContent || '').length) return null
    node = direction === 'backward' ? node.previousSibling : node.nextSibling
  } else {
    node = node.childNodes[direction === 'backward' ? offset - 1 : offset] || null
  }
  while (node?.nodeType === Node.TEXT_NODE && !node.textContent) node = direction === 'backward' ? node.previousSibling : node.nextSibling
  return node instanceof HTMLElement && node.dataset.skillId ? node : null
}

function removeToken(token: HTMLElement) {
  const id = token.dataset.skillId
  if (!id || props.disabled || composing) return
  rememberSelection()
  token.remove()
  if (editorEl.value) setEditorSelection(editorEl.value, cachedSelection.start, cachedSelection.end)
  // Reconcile positions and the visible empty state immediately, without waiting
  // for the parent's selectedSkills update or adding token labels to the text.
  publishInput(new InputEvent('input', { inputType: 'deleteContentBackward' }))
}

function onKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  if (composing || event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && performance.now() - compositionEndedAt < 100) {
    event.preventDefault()
    event.stopPropagation()
    return
  }
  const focusedToken = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-skill-id]') : null
  if (focusedToken) {
    // A focused remove button owns Enter/Space. It must never bubble as Send.
    if (['Enter', ' ', 'Backspace', 'Delete'].includes(event.key)) {
      event.preventDefault()
      removeToken(focusedToken)
      focus()
    }
    return
  }
  if ((event.key === 'Backspace' || event.key === 'Delete') && !event.altKey && !event.ctrlKey && !event.metaKey) {
    const token = adjacentToken(event.key === 'Backspace' ? 'backward' : 'forward')
    if (token) {
      event.preventDefault()
      removeToken(token)
      return
    }
  }
  emit('keydown', event)
}

function onCompositionStart() {
  composing = true
  // An IME owns the live DOM until compositionend. Hide the placeholder now
  // without publishing or rebuilding that DOM, then restore it if cancelled.
  empty.value = false
  composingKey = props.draftKey
  compositionBaseModel = props.modelValue
  ignoreCompositionEnd = false
  emit('compositionChange', true)
}

function onCompositionEnd(event: CompositionEvent) {
  if (ignoreCompositionEnd || composingKey !== props.draftKey) {
    composing = false
    ignoreCompositionEnd = false
    syncFromProps(true)
    return
  }
  composing = false
  compositionEndedAt = performance.now()
  emit('compositionChange', false)
  if (props.modelValue !== compositionBaseModel) {
    syncFromProps(true)
    return
  }
  publishInput(event)
  // References may have arrived while an IME owned the DOM. Apply them only
  // after the committed text has propagated to the parent.
  void nextTick(() => syncFromProps())
}

function insertPlainText(value: string, inputType = 'insertText') {
  const root = editorEl.value
  if (!root || props.disabled || composing) return
  const selection = rememberSelection()
  const available = props.maxlength - (getText().length - (selection.end - selection.start))
  const text = value.replace(/\r\n?/g, '\n').slice(0, Math.max(0, available))
  root.focus()
  if (!editorSelection(root)) setEditorSelection(root, selection.start, selection.end)
  // Chromium/WebKit preserve their native undo stack for insertText. The Range
  // fallback is for hosts without this editing command (including DOM tests).
  if (text && typeof document.execCommand === 'function') {
    nativeInsertion = true
    let inserted = false
    try { inserted = document.execCommand('insertText', false, text) } catch { /* Range fallback below. */ }
    finally { nativeInsertion = false }
    if (inserted) {
      publishInput(new InputEvent('input', { inputType, data: text }))
      return
    }
  }
  const current = document.getSelection()
  if (!current?.rangeCount) return
  const range = current.getRangeAt(0)
  range.deleteContents()
  const node = document.createTextNode(text)
  range.insertNode(node)
  range.setStartAfter(node)
  range.collapse(true)
  current.removeAllRanges()
  current.addRange(range)
  publishInput(new InputEvent('input', { inputType, data: text }))
}

function onPaste(event: ClipboardEvent) {
  if (event.defaultPrevented || props.disabled || composing || !event.clipboardData) return
  const text = event.clipboardData.getData('text/plain')
  if (event.clipboardData.files.length) return
  if (!text) {
    if (event.clipboardData.getData('text/html')) event.preventDefault()
    return
  }
  // The composer's document capture listener gets first refusal for files and
  // long-text attachments. Ordinary clipboard HTML is always reduced to text.
  event.preventDefault()
  insertPlainText(text, 'insertFromPaste')
}

function onDrop(event: DragEvent) {
  if (event.defaultPrevented || props.disabled || !event.dataTransfer || event.dataTransfer.files.length) return
  // File drops continue to the composer's attachment handler. Text and links
  // never import foreign markup into the editable document.
  event.preventDefault()
  const text = event.dataTransfer.getData('text/plain')
  if (text) insertPlainText(text, 'insertFromDrop')
}

function copySelection(event: ClipboardEvent): boolean {
  const root = editorEl.value
  const selection = document.getSelection()
  if (!root || !event.clipboardData || !selection?.rangeCount || selection.isCollapsed) return false
  const range = selection.getRangeAt(0)
  if (!root.contains(range.commonAncestorContainer)) return false
  const fragment = document.createElement('div')
  fragment.append(range.cloneContents())
  fragment.querySelectorAll<HTMLElement>('[data-skill-id]').forEach(token => {
    const skill = props.selectedSkills.find(item => item.instanceId === token.dataset.skillId)
    token.replaceWith(document.createTextNode(skill ? `/${skill.name}` : ''))
  })
  event.clipboardData.setData('text/plain', readInlineEditor(fragment).text)
  event.preventDefault()
  return true
}
function onCopy(event: ClipboardEvent) { copySelection(event) }
function onCut(event: ClipboardEvent) {
  if (props.disabled || composing || !copySelection(event)) return
  insertPlainText('', 'deleteByCut')
}
function onClick(event: MouseEvent) {
  const token = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-skill-id]') : null
  if (token) removeToken(token)
  else rememberSelection()
}

function getText() { return editorEl.value ? readInlineEditor(editorEl.value).text : props.modelValue }
function getSelection() { return rememberSelection() }
function getCaret() { return rememberSelection().start }
function setSelectionRange(start: number, end = start) {
  cachedSelection = { start, end }
  if (editorEl.value && !composing) setEditorSelection(editorEl.value, start, end)
}
function resize() {
  const root = editorEl.value
  if (!root) return
  root.style.height = 'auto'
  root.style.height = `${Math.min(root.scrollHeight, 176)}px`
}
function focus() {
  const root = editorEl.value
  if (!root || props.disabled) return
  // A capability menu takes focus away from the editor. Browsers then choose
  // the first caret boundary when the contenteditable is focused again,
  // which puts the caret before a token at offset 0. Restore the logical
  // plain-text selection after focus; setEditorSelection maps an equal offset
  // to the trailing text node, so typing continues after the token.
  const focusFromOutside = document.activeElement !== root
  root.focus()
  if (focusFromOutside && !composing) {
    setEditorSelection(root, cachedSelection.start, cachedSelection.end)
  }
}
function blur() { editorEl.value?.blur() }
function onSelectionChange() {
  const anchor = document.getSelection()?.anchorNode
  if (anchor && editorEl.value?.contains(anchor)) rememberSelection()
}

watch(() => props.draftKey, () => {
  ignoreCompositionEnd = composing
  composing = false
  emit('compositionChange', false)
  positions = {}
  cachedSelection = { start: props.modelValue.length, end: props.modelValue.length }
  syncFromProps(true)
}, { flush: 'post' })
watch(() => [props.modelValue, props.selectedSkills, props.skillPositions, props.disabled], () => syncFromProps(), { deep: true, flush: 'post' })

onMounted(() => {
  const root = editorEl.value!
  Object.defineProperties(root, {
    value: { configurable: true, get: getText },
    selectionStart: { configurable: true, get: getCaret },
    selectionEnd: { configurable: true, get: () => getSelection().end },
    setSelectionRange: { configurable: true, value: setSelectionRange },
  })
  syncFromProps(true)
  document.addEventListener('selectionchange', onSelectionChange)
})
onBeforeUnmount(() => {
  document.removeEventListener('selectionchange', onSelectionChange)
})

defineExpose({ element: () => editorEl.value, focus, blur, getCaret, getSelection, setSelectionRange, resize, getText, insertPlainText, isComposing: () => composing })
</script>

<style scoped>
.chat-inline-editor {
  position: relative;
  box-sizing: border-box;
  width: 100%;
  min-height: 44px;
  max-height: 176px;
  overflow-y: auto;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  font: inherit;
  line-height: 1.6;
  color: var(--text);
  background: transparent;
  border: 0;
  padding: 0;
  cursor: text;
  outline: none;
}
.chat-inline-editor[data-empty]::before {
  content: attr(data-placeholder);
  color: var(--text-muted);
  pointer-events: none;
  position: absolute;
  /* Auto insets keep the static text position inside the host's padding. */
  inset: auto;
}
.chat-inline-editor:focus-visible { border-radius: var(--radius-sm); box-shadow: var(--focus-ring-inset); }
.chat-inline-editor[aria-disabled="true"] { cursor: not-allowed; opacity: 0.6; }
.chat-inline-editor :deep(.chat-inline-editor__skill) {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  margin-inline: 3px;
  padding: 1px 3px;
  border-radius: var(--radius-sm);
  color: var(--text);
  background: var(--bg-surface);
  border: 1px solid var(--border);
  font: inherit;
  font-weight: 500;
  line-height: inherit;
  cursor: pointer;
  vertical-align: baseline;
  white-space: nowrap;
  max-width: 100%;
}
.chat-inline-editor :deep(.chat-inline-editor__skill:hover) { background: var(--bg-surface-2); }
.chat-inline-editor :deep(.chat-inline-editor__skill:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.chat-inline-editor :deep(.chat-inline-editor__skill-icon) { font-size: 0.85em; }
@media (max-width: 600px) {
  .chat-inline-editor { font-size: 16px; min-height: 48px; }
  .chat-inline-editor :deep(.chat-inline-editor__skill) { min-height: 32px; align-items: center; }
}
</style>
