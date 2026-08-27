// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

import {
  RECENT_DRAFT_SESSION_KEY,
  recentDraftSessionKey,
  useChatDraftPersistence,
} from './useChatDraftPersistence'

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
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBe('agent:main:webchat:a')
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
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()
  })

  it('points to only the most recently edited non-empty draft', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const inputText = ref('')
    mount(sessionKey, inputText)

    inputText.value = 'draft A'
    await nextTick()
    sessionKey.value = 'agent:main:webchat:b'
    await nextTick()
    inputText.value = 'draft B'
    await nextTick()

    expect(recentDraftSessionKey()).toBe('agent:main:webchat:b')
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:a')).toBe('draft A')
  })

  it('explicitly discards the recoverable draft without scanning other drafts', async () => {
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:older', 'older draft')
    const sessionKey = ref('agent:main:webchat:recent')
    const inputText = ref('')
    const { api } = mount(sessionKey, inputText)
    inputText.value = 'discard me'
    await nextTick()

    api.discardRecentDraft()

    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:recent')).toBeNull()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:older')).toBe('older draft')
  })

  it('does not recreate a discarded pointer while an explicit new task changes session', async () => {
    const sessionKey = ref('agent:main:webchat:discarded')
    const inputText = ref('')
    const { api } = mount(sessionKey, inputText)
    inputText.value = 'discard before switching'
    await nextTick()

    // Match ChatView's explicit-new ordering: empty and discard before the
    // provisional session key changes.
    inputText.value = ''
    api.clearDraft(sessionKey.value)
    api.discardRecentDraft()
    sessionKey.value = 'agent:main:webchat:fresh'
    await nextTick()

    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:discarded')).toBeNull()
  })

  it('does not delete another session draft when the current session starts a new task', () => {
    const otherKey = 'agent:main:webchat:other'
    localStorage.setItem(`opensquilla.chat.draft:${otherKey}`, 'keep this draft')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, otherKey)
    const sessionKey = ref('agent:main:webchat:current')
    const inputText = ref('')
    const { api } = mount(sessionKey, inputText)

    api.clearDraft(sessionKey.value)

    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBe(otherKey)
    expect(localStorage.getItem(`opensquilla.chat.draft:${otherKey}`)).toBe('keep this draft')
  })

  it('retires a corrupt or stale recovery pointer', () => {
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, '')
    expect(recentDraftSessionKey()).toBe('')
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()

    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, 'not-a-session')
    expect(recentDraftSessionKey()).toBe('')
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()

    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, 'agent:main:webchat:missing')
    expect(recentDraftSessionKey()).toBe('')
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBeNull()
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
