// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

import { useChatDraftPersistence } from './useChatDraftPersistence'

function mount(sessionKey: ReturnType<typeof ref<string>>, inputText: ReturnType<typeof ref<string>>) {
  const scope = effectScope()
  const api = scope.run(() =>
    useChatDraftPersistence({
      sessionKey: sessionKey as ReturnType<typeof ref<string>> & { value: string },
      inputText: inputText as ReturnType<typeof ref<string>> & { value: string },
    }),
  )!
  return { api, scope }
}

afterEach(() => {
  localStorage.clear()
})

describe('useChatDraftPersistence', () => {
  it('persists composer text per session and restores it on return', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const inputText = ref('')
    const { scope } = mount(sessionKey, inputText)

    inputText.value = 'half-written instruction'
    await nextTick()

    // Simulate a fresh mount (refresh) for the same session.
    scope.stop()
    const sessionKey2 = ref('agent:main:webchat:a')
    const inputText2 = ref('')
    mount(sessionKey2, inputText2)
    await nextTick()

    expect(inputText2.value).toBe('half-written instruction')
  })

  it('keeps drafts isolated per session and does not clobber typed text', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const inputText = ref('')
    mount(sessionKey, inputText)

    inputText.value = 'draft for A'
    await nextTick()

    // Switch to session B: A's draft must not leak in.
    sessionKey.value = 'agent:main:webchat:b'
    await nextTick()
    expect(inputText.value).toBe('') // B has no draft

    // Type in B, then switch back to A: A's draft is restored.
    inputText.value = 'draft for B'
    await nextTick()
    sessionKey.value = 'agent:main:webchat:a'
    await nextTick()
    expect(inputText.value).toBe('draft for A')

    // Repeated navigation restores B's untouched editor draft.
    sessionKey.value = 'agent:main:webchat:b'
    await nextTick()
    expect(inputText.value).toBe('draft for B')
  })

  it('clears the persisted draft once the composer is emptied (after send)', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const inputText = ref('')
    mount(sessionKey, inputText)

    inputText.value = 'about to send'
    await nextTick()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:a')).toBe('about to send')

    inputText.value = '' // send path empties the composer
    await nextTick()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:a')).toBeNull()
  })

  it('does not overwrite text already typed in the newly-active session', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const inputText = ref('')
    mount(sessionKey, inputText)
    inputText.value = 'saved draft'
    await nextTick()

    // New view already has unsent text when the session resolves — keep it.
    const sessionKey2 = ref('agent:main:webchat:a')
    const inputText2 = ref('user is already typing')
    mount(sessionKey2, inputText2)
    await nextTick()
    expect(inputText2.value).toBe('user is already typing')
  })
})
