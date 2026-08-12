import { describe, expect, it } from 'vitest'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import {
  hasRouterAfterLatestUser,
  projectSessionCreationRouterPresentation,
} from './sessionCreationRouterPresentation'

function message(
  id: string,
  displayRole: string,
  extra: Partial<ChatRenderedMessage> = {},
): ChatRenderedMessage {
  return {
    id,
    role: displayRole,
    displayRole,
    roleLabel: displayRole,
    text: '',
    timeStr: '',
    showHeader: false,
    ...extra,
  }
}

function successfulSpawn(callId: string): ChatToolCall {
  return {
    toolId: callId,
    name: 'sessions_spawn',
    displayName: 'Create chat',
    inputPreview: '',
    isRunning: false,
    isError: false,
    status: 'success',
    result: '{"session_key":"agent:main:subagent:child"}',
    resultPreview: '',
    isOpen: false,
  }
}

function cardOwner(id = 'card'): ChatRenderedMessage {
  return message(id, 'assistant', {
    createdSessionLinks: [{
      callId: 'spawn-1',
      sessionKey: 'agent:main:subagent:child',
    }],
    toolCalls: [successfulSpawn('spawn-1')],
  })
}

describe('session creation router presentation', () => {
  it('keeps the creation route above a live created-chat card', () => {
    const user = message('user', 'user')
    const route = message('creation-route', 'router', { isRouterStrip: true })
    const card = cardOwner()

    const projection = projectSessionCreationRouterPresentation(
      [user, route, card],
      true,
    )

    expect(projection.active).toBe(true)
    expect(projection.messages).toEqual([user, route, card])
    expect(hasRouterAfterLatestUser(projection.messages)).toBe(true)
  })

  it('hides the resumed parent route below the card', () => {
    const projection = projectSessionCreationRouterPresentation([
      message('user', 'user'),
      message('creation-route', 'router', { isRouterStrip: true }),
      cardOwner(),
      message('resume-route', 'router', { isRouterStrip: true }),
    ], true)

    expect(projection.messages.filter(item => item.isRouterStrip).map(item => item.id))
      .toEqual(['creation-route'])
    expect(hasRouterAfterLatestUser(projection.messages)).toBe(true)
  })

  it('uses the last card boundary for multiple child sessions completing together', () => {
    const firstCard = cardOwner('first-card')
    const secondCard = message('second-card', 'assistant', {
      createdSessionLinks: [{
        callId: 'spawn-2',
        sessionKey: 'agent:main:subagent:second',
      }],
      toolCalls: [successfulSpawn('spawn-2')],
    })
    const projection = projectSessionCreationRouterPresentation([
      message('user', 'user'),
      message('first-route', 'router', { isRouterStrip: true }),
      firstCard,
      message('between-cards-route', 'router', { isRouterStrip: true }),
      secondCard,
    ], true)

    expect(projection.messages.filter(item => item.isRouterStrip).map(item => item.id))
      .toEqual(['first-route'])
    expect(projection.messages.filter(item => item.createdSessionLinks?.length)).toHaveLength(2)
  })

  it('does not remove routes from an earlier user interaction', () => {
    const priorRoute = message('prior-route', 'router', { isRouterStrip: true })
    const projection = projectSessionCreationRouterPresentation([
      message('prior-user', 'user'),
      priorRoute,
      message('current-user', 'user'),
      message('current-creation-route', 'router', { isRouterStrip: true }),
      cardOwner(),
    ], true)

    expect(projection.messages.filter(item => item.isRouterStrip).map(item => item.id))
      .toEqual(['prior-route', 'current-creation-route'])
  })

  it('keeps an earlier creation interaction deduplicated after a later user turn', () => {
    const source = cardOwner('creation-source')
    const finalReply = message('creation-final', 'assistant', {
      text: 'Child completed.',
      createdSessionLinks: source.createdSessionLinks,
    })
    source.createdSessionLinks = []
    const projection = projectSessionCreationRouterPresentation([
      message('creation-user', 'user'),
      message('creation-route', 'router', { isRouterStrip: true }),
      source,
      message('resume-route', 'router', { isRouterStrip: true }),
      finalReply,
      message('later-user', 'user'),
      message('later-reply', 'assistant', { text: 'A separate answer.' }),
    ], false)

    expect(projection.messages.filter(item => item.isRouterStrip).map(item => item.id))
      .toEqual(['creation-route'])
  })

  it('leaves settled history and rehomed cards unchanged', () => {
    const route = message('final-route', 'router', { isRouterStrip: true })
    const rehomedCard = message('final-reply', 'assistant', {
      text: 'Done',
      createdSessionLinks: [{
        callId: 'spawn-1',
        sessionKey: 'agent:main:subagent:child',
      }],
    })
    const messages = [message('user', 'user'), route, rehomedCard]

    expect(projectSessionCreationRouterPresentation(messages, false)).toEqual({
      messages,
      active: false,
    })
    expect(projectSessionCreationRouterPresentation(messages, true)).toEqual({
      messages,
      active: false,
    })
  })

  it('keeps only the creation route after a multi-turn session creation settles', () => {
    const visibleCreationReply = cardOwner('first-spawn')
    visibleCreationReply.text = 'Chat created, waiting for completion.'
    const rehomedFinalReply = message('final-reply', 'assistant', {
      text: 'Child completed.',
      createdSessionLinks: visibleCreationReply.createdSessionLinks,
    })
    visibleCreationReply.createdSessionLinks = []
    const projection = projectSessionCreationRouterPresentation([
      message('user', 'user'),
      message('creation-route', 'router', { isRouterStrip: true }),
      visibleCreationReply,
      message('resumed-route', 'router', { isRouterStrip: true }),
      rehomedFinalReply,
    ], false)

    expect(projection.active).toBe(false)
    expect(projection.messages.filter(item => item.isRouterStrip).map(item => item.id))
      .toEqual(['creation-route'])
    expect(projection.messages.filter(item => item.createdSessionLinks?.length)).toHaveLength(1)
    expect(projection.messages.find(item => item.id === 'first-spawn')?.text)
      .toBe('Chat created, waiting for completion.')
  })

  it('does not activate for failed or incomplete spawn results without a card link', () => {
    const messages = [
      message('user', 'user'),
      message('route', 'router', { isRouterStrip: true }),
      message('failed-spawn', 'assistant', {
        toolCalls: [{
          ...successfulSpawn('spawn-failed'),
          isError: true,
          status: 'error',
        }],
      }),
    ]

    expect(projectSessionCreationRouterPresentation(messages, true)).toEqual({
      messages,
      active: false,
    })
  })
})
