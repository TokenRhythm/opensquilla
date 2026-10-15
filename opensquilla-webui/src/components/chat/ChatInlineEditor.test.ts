// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref } from 'vue'
import i18n from '@/i18n'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import ChatInlineEditor from './ChatInlineEditor.vue'

const visual: SelectedSkillRef = { name: 'Visualize', instanceId: 'visual-1', digest: 'digest-1' }
interface EditorApi {
  getText(): string
  getCaret(): number
  getSelection(): { start: number; end: number }
  setSelectionRange(start: number, end?: number): void
  insertPlainText(text: string): void
  focus(): void
}
const unmounts: Array<() => void> = []

async function mountEditor(initial: { text?: string; skills?: SelectedSkillRef[]; positions?: Record<string, number> } = {}) {
  const state = reactive({
    text: initial.text || '', skills: initial.skills || [], positions: initial.positions,
    draftKey: 'session-a', disabled: false,
  })
  const keydowns = vi.fn()
  const removed = vi.fn((id: string) => { state.skills = state.skills.filter(skill => skill.instanceId !== id) })
  const composing = vi.fn()
  const observed: string[] = []
  const api = ref<EditorApi>()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ setup: () => () => h(ChatInlineEditor, {
    ref: api,
    modelValue: state.text,
    selectedSkills: state.skills,
    skillPositions: state.positions,
    draftKey: state.draftKey,
    disabled: state.disabled,
    ariaLabel: 'Message',
    placeholder: 'Ask anything',
    'onUpdate:modelValue': (text: string) => { state.text = text },
    onRemoveSkill: removed,
    onKeydown: keydowns,
    onCompositionChange: composing,
    onPositionsChange: (positions: Record<string, number>) => { state.positions = positions },
    onInput: () => observed.push(state.text),
  }) })
  app.use(i18n)
  app.mount(host)
  unmounts.push(() => { app.unmount(); host.remove() })
  await nextTick()
  const editor = host.querySelector<HTMLDivElement>('[role="textbox"]')!
  const vm = api.value!
  vm.focus()
  return { state, editor, vm, keydowns, removed, composing, observed }
}

afterEach(() => {
  unmounts.splice(0).forEach(unmount => unmount())
  vi.restoreAllMocks()
  document.getSelection()?.removeAllRanges()
})

describe('ChatInlineEditor', () => {
  it('keeps capability labels out of text and caret coordinates', async () => {
    const { editor, vm } = await mountEditor({ text: '请解释这个流程', skills: [visual], positions: { 'visual-1': 3 } })
    expect(editor.textContent).toContain('Visualize')
    expect(vm.getText()).toBe('请解释这个流程')
    vm.setSelectionRange(3)
    expect(vm.getCaret()).toBe(3)
    vm.insertPlainText('清楚')
    expect(vm.getText()).toBe('请解释清楚这个流程')
    expect(editor.querySelectorAll('[data-skill-id]')).toHaveLength(1)
  })

  it('restores the caret after a token when focus returns from the capability menu', async () => {
    const { editor, state, vm } = await mountEditor()
    editor.blur()
    state.skills = [visual]
    state.positions = { 'visual-1': 0 }
    await nextTick()

    vm.focus()
    await nextTick()

    const selection = document.getSelection()
    expect(vm.getCaret()).toBe(0)
    expect(editor.firstChild).toBe(editor.querySelector('[data-skill-id]'))
    expect(editor.firstChild?.nodeType).toBe(Node.ELEMENT_NODE)
    expect(selection?.anchorNode).toBe(editor)
    expect(selection?.anchorOffset).toBe(1)

    vm.insertPlainText('继续')
    await nextTick()
    expect(state.text).toBe('继续')
    expect(vm.getText()).toBe('继续')
  })

  it('inserts a new reference where a completed slash token was removed', async () => {
    const { state, editor, vm } = await mountEditor({ text: 'before /vis after' })
    vm.setSelectionRange(11)
    state.text = 'before  after'
    state.skills = [visual]
    await nextTick()
    const token = editor.querySelector('[data-skill-id]')!
    expect(token.previousSibling?.textContent).toBe('before ')
    expect(token.nextSibling?.textContent).toBe(' after')
    expect(vm.getText()).toBe('before  after')
  })

  it('does not publish or redraw an active Chinese composition and guards its commit Enter', async () => {
    let time = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => time)
    const { state, editor, keydowns, observed } = await mountEditor()
    editor.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    const composingNode = document.createTextNode('中文输入')
    editor.replaceChildren(composingNode)
    editor.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertCompositionText', isComposing: true }))
    state.skills = [visual]
    await nextTick()
    expect(editor.hasAttribute('data-empty')).toBe(false)
    expect(editor.firstChild).toBe(composingNode)
    expect(state.text).toBe('')
    editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true }))
    expect(keydowns).not.toHaveBeenCalled()
    editor.dispatchEvent(new CompositionEvent('compositionend', { data: '中文输入', bubbles: true }))
    await nextTick()
    expect(state.text).toBe('中文输入')
    expect(observed).toEqual(['中文输入'])
    expect(editor.querySelector('[data-skill-id]')).not.toBeNull()
    const commitEnter = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    editor.dispatchEvent(commitEnter)
    expect(commitEnter.defaultPrevented).toBe(true)
    expect(keydowns).not.toHaveBeenCalled()
    time += 101
    editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(keydowns).toHaveBeenCalledOnce()
  })

  it('hides the placeholder during composition and restores it after a cancelled empty composition', async () => {
    const { editor, state } = await mountEditor()
    expect(editor.hasAttribute('data-empty')).toBe(true)
    editor.dispatchEvent(new CompositionEvent('compositionstart'))
    await nextTick()
    expect(editor.hasAttribute('data-empty')).toBe(false)
    expect(state.text).toBe('')
    editor.dispatchEvent(new CompositionEvent('compositionend', { data: '' }))
    await nextTick()
    expect(editor.hasAttribute('data-empty')).toBe(true)
    expect(state.text).toBe('')
  })

  it('ignores a late compositionend from the previous conversation', async () => {
    const { state, editor, vm } = await mountEditor({ text: '旧草稿' })
    editor.dispatchEvent(new CompositionEvent('compositionstart'))
    editor.textContent = '旧草稿正在输入'
    state.draftKey = 'session-b'
    state.text = '新会话内容'
    state.skills = [visual]
    state.positions = { 'visual-1': 2 }
    await nextTick()
    // Some IMEs deliver their old committed DOM mutation after a route switch.
    editor.textContent = '旧草稿正在输入'
    editor.dispatchEvent(new CompositionEvent('compositionend', { data: '正在输入' }))
    await nextTick()
    expect(state.text).toBe('新会话内容')
    expect(vm.getText()).toBe('新会话内容')
    expect(editor.querySelector('[data-skill-id]')?.previousSibling?.textContent).toBe('新会')
  })

  it('respects an external draft replacement received during composition', async () => {
    const { state, editor, vm } = await mountEditor({ text: 'before' })
    editor.dispatchEvent(new CompositionEvent('compositionstart'))
    editor.textContent = 'before中文'
    state.text = 'recovered draft'
    await nextTick()
    expect(editor.textContent).toBe('before中文')
    editor.dispatchEvent(new CompositionEvent('compositionend'))
    await nextTick()
    expect(state.text).toBe('recovered draft')
    expect(vm.getText()).toBe('recovered draft')
  })

  it.each(['Backspace', 'Delete'])('removes a reference atomically with %s without losing adjacent text', async key => {
    const { editor, state, vm, removed } = await mountEditor({ text: 'hello world', skills: [visual], positions: { 'visual-1': 5 } })
    const token = editor.querySelector('[data-skill-id]')!
    const range = document.createRange()
    if (key === 'Backspace') range.setStartAfter(token)
    else range.setStartBefore(token)
    range.collapse(true)
    document.getSelection()?.removeAllRanges()
    document.getSelection()?.addRange(range)
    const event = new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true })
    editor.dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(removed).toHaveBeenCalledTimes(1)
    expect(state.skills).toHaveLength(0)
    expect(vm.getText()).toBe('hello world')
  })

  it('reconciles a native selection deletion that included a reference', async () => {
    const { editor, state, removed } = await mountEditor({ text: 'delete me', skills: [visual] })
    editor.replaceChildren(document.createTextNode('me'))
    editor.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }))
    await nextTick()
    expect(state.text).toBe('me')
    expect(removed).toHaveBeenCalledExactlyOnceWith('visual-1')
  })

  it('treats Enter on a focused reference as remove, never as Send', async () => {
    const { editor, state, keydowns, removed } = await mountEditor({ text: 'keep', skills: [visual] })
    const token = editor.querySelector<HTMLButtonElement>('[data-skill-id]')!
    token.focus()
    token.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    await nextTick()
    expect(removed).toHaveBeenCalledExactlyOnceWith('visual-1')
    expect(keydowns).not.toHaveBeenCalled()
    expect(state.text).toBe('keep')
  })

  it('pastes plain text and line breaks without importing clipboard HTML', async () => {
    const { editor, vm, state } = await mountEditor({ text: '你好：' })
    vm.setSelectionRange(3)
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', '第一行\r\n第二行')
    clipboardData.setData('text/html', '<b>第一行</b><script>bad()</script>')
    editor.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }))
    await nextTick()
    expect(state.text).toBe('你好：第一行\n第二行')
    expect(editor.querySelector('b,script')).toBeNull()
  })

  it('leaves a capture-claimed paste to attachment handling', async () => {
    const { editor, state } = await mountEditor({ text: 'keep' })
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', 'attached instead')
    const event = new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true })
    event.preventDefault()
    editor.dispatchEvent(event)
    await nextTick()
    expect(state.text).toBe('keep')
  })

  it('restores skill positions when the draft changes and never leaks prior references', async () => {
    const { state, editor, vm } = await mountEditor({ text: 'old', skills: [visual] })
    state.draftKey = 'different'
    state.text = 'new draft'
    state.skills = [{ ...visual, name: 'Browser', instanceId: 'browser-2' }]
    state.positions = { 'browser-2': 4 }
    await nextTick()
    expect(editor.querySelector('[data-skill-id="visual-1"]')).toBeNull()
    expect(editor.querySelector('[data-skill-id="browser-2"]')?.previousSibling?.textContent).toBe('new ')
    expect(vm.getText()).toBe('new draft')
  })

  it('persists moved reference positions through real model and position feedback', async () => {
    const { state, vm, editor } = await mountEditor({ text: 'before after', skills: [visual], positions: { 'visual-1': 7 } })
    vm.setSelectionRange(0)
    vm.insertPlainText('中文 ')
    await nextTick()
    expect(state.text).toBe('中文 before after')
    expect(state.positions).toEqual({ 'visual-1': 10 })
    expect(editor.querySelector('[data-skill-id]')?.previousSibling?.textContent).toContain('before ')
    const restoredText = state.text
    const restoredPositions = { ...state.positions }
    state.draftKey = 'session-b'
    state.text = 'other'
    state.skills = []
    state.positions = {}
    await nextTick()
    state.draftKey = 'session-a'
    state.text = restoredText
    state.skills = [visual]
    state.positions = restoredPositions
    await nextTick()
    expect(editor.querySelector('[data-skill-id]')?.previousSibling?.textContent).toBe('中文 before ')
    expect(vm.getText()).toBe(restoredText)
  })
})
