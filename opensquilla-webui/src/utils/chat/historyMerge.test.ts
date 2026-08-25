import { describe, it, expect } from 'vitest'
import {
  mergeLiveOnlyFields,
  reconcileHistoryMessages,
  reconcileHistoryWindow,
  reconcileRunningHistoryMessages,
  rehomePromotedSteerRows,
} from './historyMerge'
import type { ChatMessage, ChatReasoning, ChatRenderedMessage } from '@/types/chat'
import { chatMessageKey } from './messageIdentity'

function msg(overrides: Partial<ChatMessage>): ChatMessage {
  return { role: 'assistant', text: '', ts: null, ...overrides } as ChatMessage
}
const reasoning = (seconds: number): ChatReasoning => ({ text: '', seconds })

describe('rehomePromotedSteerRows', () => {
  it('moves promoted rows after completed output and preserves FIFO before the new turn', () => {
    const rows = [
      msg({ role: 'user', messageId: 'user-old', turnId: 'turn-old' }),
      msg({
        role: 'user',
        messageId: 'steer-1',
        turnId: 'turn-new',
        promotedFromTurnId: 'turn-old',
        inputDisposition: 'promoted',
      }),
      msg({
        role: 'user',
        messageId: 'steer-2',
        turnId: 'turn-new',
        promotedFromTurnId: 'turn-old',
        inputDisposition: 'promoted',
      }),
      msg({ role: 'assistant', messageId: 'assistant-old', turnId: 'turn-old' }),
      msg({ role: 'router', messageId: 'router-new', turnId: 'turn-new' }),
    ]

    expect(rehomePromotedSteerRows(rows).map(row => row.messageId)).toEqual([
      'user-old',
      'assistant-old',
      'steer-1',
      'steer-2',
      'router-new',
    ])
  })
})

describe('mergeLiveOnlyFields', () => {
  it('keeps the optimistic identity across the first authoritative replacement', () => {
    const optimistic = msg({ clientId: 'local-turn' })
    const merged = mergeLiveOnlyFields(
      optimistic,
      msg({ messageId: 'server-turn' }),
    )

    expect(merged.clientId).toBe('local-turn')
    expect(chatMessageKey({ ...optimistic, id: 'assistant-0' } as ChatRenderedMessage, 0))
      .toBe(chatMessageKey({ ...merged, id: 'assistant-0' } as ChatRenderedMessage, 0))
  })

  it('keeps live reasoning seconds when the server snapshot measured none', () => {
    const merged = mergeLiveOnlyFields(msg({ reasoning: reasoning(8) }), msg({ reasoning: undefined }))
    expect(merged.reasoning?.seconds).toBe(8)
  })

  it('lets the server win when it measured its own seconds', () => {
    const merged = mergeLiveOnlyFields(msg({ reasoning: reasoning(8) }), msg({ reasoning: reasoning(12) }))
    expect(merged.reasoning?.seconds).toBe(12)
  })

  it('keeps structured reasoning blocks when history only has flattened text', () => {
    const reasoningBlocks = [{
      id: 'reasoning-1',
      index: 0,
      text: 'inspect',
      status: 'completed' as const,
      startedAt: 1_000,
      endedAt: 3_000,
      contentKind: 'reasoning' as const,
    }]
    const merged = mergeLiveOnlyFields(
      msg({ reasoning: { text: 'inspect', seconds: 2 }, reasoningBlocks }),
      msg({ reasoning: { text: 'inspect', seconds: 0 } }),
    )

    expect(merged.reasoningBlocks).toEqual(reasoningBlocks)
    expect(merged.reasoningBlocks).not.toBe(reasoningBlocks)
  })

  it('keeps the live activity snapshot when history has no persisted phases', () => {
    const statusHistory = [
      { action: 'inspect', label: 'Inspecting', at: 1_000 },
      { action: 'write', label: 'Writing', at: 2_000 },
    ]
    const merged = mergeLiveOnlyFields(
      msg({ statusHistory }),
      msg({ statusHistory: undefined }),
    )

    expect(merged.statusHistory).toEqual(statusHistory)
  })

  it('lets a persisted activity snapshot replace the live one', () => {
    const merged = mergeLiveOnlyFields(
      msg({ statusHistory: [{ action: 'inspect', label: 'Inspecting', at: 1_000 }] }),
      msg({ statusHistory: [{ action: 'server', label: 'Server phase', at: 2_000 }] }),
    )

    expect(merged.statusHistory).toEqual([
      { action: 'server', label: 'Server phase', at: 2_000 },
    ])
  })

  it('keeps live task phases when history only adds a durable compaction marker', () => {
    const merged = mergeLiveOnlyFields(
      msg({
        statusHistory: [
          { action: 'inspect', label: 'Inspecting', at: 1_000 },
          {
            action: 'context_compaction',
            label: 'Organizing context',
            category: 'maintenance',
            id: 'compact-1',
            state: 'running',
            at: 1_500,
          },
          { action: 'write', label: 'Writing', at: 2_000 },
        ],
      }),
      msg({
        statusHistory: [{
          action: 'context_compaction',
          label: 'Context organized',
          category: 'maintenance',
          id: 'compact-1',
          state: 'completed',
          durability: 'durable',
          at: 1_800,
        }],
      }),
    )

    expect(merged.statusHistory).toEqual([
      { action: 'inspect', label: 'Inspecting', at: 1_000 },
      {
        action: 'context_compaction',
        label: 'Context organized',
        category: 'maintenance',
        id: 'compact-1',
        state: 'completed',
        durability: 'durable',
        at: 1_500,
      },
      { action: 'write', label: 'Writing', at: 2_000 },
    ])
  })

  it('keeps routerSettled sticky once it has settled', () => {
    expect(mergeLiveOnlyFields(msg({ routerSettled: true }), msg({ routerSettled: undefined })).routerSettled).toBe(true)
  })

  it('keeps the local interrupted flag until the server persists its own', () => {
    expect(mergeLiveOnlyFields(msg({ interrupted: true }), msg({ interrupted: undefined })).interrupted).toBe(true)
    // server defines it (even as false) → the server value wins
    expect(mergeLiveOnlyFields(msg({ interrupted: true }), msg({ interrupted: false })).interrupted).toBe(false)
  })

  it('keeps the ordered local timeline and matching calls for an older server row', () => {
    const timeline = [
      { type: 'text', raw: 'Working note.', presentation: 'intermediate' as const },
      { type: 'tool-group', groupId: 'stream:tool-group:file.read:0' },
      { type: 'text', raw: 'Final answer.', presentation: 'answer' as const },
    ]
    const toolCalls = [{
      id: 'tool-1',
      name: 'read_file',
      groupId: 'stream:tool-group:file.read:0',
      result: 'ok',
    }]

    const merged = mergeLiveOnlyFields(
      msg({ timeline, tool_calls: toolCalls }),
      msg({ timeline: [], tool_calls: [{ id: 'server-tool', name: 'read_file' }] }),
    )

    expect(merged.timeline).toEqual(timeline)
    expect(merged.tool_calls).toEqual(toolCalls)
  })

  it('does not let stale history regress a terminal steer disposition', () => {
    const merged = mergeLiveOnlyFields(
      msg({
        role: 'user',
        turnId: 'turn-new',
        inputDisposition: 'promoted',
        inputDispositionRevision: 2,
      }),
      msg({
        role: 'user',
        turnId: 'turn-old',
        inputDisposition: 'steering',
        inputDispositionRevision: 1,
      }),
    )

    expect(merged).toMatchObject({
      turnId: 'turn-new',
      inputDisposition: 'promoted',
      inputDispositionRevision: 2,
    })
  })

  it('preserves prev reasoning whenever the server row measured none, independent of prev.role', () => {
    // The role check only governs whether the SERVER's measured seconds may
    // suppress the graft; it does not gate the graft itself on prev being an
    // assistant. Non-assistant rows never carry reasoning in practice, so this
    // branch is unreachable — but the suite locks the contract the code actually
    // has, not the one a reader might assume. (Asymmetry surfaced by this test;
    // behavior left unchanged — see the implementation note.)
    const merged = mergeLiveOnlyFields(
      msg({ role: 'user', reasoning: reasoning(8) }),
      msg({ role: 'user', reasoning: undefined }),
    )
    expect(merged.reasoning?.seconds).toBe(8)
  })
})

describe('reconcileHistoryMessages', () => {
  it('returns the incoming window verbatim when there is no prior state', () => {
    const incoming = [msg({ messageId: 'a' })]
    expect(reconcileHistoryMessages([], incoming)).toBe(incoming)
  })

  it('is server-authoritative: ordering and membership follow the incoming window', () => {
    const prev = [msg({ messageId: 'a' }), msg({ messageId: 'b' }), msg({ messageId: 'c' })]
    const incoming = [msg({ messageId: 'c' }), msg({ messageId: 'a' })] // reordered, b dropped
    expect(reconcileHistoryMessages(prev, incoming).map(m => m.messageId)).toEqual(['c', 'a'])
  })

  it('rides live-only fields along only on a real messageId match', () => {
    const prev = [msg({ messageId: 'm1', reasoning: reasoning(9), routerSettled: true })]
    const out = reconcileHistoryMessages(prev, [msg({ messageId: 'm1', reasoning: undefined })])
    expect(out[0].reasoning?.seconds).toBe(9)
    expect(out[0].routerSettled).toBe(true)
  })

  it('keeps the live approval timeline when the canonical assistant row arrives', () => {
    const approvalTimeline = [{
      type: 'interrupt',
      approvalId: 'approval-1',
    }]
    const interrupts = [{
      type: 'interrupt',
      key: 'stream:interrupt:approval-1',
      interruptKind: 'approval',
      resolution: 'approved',
      busy: false,
      error: '',
    }]
    const prev = [
      msg({ role: 'user', text: 'run it', messageId: 'u1', restoredFromHistory: true }),
      msg({
        role: 'assistant',
        text: 'done',
        timeline: approvalTimeline,
        interrupts,
      } as any),
    ]
    const incoming = [
      msg({ role: 'user', text: 'run it', messageId: 'u1', restoredFromHistory: true }),
      msg({
        role: 'assistant',
        text: 'done',
        messageId: 'a1',
        restoredFromHistory: true,
      }),
    ]

    const out = reconcileHistoryMessages(prev, incoming)

    expect(out[1].timeline).toEqual(approvalTimeline)
    expect((out[1] as any).interrupts).toEqual(interrupts)
  })

  it('takes server rows verbatim when they carry no messageId', () => {
    const prev = [msg({ messageId: 'm1', reasoning: reasoning(9) })]
    const out = reconcileHistoryMessages(prev, [msg({ messageId: undefined, reasoning: undefined })])
    expect(out[0].reasoning).toBeUndefined()
  })
})

describe('reconcileHistoryWindow', () => {
  it('preserves a live turn id when durable user ownership uniquely matches old history', () => {
    const previous = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        turnId: 'live-turn-1',
      }),
      msg({
        role: 'assistant',
        text: 'done',
        turnId: 'live-turn-1',
      }),
    ]
    const latestWindow = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'done',
        messageId: 'assistant-1',
        restoredFromHistory: true,
      }),
    ]

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged.map(message => message.turnId)).toEqual([
      'live-turn-1',
      'live-turn-1',
    ])
  })

  it('keeps an explicit server turn id authoritative over the live identity', () => {
    const previous = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        turnId: 'live-turn-1',
      }),
      msg({
        role: 'assistant',
        text: 'done',
        turnId: 'live-turn-1',
      }),
    ]
    const latestWindow = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        turnId: 'server-turn-1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'done',
        messageId: 'assistant-1',
        turnId: 'server-turn-1',
        restoredFromHistory: true,
      }),
    ]

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged.map(message => message.turnId)).toEqual([
      'server-turn-1',
      'server-turn-1',
    ])
  })

  it.each([
    {
      name: 'different durable user ownership',
      previous: [
        msg({
          role: 'user',
          text: 'first turn',
          messageId: 'user-1',
          turnId: 'live-turn-1',
        }),
        msg({ role: 'assistant', text: 'done', turnId: 'live-turn-1' }),
      ],
      latestWindow: [
        msg({
          role: 'user',
          text: 'different turn',
          messageId: 'user-2',
          restoredFromHistory: true,
        }),
        msg({
          role: 'assistant',
          text: 'done',
          messageId: 'assistant-2',
          restoredFromHistory: true,
        }),
      ],
    },
    {
      name: 'ambiguous optimistic assistants',
      previous: [
        msg({
          role: 'user',
          text: 'build it',
          messageId: 'user-1',
          turnId: 'live-turn-1',
        }),
        msg({ role: 'assistant', text: 'first', turnId: 'live-turn-1' }),
        msg({ role: 'assistant', text: 'second', turnId: 'live-turn-1' }),
      ],
      latestWindow: [
        msg({
          role: 'user',
          text: 'build it',
          messageId: 'user-1',
          restoredFromHistory: true,
        }),
        msg({
          role: 'assistant',
          text: 'done',
          messageId: 'assistant-1',
          restoredFromHistory: true,
        }),
      ],
    },
  ])('does not infer an assistant turn id from $name', ({ previous, latestWindow }) => {
    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged[merged.length - 1]?.turnId).toBeUndefined()
  })

  it('keeps optimistic turn identity and assistant activity on the first authoritative refresh', () => {
    const statusHistory = [
      { action: 'inspect', label: 'Inspecting', at: 1_000 },
      { action: 'write', label: 'Writing', at: 2_000 },
    ]
    const previous = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        clientId: 'local-user-1',
      }),
      msg({
        role: 'assistant',
        text: 'local answer',
        statusHistory,
        interrupted: true,
      }),
    ]
    const latestWindow = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'user-1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'server answer',
        messageId: 'assistant-1',
        restoredFromHistory: true,
      }),
    ]

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged).toHaveLength(2)
    expect(merged[0]).toMatchObject({
      messageId: 'user-1',
      clientId: 'local-user-1',
      restoredFromHistory: true,
    })
    expect(merged[1]).toMatchObject({
      messageId: 'assistant-1',
      text: 'server answer',
      statusHistory,
      interrupted: true,
      restoredFromHistory: true,
    })
  })

  it('keeps optimistic assistant activity when an older canonical turn overlaps', () => {
    const statusHistory = [
      { action: 'tool:read', label: 'Reading a file', at: 3_000 },
    ]
    const previous = [
      msg({
        role: 'user',
        text: 'older question',
        messageId: 'user-old',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'older answer',
        messageId: 'assistant-old',
        restoredFromHistory: true,
      }),
      msg({
        role: 'user',
        text: 'new question',
        messageId: 'user-new',
        clientId: 'local-user-new',
      }),
      msg({
        role: 'assistant',
        text: 'local new answer',
        statusHistory,
      }),
    ]
    const latestWindow = [
      msg({
        role: 'user',
        text: 'older question',
        messageId: 'user-old',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'older answer',
        messageId: 'assistant-old',
        restoredFromHistory: true,
      }),
      msg({
        role: 'user',
        text: 'new question',
        messageId: 'user-new',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'server new answer',
        messageId: 'assistant-new',
        restoredFromHistory: true,
      }),
    ]

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged[2].clientId).toBe('local-user-new')
    expect(merged[3].statusHistory).toEqual(statusHistory)
  })

  it('does not graft optimistic assistant state across different user message ids', () => {
    const previous = [
      msg({ role: 'user', text: 'first turn', messageId: 'user-1' }),
      msg({
        role: 'assistant',
        text: 'local answer',
        statusHistory: [{ action: 'write', label: 'Writing', at: 1_000 }],
        interrupted: true,
      }),
    ]
    const latestWindow = [
      msg({
        role: 'user',
        text: 'different turn',
        messageId: 'user-2',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'server answer',
        messageId: 'assistant-2',
        restoredFromHistory: true,
      }),
    ]

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged[1].statusHistory).toBeUndefined()
    expect(merged[1].interrupted).toBeUndefined()
  })

  it('keeps canonical pages older than the refreshed server window', () => {
    const previous = Array.from({ length: 250 }, (_, index) => msg({
      messageId: `m-${index}`,
      text: `previous ${index}`,
      restoredFromHistory: true,
    }))
    const latestWindow = Array.from({ length: 200 }, (_, index) => msg({
      messageId: `m-${index + 50}`,
      text: `server ${index + 50}`,
      restoredFromHistory: true,
    }))

    const merged = reconcileHistoryWindow(previous, latestWindow)

    expect(merged).toHaveLength(250)
    expect(merged[0].messageId).toBe('m-0')
    expect(merged[49].messageId).toBe('m-49')
    expect(merged[50].text).toBe('server 50')
    expect(merged[249].messageId).toBe('m-249')
  })

  it('does not concatenate canonical rows when the refreshed window has no overlap', () => {
    const previous = [
      msg({ messageId: 'old', restoredFromHistory: true }),
      msg({ role: 'user', text: 'optimistic', restoredFromHistory: false }),
    ]
    const incoming = [msg({ messageId: 'new', restoredFromHistory: true })]

    expect(reconcileHistoryWindow(previous, incoming).map(message => message.messageId)).toEqual(['new'])
  })
})

describe('reconcileRunningHistoryMessages', () => {
  it('replaces a canonical live assistant with its durable row by turn identity', () => {
    const previous = [
      msg({
        role: 'user',
        text: 'Run the Goal',
        messageId: 'goal-user',
        turnId: 'goal-root-turn',
      }),
      msg({
        role: 'assistant',
        text: 'Initial Goal reply',
        ts: 'live-initial-time',
        turnId: 'goal-root-turn',
      }),
      msg({
        role: 'assistant',
        text: 'Canonical continuation body',
        ts: 'live-time',
        turnId: 'goal-continuation-turn',
        turnInputMode: 'system_event',
        turnRunKind: 'goal',
      }),
    ]
    const incoming = [
      msg({
        role: 'user',
        text: 'Run the Goal',
        messageId: 'goal-user',
        turnId: 'goal-root-turn',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Initial Goal reply',
        ts: 'server-initial-time',
        messageId: 'goal-initial-answer',
        turnId: 'goal-root-turn',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Canonical continuation body',
        ts: 'server-time',
        messageId: 'goal-answer',
        turnId: 'goal-continuation-turn',
        restoredFromHistory: true,
      }),
    ]

    const out = reconcileRunningHistoryMessages(previous, incoming)

    expect(out).toHaveLength(3)
    expect(out[2]).toMatchObject({
      messageId: 'goal-answer',
      text: 'Canonical continuation body',
      turnId: 'goal-continuation-turn',
      turnInputMode: 'system_event',
      turnRunKind: 'goal',
      restoredFromHistory: true,
    })
  })

  it('keeps identical assistant text from distinct turns as distinct rows', () => {
    const previous = [
      msg({ role: 'user', text: 'Run the Goal', messageId: 'goal-user' }),
      msg({ role: 'assistant', text: 'Still working', ts: 'live-1', turnId: 'turn-1' }),
      msg({ role: 'assistant', text: 'Still working', ts: 'live-2', turnId: 'turn-2' }),
    ]
    const incoming = [
      msg({
        role: 'user',
        text: 'Run the Goal',
        messageId: 'goal-user',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Still working',
        messageId: 'answer-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Still working',
        messageId: 'answer-2',
        turnId: 'turn-2',
        restoredFromHistory: true,
      }),
    ]

    const out = reconcileRunningHistoryMessages(previous, incoming)

    expect(out.map(message => message.messageId)).toEqual([
      'goal-user',
      'answer-1',
      'answer-2',
    ])
    expect(out.filter(message => message.text === 'Still working')).toHaveLength(2)
  })

  it('preserves the live tail after the last user when a running history snapshot is colder', () => {
    const prev = [
      msg({ role: 'user', text: 'build it', messageId: 'u1' }),
      msg({
        role: 'router',
        text: '',
        routerDecision: { source: 'llm_ensemble', model: 'z-ai/glm-5.2', tier: 'c1' },
        ensemble: {
          profile: 'llm_ensemble',
          modelCount: 1,
          totalCandidates: 1,
          requestCount: 1,
          fallbackUsed: false,
          fallbackReason: '',
          costUsd: 0,
          savedUsd: 0,
          savedPct: 0,
          models: [{
            role: 'proposer_1',
            label: 'proposer_1',
            provider: 'openrouter',
            model: 'z-ai/glm-5.2',
            modelShort: 'glm-5.2',
            input: 0,
            output: 0,
            costUsd: 0,
            status: 'running',
          }],
        },
      }),
      msg({ role: 'assistant', text: 'Writing a file', tool_calls: [{ id: 't1', name: 'write_file' }] as any }),
    ]
    const incoming = [msg({ role: 'user', text: 'build it', messageId: 'u1', restoredFromHistory: true })]

    const out = reconcileRunningHistoryMessages(prev, incoming)

    expect(out).toHaveLength(3)
    expect(out[0].messageId).toBe('u1')
    expect(out[1].role).toBe('router')
    expect(out[1].ensemble?.models[0]?.model).toBe('z-ai/glm-5.2')
    expect(out[2].role).toBe('assistant')
    expect(out[2].tool_calls?.[0]?.name).toBe('write_file')
  })

  it('does not duplicate live rows that the server snapshot already contains by message id', () => {
    const prev = [
      msg({ role: 'user', text: 'build it', messageId: 'u1' }),
      msg({ role: 'assistant', text: 'partial', messageId: 'a1', routerSettled: true }),
    ]
    const incoming = [
      msg({ role: 'user', text: 'build it', messageId: 'u1', restoredFromHistory: true }),
      msg({ role: 'assistant', text: 'partial', messageId: 'a1', restoredFromHistory: true }),
    ]

    const out = reconcileRunningHistoryMessages(prev, incoming)

    expect(out.map(message => message.messageId)).toEqual(['u1', 'a1'])
    expect(out[1].routerSettled).toBe(true)
  })

  it('retains a mutation-confirmed user row across an older non-empty history response', () => {
    const confirmedGoal = msg({
      role: 'user',
      text: 'Ship the Goal',
      messageId: 'goal-user-1',
      clientId: 'goal-client-1',
      turnId: 'goal-task-1',
    })
    const prev = [
      msg({
        role: 'user',
        text: 'Earlier request',
        messageId: 'u1',
        restoredFromHistory: true,
      }),
      confirmedGoal,
    ]
    const staleIncoming = [
      msg({
        role: 'user',
        text: 'Earlier request',
        messageId: 'u1',
        restoredFromHistory: true,
      }),
    ]

    const preserved = reconcileRunningHistoryMessages(prev, staleIncoming)

    expect(preserved.map(message => message.messageId)).toEqual(['u1', 'goal-user-1'])
    expect(preserved[1]).toBe(confirmedGoal)

    const canonical = reconcileRunningHistoryMessages(preserved, [
      ...staleIncoming,
      msg({
        role: 'user',
        text: 'Ship the Goal',
        messageId: 'goal-user-1',
        turnId: 'goal-task-1',
        restoredFromHistory: true,
      }),
    ])

    expect(canonical.map(message => message.messageId)).toEqual(['u1', 'goal-user-1'])
    expect(canonical[1]).toMatchObject({
      clientId: 'goal-client-1',
      restoredFromHistory: true,
      turnId: 'goal-task-1',
    })
  })

  it('retains a mutation-confirmed user anchor and its live tail across stale history', () => {
    const prev = [
      msg({
        role: 'user',
        text: 'Earlier request',
        messageId: 'u1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Earlier response',
        messageId: 'a1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'user',
        text: 'Ship the Goal',
        messageId: 'goal-user-1',
        clientId: 'goal-client-1',
        turnId: 'goal-task-1',
      }),
      msg({ role: 'router', text: '', turnId: 'goal-task-1' }),
      msg({ role: 'assistant', text: 'Working on it', turnId: 'goal-task-1' }),
    ]
    const staleIncoming = [
      msg({
        role: 'user',
        text: 'Earlier request',
        messageId: 'u1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'Earlier response',
        messageId: 'a1',
        restoredFromHistory: true,
      }),
    ]

    const preserved = reconcileRunningHistoryMessages(prev, staleIncoming)

    expect(preserved.map(message => [message.role, message.messageId, message.text])).toEqual([
      ['user', 'u1', 'Earlier request'],
      ['assistant', 'a1', 'Earlier response'],
      ['user', 'goal-user-1', 'Ship the Goal'],
      ['router', undefined, ''],
      ['assistant', undefined, 'Working on it'],
    ])

    const canonical = reconcileRunningHistoryMessages(preserved, [
      ...staleIncoming,
      msg({
        role: 'user',
        text: 'Ship the Goal',
        messageId: 'goal-user-1',
        turnId: 'goal-task-1',
        restoredFromHistory: true,
      }),
    ])

    expect(canonical.map(message => [message.role, message.messageId])).toEqual([
      ['user', 'u1'],
      ['assistant', 'a1'],
      ['user', 'goal-user-1'],
      ['router', undefined],
      ['assistant', undefined],
    ])
    expect(canonical[2]).toMatchObject({
      clientId: 'goal-client-1',
      restoredFromHistory: true,
    })
  })

  it('replaces the unique optimistic assistant owned by the same durable user turn', () => {
    const statusHistory = [{ action: 'answer', label: 'Answering', at: 2_000 }]
    const prev = [
      msg({
        role: 'user',
        text: 'build it',
        ts: 'live-user',
        messageId: 'u1',
        clientId: 'local-u1',
      }),
      msg({
        role: 'assistant',
        text: 'verified answer',
        ts: 'live-assistant',
        statusHistory,
      }),
    ]
    const incoming = [
      msg({
        role: 'user',
        text: 'build it',
        ts: 'server-user',
        messageId: 'u1',
        restoredFromHistory: true,
      }),
      msg({
        role: 'assistant',
        text: 'verified answer',
        ts: 'server-assistant',
        messageId: 'a1',
        restoredFromHistory: true,
      }),
    ]

    const out = reconcileRunningHistoryMessages(prev, incoming)

    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({
      messageId: 'u1',
      clientId: 'local-u1',
      restoredFromHistory: true,
    })
    expect(out[1]).toMatchObject({
      messageId: 'a1',
      text: 'verified answer',
      statusHistory,
      restoredFromHistory: true,
    })
  })

  it('preserves the full live tail when the last user row is a same-turn steer', () => {
    const prev = [
      msg({ role: 'user', text: 'build it', messageId: 'u1', turnId: 'turn-1' }),
      msg({ role: 'router', text: '', turnId: 'turn-1' }),
      msg({ role: 'assistant', text: 'first segment', turnId: 'turn-1' }),
      msg({
        role: 'user',
        text: 'also update tests',
        clientId: 'steer-local',
        turnId: 'turn-1',
        inputDisposition: 'steering',
        inputDispositionRevision: 1,
      }),
    ]
    const incoming = [
      msg({
        role: 'user',
        text: 'build it',
        messageId: 'u1',
        turnId: 'turn-1',
        restoredFromHistory: true,
      }),
    ]

    const out = reconcileRunningHistoryMessages(prev, incoming)

    expect(out.map(message => [message.role, message.text])).toEqual([
      ['user', 'build it'],
      ['router', ''],
      ['assistant', 'first segment'],
      ['user', 'also update tests'],
    ])
    expect(out[3]).toMatchObject({
      clientId: 'steer-local',
      inputDisposition: 'steering',
    })
  })
})
