import { ref, watch } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { createSideChatLauncher, parseSideChatCommand, type SideChatLaunchRequest } from '@/modules/sideChatLauncher'
import { useSideChatCommand } from './useSideChatCommand'

function harness() {
  const inputText = ref('/side hello')
  const revision = ref(0)
  watch(inputText, () => revision.value++, { flush: 'sync' })
  const sessionKey = ref('parent-a')
  const state = { identity: 'owner-a', allowed: true, attachments: false, active: true }
  const launcher = createSideChatLauncher()
  const open = vi.fn<(request: SideChatLaunchRequest) => Promise<boolean>>(async () => true)
  launcher.register(open)
  const notify = vi.fn()
  const command = useSideChatCommand({ launcher, inputText, revision, sessionKey,
    identity: () => state.identity, allowed: () => state.allowed, hasAttachments: () => state.attachments,
    active: () => state.active, notify, translate: key => key, resized: vi.fn() })
  return { inputText, revision, sessionKey, state, open, notify, command }
}

describe('local side chat command hand-off', () => {
  it.each(['/side', '/btw', ' /SIDE \n'])('recognizes bare %s without inventing a prompt', text => {
    expect(parseSideChatCommand(text)).toEqual({ text: '' })
  })
  it('preserves multiline prompt text and does not consume similar ordinary input', () => {
    expect(parseSideChatCommand('/btw  hello  there\nnext line ')).toEqual({ text: 'hello  there\nnext line' })
    for (const text of ['/sidebar', '//side', 'use /side', '/sideways hello']) expect(parseSideChatCommand(text)).toBeNull()
  })
  it('clears a draft only after the child confirms acceptance', async () => {
    const f = harness()
    let finish!: (accepted: boolean) => void
    f.open.mockImplementation(request => {
      expect(request.parentKey).toBe('parent-a')
      expect(request.text).toBe('hello')
      expect(request.isCurrent()).toBe(true)
      return new Promise(resolve => { finish = resolve })
    })
    const first = f.command.run(f.inputText.value)
    const duplicate = f.command.run(f.inputText.value)
    expect(f.open).toHaveBeenCalledOnce()
    expect(f.inputText.value).toBe('/side hello')
    finish(true)
    await Promise.all([first, duplicate])
    expect(f.inputText.value).toBe('')
  })
  it('keeps rejected/unknown delivery editable and retries with the same launch identity', async () => {
    const f = harness()
    f.open.mockResolvedValueOnce(false).mockRejectedValueOnce(new Error('creation failed')).mockResolvedValueOnce(true)
    await f.command.run(f.inputText.value)
    const id = f.open.mock.calls[0]![0].requestId
    expect(f.inputText.value).toBe('/side hello')
    await f.command.run(f.inputText.value)
    expect(f.inputText.value).toBe('/side hello')
    await f.command.run(f.inputText.value)
    expect(f.open.mock.calls.map(([request]) => request.requestId)).toEqual([id, id, id])
    expect(f.inputText.value).toBe('')
  })
  it.each(['edit', 'session', 'identity', 'leave', 'attachment', 'policy'] as const)('fences an asynchronous launch after %s', async change => {
    const f = harness()
    let finish!: (accepted: boolean) => void
    f.open.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const result = f.command.run(f.inputText.value)
    const request = f.open.mock.calls[0]![0]
    if (change === 'edit') f.inputText.value = 'new draft'
    if (change === 'session') f.sessionKey.value = 'parent-b'
    if (change === 'identity') f.state.identity = 'owner-b'
    if (change === 'leave') f.state.active = false
    if (change === 'attachment') f.state.attachments = true
    if (change === 'policy') f.state.allowed = false
    expect(request.isCurrent()).toBe(false)
    finish(true)
    await result
    expect(f.inputText.value).toBe(change === 'edit' ? 'new draft' : '/side hello')
  })
  it.each(['nested', 'attachments', 'inactive'] as const)('rejects %s without changing or forwarding the draft', async kind => {
    const f = harness()
    if (kind === 'nested') f.state.allowed = false
    if (kind === 'attachments') f.state.attachments = true
    if (kind === 'inactive') f.state.active = false
    expect(await f.command.run('/btw hello')).toBe(true)
    expect(f.open).not.toHaveBeenCalled()
    expect(f.inputText.value).toBe('/side hello')
    expect(f.notify).toHaveBeenCalledOnce()
  })
  it('does not let an old host unregister a replacement', async () => {
    const launcher = createSideChatLauncher()
    const old = vi.fn(async () => true)
    const next = vi.fn(async () => false)
    const release = launcher.register(old)
    launcher.register(next)
    release()
    expect(await launcher.open({ parentKey: 'a', requestId: 'one', text: '', isCurrent: () => true })).toBe(false)
    expect(old).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledOnce()
  })
})
