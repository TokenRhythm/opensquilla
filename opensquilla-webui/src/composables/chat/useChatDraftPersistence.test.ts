// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, type Ref } from 'vue'

import {
  RECENT_DRAFT_SESSION_KEY,
  recentDraftSessionKey,
  recoverableDraftSessionKey,
  useChatDraftPersistence,
} from './useChatDraftPersistence'

function mount(sessionKey: ReturnType<typeof ref<string>>, inputText: ReturnType<typeof ref<string>>, localPaths?: Ref<string[]>) {
  const scope = effectScope()
  const api = scope.run(() =>
    useChatDraftPersistence({
      sessionKey: sessionKey as ReturnType<typeof ref<string>> & { value: string },
      inputText: inputText as ReturnType<typeof ref<string>> & { value: string },
      localPaths,
    }),
  )!
  return { api, scope }
}

afterEach(() => {
  vi.unstubAllGlobals()
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

  it('preserves a fresh draft during namespace binding even when storage is unavailable', async () => {
    const sessionKey = ref('agent:main:webchat:provisional')
    const inputText = ref('Typing while Hello is delayed')
    const { api, scope } = mount(sessionKey, inputText)
    const denied = () => { throw new Error('storage denied') }
    vi.stubGlobal('localStorage', { getItem: denied, setItem: denied, removeItem: denied })
    try {
      api.rebindCurrentDraft(`agent:main:webchat:guest:${'a'.repeat(64)}:provisional`)
      inputText.value += ', then continuing'
      await nextTick()
      expect(inputText.value).toBe('Typing while Hello is delayed, then continuing')
    } finally { scope.stop() }
  })

  it('does not carry a namespace rebind into an intervening history navigation', async () => {
    const sessionKey = ref('agent:main:webchat:provisional')
    const inputText = ref('Fresh draft')
    const { api, scope } = mount(sessionKey, inputText)
    const historyKey = 'agent:main:webchat:history'
    api.saveDraft(historyKey, 'Existing history reply')
    try {
      api.rebindCurrentDraft(`agent:main:webchat:guest:${'a'.repeat(64)}:provisional`)
      sessionKey.value = historyKey
      await nextTick()
      expect(inputText.value).toBe('Existing history reply')
    } finally { scope.stop() }
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

  it('validates a scoped draft without changing the recent pointer', () => {
    const scopedKey = 'agent:main:webchat:scoped'
    const recentKey = 'agent:main:webchat:recent'
    localStorage.setItem(`opensquilla.chat.draft:${scopedKey}`, 'scoped draft')
    localStorage.setItem(`opensquilla.chat.draft:${recentKey}`, 'recent draft')
    localStorage.setItem(RECENT_DRAFT_SESSION_KEY, recentKey)

    expect(recoverableDraftSessionKey(scopedKey)).toBe(scopedKey)
    expect(recoverableDraftSessionKey('agent:main:webchat:missing')).toBe('')
    expect(recoverableDraftSessionKey('not-a-session')).toBe('')
    expect(localStorage.getItem(RECENT_DRAFT_SESSION_KEY)).toBe(recentKey)
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

describe('skill draft persistence', () => {
  it('restores skill identity with its own session and preserves it through namespace binding', async () => {
    const selectedSkills = ref([{ name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }])
    const sessionKey = ref('agent:main:webchat:one')
    const inputText = ref('Analyze this')
    const scope = effectScope()
    const api = scope.run(() => useChatDraftPersistence({ sessionKey, inputText, selectedSkills }))!
    inputText.value += ' report'
    await nextTick()
    sessionKey.value = 'agent:main:webchat:two'
    await nextTick()
    expect(selectedSkills.value).toEqual([])
    sessionKey.value = 'agent:main:webchat:one'
    await nextTick()
    expect(inputText.value).toBe('Analyze this report')
    expect(selectedSkills.value[0]?.instanceId).toBe('skill:tables')
    api.rebindCurrentDraft('agent:main:webchat:bound')
    await nextTick()
    expect(selectedSkills.value[0]?.instanceId).toBe('skill:tables')
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:one')).toBeNull()
    scope.stop()
    const restoredSkills = ref<typeof selectedSkills.value>([])
    const restoredText = ref('')
    const restoredScope = effectScope()
    restoredScope.run(() => useChatDraftPersistence({ sessionKey, inputText: restoredText, selectedSkills: restoredSkills }))
    expect(restoredSkills.value).toEqual(selectedSkills.value)
    expect(restoredText.value).toBe(inputText.value)
    restoredScope.stop()
  })
})

describe('local path draft persistence', () => {
  it('restores visible text and explicit paths through session switches, namespace binding and refresh', async () => {
    const sessionKey = ref('agent:main:webchat:paths')
    const inputText = ref('')
    const localPaths = ref<string[]>([])
    const { api, scope } = mount(sessionKey, inputText, localPaths)
    inputText.value = 'Compare these files'
    localPaths.value.push('C:\\项目\\report.pdf', 'C:\\Downloads\\other.html')
    await nextTick()

    sessionKey.value = 'agent:main:webchat:other'
    await nextTick()
    expect(inputText.value).toBe('')
    expect(localPaths.value).toEqual([])
    sessionKey.value = 'agent:main:webchat:paths'
    await nextTick()
    expect(inputText.value).toBe('Compare these files')
    expect(localPaths.value).toEqual(['C:\\项目\\report.pdf', 'C:\\Downloads\\other.html'])

    api.rebindCurrentDraft('agent:main:webchat:bound')
    await nextTick()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:paths')).toBeNull()
    scope.stop()
    const restoredText = ref('')
    const restoredPaths = ref<string[]>([])
    const restored = mount(sessionKey, restoredText, restoredPaths)
    expect(restoredText.value).toBe(inputText.value)
    expect(restoredPaths.value).toEqual(localPaths.value)
    restored.scope.stop()
  })

  it('persists a paths-only draft and clears it when the final path is removed', async () => {
    const sessionKey = ref('agent:main:webchat:paths')
    const localPaths = ref<string[]>([])
    const { scope } = mount(sessionKey, ref(''), localPaths)
    localPaths.value.push('C:\\file.pdf')
    await nextTick()
    expect(JSON.parse(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:paths')!))
      .toEqual({ version: 1, text: '', selectedSkills: [], localPaths: ['C:\\file.pdf'] })
    expect(recentDraftSessionKey()).toBe(sessionKey.value)
    scope.stop()

    const restoredPaths = ref<string[]>([])
    const restored = mount(sessionKey, ref(''), restoredPaths)
    expect(restoredPaths.value).toEqual(['C:\\file.pdf'])
    restoredPaths.value.splice(0)
    await nextTick()
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:paths')).toBeNull()
    expect(recentDraftSessionKey()).toBe('')
    restored.scope.stop()
  })

  it('does not replace newly selected paths on first activation with a saved draft', () => {
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:paths', 'old draft')
    const inputText = ref('')
    const localPaths = ref(['C:\\new.pdf'])
    const { scope } = mount(ref('agent:main:webchat:paths'), inputText, localPaths)
    expect(inputText.value).toBe('')
    expect(localPaths.value).toEqual(['C:\\new.pdf'])
    scope.stop()
  })

  it('consumes only an accepted canonical text snapshot, preserving later path changes', async () => {
    const sourceKey = 'agent:main:webchat:source'
    const sessionKey = ref(sourceKey)
    const inputText = ref('Inspect this  ')
    const localPaths = ref(['C:\\first.pdf'])
    const { api, scope } = mount(sessionKey, inputText, localPaths)
    sessionKey.value = 'agent:main:webchat:other'
    await api.consumeAcceptedDraft(sourceKey, { text: 'Inspect this\nC:\\first.pdf', selectedSkills: [] })
    expect(localStorage.getItem(`opensquilla.chat.draft:${sourceKey}`)).toBeNull()

    api.saveDraft(sourceKey, 'Inspect this  ', [], ['C:\\second.pdf'])
    await api.consumeAcceptedDraft(sourceKey, { text: 'Inspect this\nC:\\first.pdf', selectedSkills: [] })
    expect(JSON.parse(localStorage.getItem(`opensquilla.chat.draft:${sourceKey}`)!)).toMatchObject({ localPaths: ['C:\\second.pdf'] })
    scope.stop()
  })

  it('leaves legacy path-looking text as text and supports old consumers without a local path ref', () => {
    const sessionKey = ref('agent:main:webchat:legacy')
    const legacy = 'C:\\Downloads\\report.pdf\nPlease inspect it'
    localStorage.setItem(`opensquilla.chat.draft:${sessionKey.value}`, legacy)
    const inputText = ref('')
    const localPaths = ref<string[]>([])
    const { api, scope } = mount(sessionKey, inputText, localPaths)
    expect(inputText.value).toBe(legacy)
    expect(localPaths.value).toEqual([])
    scope.stop()

    api.saveDraft(sessionKey.value, 'Inspect', [], ['C:\\file.pdf'])
    const fallbackText = ref('')
    const fallback = mount(sessionKey, fallbackText)
    expect(fallbackText.value).toBe('Inspect\nC:\\file.pdf')
    fallback.scope.stop()
  })

  it.each([
    { name: 'empty path', paths: [''] },
    { name: 'untrimmed path', paths: [' C:\\file.pdf'] },
    { name: 'control character', paths: ['C:\\bad\nfile.pdf'] },
    { name: 'overlong path', paths: ['x'.repeat(32_769)] },
    { name: 'overlong total', paths: Array.from({ length: 4 }, () => 'x'.repeat(30_000)) },
  ])('does not restore malformed or oversized path metadata as references: $name', ({ paths }) => {
    const sessionKey = ref('agent:main:webchat:invalid')
    const raw = JSON.stringify({ version: 1, text: '', selectedSkills: [], localPaths: paths })
    localStorage.setItem(`opensquilla.chat.draft:${sessionKey.value}`, raw)
    const inputText = ref('')
    const localPaths = ref<string[]>([])
    const { scope } = mount(sessionKey, inputText, localPaths)
    expect(localPaths.value).toEqual([])
    expect(inputText.value).toBe(raw)
    scope.stop()
  })

  it('uses the existing capped text fallback for overlong drafts without dropping only the paths', async () => {
    const sessionKey = ref('agent:main:webchat:other')
    const { api, scope } = mount(sessionKey, ref(''), ref<string[]>([]))
    const text = 'x'.repeat(99_990)
    const canonical = `${text}\nC:\\file.pdf`
    api.saveDraft('agent:main:webchat:source', text, [], ['C:\\file.pdf'])
    expect(api.loadDraft('agent:main:webchat:source')).toBe(canonical.slice(0, 100_000))
    await api.consumeAcceptedDraft('agent:main:webchat:source', { text: canonical, selectedSkills: [] })
    expect(api.loadDraft('agent:main:webchat:source')).toBe('')
    scope.stop()
  })
})

it('consumes only the exact accepted offscreen skill draft after the session watcher saves it', async () => {
  const skills = [{ name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }]
  const sessionKey = ref('agent:main:webchat:source')
  const inputText = ref('Pending request')
  const selectedSkills = ref(skills)
  const scope = effectScope()
  const api = scope.run(() => useChatDraftPersistence({ sessionKey, inputText, selectedSkills }))!
  sessionKey.value = 'agent:main:webchat:other'
  await api.consumeAcceptedDraft('agent:main:webchat:source', { text: 'Pending request', selectedSkills: skills })
  expect(api.loadDraft('agent:main:webchat:source')).toBe('')
  api.saveDraft('agent:main:webchat:source', 'Newer draft', skills)
  await api.consumeAcceptedDraft('agent:main:webchat:source', { text: 'Pending request', selectedSkills: skills })
  expect(api.loadDraft('agent:main:webchat:source')).toBe('Newer draft')
  api.saveDraft('agent:main:webchat:source', 'Pending request', [{ ...skills[0]!, digest: 'b'.repeat(64) }])
  await api.consumeAcceptedDraft('agent:main:webchat:source', { text: 'Pending request', selectedSkills: skills })
  expect(api.loadDraft('agent:main:webchat:source')).toBe('Pending request')
  scope.stop()
})
