// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref } from 'vue'
import i18n from '@/i18n'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: '',
  'onUpdate:modelValue': () => {},
  attachments: [],
  busySendMode: 'queue' as const,
  hasSendContent: false,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Send',
  runMode: 'safe' as const,
  runModeLocked: false,
  runModeLockMessage: '',
  allowedRunModes: ['safe' as const, 'full' as const],
  sessionRoutingMode: 'off' as const,
  sessionRoutingBusy: false,
  routerVisualEffectsEnabled: true,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

async function mountComposer() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, BASE_PROPS)
  app.use(i18n)
  const vm = app.mount(el) as unknown as { focusTextarea: (options?: { preserveFocus?: boolean }) => void }
  await nextTick()
  const textarea = el.querySelector<HTMLDivElement>('.chat-inline-editor')
  expect(textarea).toBeTruthy()
  return { app, textarea: textarea as HTMLDivElement, vm }
}

const unmounts: Array<() => void> = []
const skillCommands: ChatSlashCommand[] = ['deep-research', 'visualize'].map(name => ({
  kind: 'skill', name, cmd: `/${name}`, label: name, desc: '', aliases: [],
  skill: {
    name, instanceId: name, digest: `${name}-digest`, generation: 1,
    description: '', aliases: [], kind: 'skill', source: 'bundled',
    disabled: false, manualOnly: false, ready: true,
  },
}))

interface ComposerApi {
  focusTextarea(options?: { preserveFocus?: boolean }): void
  getSelection(): { start: number; end: number }
  setSelectionRange(start: number, end: number): void
}

async function mountSkillComposer(text = '') {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  const state = reactive({ text, skills: [] as SelectedSkillRef[], positions: {} as Record<string, number> })
  const api = ref<ComposerApi>()
  const keydowns = vi.fn()
  const compositionChanges = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ setup: () => () => h(ChatComposer, {
    ...BASE_PROPS,
    ref: api,
    modelValue: state.text,
    selectedSkills: state.skills,
    skillPositions: state.positions,
    skillPaletteItems: skillCommands,
    'onUpdate:modelValue': (value: string) => { state.text = value },
    onPositionsChange: (positions: Record<string, number>) => { state.positions = positions },
    onKeydown: keydowns,
    onCompositionChange: compositionChanges,
    // Reproduce ChatView's parent feedback and deferred focus, rather than
    // calling the inline editor directly and missing the wrapper regression.
    onSelectSkill: async (command: ChatSlashCommand) => {
      const skill = command.skill!
      state.positions[skill.instanceId] = api.value!.getSelection().start
      state.skills.push({ name: skill.name, instanceId: skill.instanceId, digest: skill.digest })
      await nextTick()
      api.value!.focusTextarea()
    },
  }) })
  app.use(i18n)
  app.mount(host)
  unmounts.push(() => app.unmount())
  await nextTick()
  const editor = host.querySelector<HTMLDivElement>('.chat-inline-editor')!
  const vm = api.value!
  vm.focusTextarea()
  await nextTick()

  async function selectSkill(name: string) {
    host.querySelector<HTMLButtonElement>('.chat-plus-btn')!.click()
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(host.querySelector('.composer-add-menu input'))
    const row = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes(name))!
    expect(row).toBeTruthy()
    row.click()
    // Menu teardown, selected skill render, and ChatView's two deferred focus
    // boundaries all have to finish before inspecting the browser selection.
    await nextTick()
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(editor)
  }
  async function paste(value: string) {
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', value)
    editor.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }))
    await nextTick()
  }
  return { editor, state, vm, selectSkill, paste, keydowns, compositionChanges }
}

afterEach(() => {
  unmounts.splice(0).forEach(unmount => unmount())
  vi.restoreAllMocks()
  document.getSelection()?.removeAllRanges()
  document.body.innerHTML = ''
})

describe('ChatComposer capability menu caret restoration', () => {
  it('continues after a skill selected into an empty composer', async () => {
    const { editor, state, selectSkill, paste } = await mountSkillComposer()
    await selectSkill('deep-research')

    expect(document.getSelection()?.anchorNode).toBe(editor)
    expect(document.getSelection()?.anchorOffset).toBe(1)
    await paste('Continue')
    const token = editor.querySelector('[data-skill-id="deep-research"]')!
    expect(editor.firstChild).toBe(token)
    expect(token.nextSibling?.textContent).toBe('Continue')
    expect(state.text).toBe('Continue')
    expect(state.positions).toEqual({ 'deep-research': 0 })
  })

  it('preserves the middle of existing text when a skill is selected', async () => {
    const { editor, state, vm, selectSkill, paste } = await mountSkillComposer('before after')
    vm.setSelectionRange(7, 7)
    await selectSkill('deep-research')

    const token = editor.querySelector('[data-skill-id="deep-research"]')!
    expect(token.previousSibling?.textContent).toBe('before ')
    expect(token.nextSibling?.textContent).toBe('after')
    expect(document.getSelection()?.anchorNode).toBe(token.nextSibling)
    expect(document.getSelection()?.anchorOffset).toBe(0)
    await paste('inserted ')
    expect(state.text).toBe('before inserted after')
    const children = [...editor.childNodes]
    expect(children.slice(children.indexOf(token) + 1).map(node => node.textContent).join('')).toBe('inserted after')
    expect(state.positions).toEqual({ 'deep-research': 7 })
  })

  it('continues after all consecutively selected skills at the same text position', async () => {
    const { editor, state, selectSkill, paste } = await mountSkillComposer()
    await selectSkill('deep-research')
    await selectSkill('visualize')

    expect([...editor.querySelectorAll<HTMLElement>('[data-skill-id]')].map(token => token.dataset.skillId))
      .toEqual(['deep-research', 'visualize'])
    expect(document.getSelection()?.anchorNode).toBe(editor)
    expect(document.getSelection()?.anchorOffset).toBe(2)
    await paste('Explain')
    expect(editor.querySelector('[data-skill-id="visualize"]')?.nextSibling?.textContent).toBe('Explain')
    expect(state.text).toBe('Explain')
    expect(state.positions).toEqual({ 'deep-research': 0, visualize: 0 })
  })

  it('commits Chinese after the restored skill and does not forward IME confirmation as Send', async () => {
    let time = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => time)
    const { editor, state, selectSkill, keydowns, compositionChanges } = await mountSkillComposer()
    await selectSkill('deep-research')
    const token = editor.querySelector('[data-skill-id="deep-research"]')!
    editor.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    // Model the native IME's DOM insertion at the restored browser caret.
    const range = document.getSelection()!.getRangeAt(0)
    const composingText = document.createTextNode('中文输入')
    range.insertNode(composingText)
    range.setStart(composingText, composingText.length)
    range.collapse(true)
    editor.dispatchEvent(new InputEvent('input', { inputType: 'insertCompositionText', isComposing: true, bubbles: true }))
    editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true }))
    expect(state.text).toBe('')
    expect(keydowns).not.toHaveBeenCalled()

    editor.dispatchEvent(new CompositionEvent('compositionend', { data: '中文输入', bubbles: true }))
    await nextTick()
    expect(state.text).toBe('中文输入')
    expect(token.nextSibling?.textContent).toBe('中文输入')
    expect(state.positions).toEqual({ 'deep-research': 0 })
    expect(compositionChanges.mock.calls).toEqual([[true], [false]])
    const commitEnter = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    editor.dispatchEvent(commitEnter)
    expect(commitEnter.defaultPrevented).toBe(true)
    expect(keydowns).not.toHaveBeenCalled()
    time += 101
    editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(keydowns).toHaveBeenCalledOnce()
  })
})

describe('ChatComposer focus contract', () => {
  it('automatically focuses a cold page whose body still owns focus', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { app, textarea, vm } = await mountComposer()
    expect(document.activeElement).toBe(document.body)

    vm.focusTextarea({ preserveFocus: true })
    await nextTick()

    expect(document.activeElement).toBe(textarea)
    app.unmount()
  })

  it('preserves an external control restored by an overlay while allowing explicit composer focus', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { app, textarea, vm } = await mountComposer()
    const settings = document.createElement('button')
    settings.textContent = 'Settings'
    document.body.appendChild(settings)
    settings.focus()

    vm.focusTextarea({ preserveFocus: true })
    await nextTick()
    expect(document.activeElement).toBe(settings)

    vm.focusTextarea()
    await nextTick()
    expect(document.activeElement).toBe(textarea)
    app.unmount()
  })

  it('rechecks an external focus change before automatic focus runs', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { app, vm } = await mountComposer()
    const settings = document.createElement('button')
    document.body.appendChild(settings)

    vm.focusTextarea({ preserveFocus: true })
    settings.focus()
    await nextTick()

    expect(document.activeElement).toBe(settings)
    app.unmount()
  })

  it('focuses the textarea while the page is visible and active', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    await nextTick()

    expect(focus).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    ['hidden', true],
    ['visible', false],
  ] as const)('does not focus when visibility is %s and window focus is %s', async (visibility, hasFocus) => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(visibility)
    vi.spyOn(document, 'hasFocus').mockReturnValue(hasFocus)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    await nextTick()

    expect(focus).not.toHaveBeenCalled()
    app.unmount()
  })

  it('rechecks page activity before the deferred focus runs', async () => {
    let visibility: DocumentVisibilityState = 'visible'
    let hasFocus = true
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
    vi.spyOn(document, 'hasFocus').mockImplementation(() => hasFocus)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    visibility = 'hidden'
    hasFocus = false
    await nextTick()

    expect(focus).not.toHaveBeenCalled()
    app.unmount()
  })
})
