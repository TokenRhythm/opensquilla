import { beforeEach, describe, expect, it } from 'vitest'
import {
  activateAssistantActivitySession,
  assistantActivityContinuityKey,
  clearAssistantActivityExpansionState,
  readAssistantActivityDuration,
  readAssistantActivityExpansion,
  readAssistantActivityManualChoice,
  releaseAssistantActivitySession,
  writeAssistantActivityDuration,
  writeAssistantActivityExpansion,
} from './activityDisclosureState'

const key = (session: string, turn = 'turn') => JSON.stringify([session, 'assistant-activity', turn])

beforeEach(() => clearAssistantActivityExpansionState())

describe('retained session activity choices', () => {
  it('keeps main and side activity live independently through mount, reset, and close', () => {
    const main = Symbol('main')
    const side = Symbol('side')
    activateAssistantActivitySession('main', 'gateway-one', main)
    writeAssistantActivityExpansion(key('main'), false, '', true)
    writeAssistantActivityDuration(key('main'), 17)
    activateAssistantActivitySession('side', 'gateway-one', side)
    writeAssistantActivityExpansion(key('side'), true, '', true)
    writeAssistantActivityDuration(key('side'), 9)
    expect(readAssistantActivityManualChoice(key('main'))).toBe(false)
    expect(readAssistantActivityDuration(key('main'))).toBe(17)
    clearAssistantActivityExpansionState('side')
    expect(readAssistantActivityDuration(key('main'))).toBe(17)
    activateAssistantActivitySession('side-two', 'gateway-one', side)
    releaseAssistantActivitySession(side)
    expect(readAssistantActivityManualChoice(key('main'))).toBe(false)
    expect(readAssistantActivityDuration(key('main'))).toBe(17)
    releaseAssistantActivitySession(main)
  })

  it('retiring one owner does not discard a session still mounted by another owner', () => {
    const first = Symbol('first')
    const second = Symbol('second')
    activateAssistantActivitySession('shared', 'gateway-one', first)
    writeAssistantActivityDuration(key('shared'), 33)
    activateAssistantActivitySession('shared', 'gateway-one', second)
    activateAssistantActivitySession('other', 'gateway-one', first)
    expect(readAssistantActivityDuration(key('shared'))).toBe(33)
    releaseAssistantActivitySession(first)
    releaseAssistantActivitySession(second)
    expect(readAssistantActivityDuration(key('shared'))).toBe(0)
  })

  it('restores only manual choices across A/B/A and never restores elapsed or automatic live state', () => {
    activateAssistantActivitySession('a', 'gateway-one')
    const continuity = assistantActivityContinuityKey('a', 'turn')
    writeAssistantActivityExpansion(key('a'), true, continuity, true)
    writeAssistantActivityExpansion(key('a', 'automatic'), true)
    writeAssistantActivityDuration(key('a'), 85, continuity)
    activateAssistantActivitySession('b', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
    writeAssistantActivityExpansion(key('b'), false, '', true)
    activateAssistantActivitySession('a', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a'))).toBe(true)
    expect(readAssistantActivityManualChoice('durable-new-key', continuity)).toBe(true)
    expect(readAssistantActivityExpansion(key('a', 'automatic'), false)).toBe(false)
    expect(readAssistantActivityDuration(key('a'), continuity)).toBe(0)
    activateAssistantActivitySession('b', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('b'))).toBe(false)
  })

  it.each(['gateway-two', null, undefined])('clears all retained choices on delivery change to %s', scope => {
    activateAssistantActivitySession('a', 'gateway-one')
    writeAssistantActivityExpansion(key('a'), true, '', true)
    activateAssistantActivitySession('b', 'gateway-one')
    activateAssistantActivitySession('a', scope)
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
    activateAssistantActivitySession('a', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
  })

  it('retains no choices across sessions without an authorized delivery scope', () => {
    activateAssistantActivitySession('a', null)
    writeAssistantActivityExpansion(key('a'), true, '', true)
    activateAssistantActivitySession('b', null)
    activateAssistantActivitySession('a', null)
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
  })

  it('keeps recent sessions while evicting the oldest beyond six sessions', () => {
    for (let index = 0; index < 7; index++) {
      activateAssistantActivitySession(`s${index}`, 'gateway-one')
      writeAssistantActivityExpansion(key(`s${index}`), true, '', true)
    }
    activateAssistantActivitySession('s5', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('s5'))).toBe(true)
    activateAssistantActivitySession('s0', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('s0'))).toBeUndefined()
  })

  it('bounds a retained session to the latest 256 explicit choices', () => {
    activateAssistantActivitySession('a', 'gateway-one')
    for (let index = 0; index < 260; index++) {
      writeAssistantActivityExpansion(key('a', `turn-${index}`), true, '', true)
    }
    activateAssistantActivitySession('b', 'gateway-one')
    activateAssistantActivitySession('a', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a', 'turn-0'))).toBeUndefined()
    expect(readAssistantActivityManualChoice(key('a', 'turn-259'))).toBe(true)
  })

  it('invalidates a deleted session without discarding another session choices', () => {
    activateAssistantActivitySession('a', 'gateway-one')
    writeAssistantActivityExpansion(key('a'), true, '', true)
    activateAssistantActivitySession('b', 'gateway-one')
    writeAssistantActivityExpansion(key('b'), true, '', true)
    clearAssistantActivityExpansionState('a')
    activateAssistantActivitySession('a', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
    activateAssistantActivitySession('b', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('b'))).toBe(true)
    clearAssistantActivityExpansionState('b')
    expect(readAssistantActivityManualChoice(key('b'))).toBeUndefined()
  })

  it('does not resurrect a cleared snapshot and still retains choices made after reset', () => {
    activateAssistantActivitySession('a', 'gateway-one')
    writeAssistantActivityExpansion(key('a'), true, '', true)
    activateAssistantActivitySession('b', 'gateway-one')
    clearAssistantActivityExpansionState()
    writeAssistantActivityExpansion(key('b'), false, '', true)
    activateAssistantActivitySession('a', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('a'))).toBeUndefined()
    activateAssistantActivitySession('b', 'gateway-one')
    expect(readAssistantActivityManualChoice(key('b'))).toBe(false)
  })
})
