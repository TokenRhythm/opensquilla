import { describe, expect, it } from 'vitest'
import { ref } from 'vue'

import { useChatRenderedMessages } from './useChatRenderedMessages'
import type { ChatMessage, ChatRouterTierConfig } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { ChatPart, InterruptViewState } from '@/types/parts'
import type { TimeTranslator } from '@/utils/messageTime'

function renderedMessagesForRouterVisualMode(
  visualMode: 'real_candidates' | 'legacy_grid',
  modelRoutingMode: ModelRoutingMode = 'squilla_router',
) {
  const configs: Record<string, ChatRouterTierConfig> = {
    fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
    balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
    strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
  }
  const options = {
    messages: ref<ChatMessage[]>([]),
    sessionKey: ref('router-visual-test'),
    routerSlots: ref(['fast', 'balanced', 'strong']),
    routerModels: ref({}),
    routerTierConfigs: ref(configs),
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref(visualMode),
    renderMarkdown: (text: string) => text,
    stripGeneratedArtifactMarkers: (text: string) => text,
    stripTimePrefix: (text: string) => text,
    isSubagentCompletionMessage: () => false,
    modelRoutingMode: ref(modelRoutingMode),
  }
  return useChatRenderedMessages(options)
}

function renderedMessagesFor(
  messages: ChatMessage[],
  interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map()),
  routerVisualEffectsEnabled = false,
  timeTranslator?: TimeTranslator,
) {
  return useChatRenderedMessages({
    messages: ref<ChatMessage[]>(messages),
    interruptState,
    sessionKey: ref('agent:main:webchat:test'),
    routerSlots: ref([]),
    routerModels: ref({}),
    routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(routerVisualEffectsEnabled),
    routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
    timeTranslator,
  })
}

describe('useChatRenderedMessages annotation-only user turns', () => {
  it('keeps the live optimistic row when prompt annotations are the only visible payload', () => {
    const api = renderedMessagesFor([{
      role: 'user',
      text: '',
      ts: 1,
      clientId: 'client-annotation-1',
      promptAnnotations: [{
        annotationId: 'annotation-1',
        documentId: 'document-1',
        documentName: 'page.html',
        revisionId: 'revision-1',
        generation: 1,
        anchorId: 'anchor-1',
        body: 'Make the primary action red.',
        tagName: 'button',
        locator: { start_offset: 7 },
        quote: '<button>',
        sourceExcerpt: null,
        sentOrder: 0,
      }],
    }])

    expect(api.renderedMessages.value).toHaveLength(1)
    expect(api.renderedMessages.value[0]).toMatchObject({
      displayRole: 'user',
      text: '',
      clientId: 'client-annotation-1',
      promptAnnotations: [{
        annotationId: 'annotation-1',
        body: 'Make the primary action red.',
      }],
    })
  })

  it('still suppresses internal empty control turns without visible payload', () => {
    const api = renderedMessagesFor([{
      role: 'user',
      text: '',
      ts: 1,
      clientId: 'client-control-1',
    }])

    expect(api.renderedMessages.value).toEqual([])
  })
})

describe('useChatRenderedMessages maintenance events', () => {
  it('localizes projected relative times for shared chat consumers', () => {
    const api = renderedMessagesFor(
      [{ role: 'user', text: 'hello', ts: Date.now() - 5 * 60_000 }],
      undefined,
      false,
      (key, named) => `localized:${key}:${named?.n ?? ''}`,
    )

    expect(api.renderedMessages.value[0]?.timeStr).toBe(
      'localized:chat.time.minutesAgo:5',
    )
  })

  it('preserves the dedicated compaction payload for ChatMessageList', () => {
    const api = renderedMessagesFor([{
      role: 'maintenance',
      text: '',
      ts: 2_000,
      messageId: 'maintenance:context-compaction:summary:7',
      restoredFromHistory: true,
      maintenance: {
        kind: 'context_compaction',
        compactionId: 'cmp-7',
        source: 'manual',
        state: 'completed',
        durability: 'durable',
      },
    }])

    expect(api.renderedMessages.value[0]).toMatchObject({
      displayRole: 'maintenance',
      messageId: 'maintenance:context-compaction:summary:7',
      maintenance: {
        kind: 'context_compaction',
        compactionId: 'cmp-7',
      },
    })
  })
})

describe('useChatRenderedMessages plan revisions', () => {
  it('renders a typed plan part once and derives currentness from the active pointer', () => {
    const currentPlanRevisionId = ref('revision-2')
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([{
        role: 'assistant',
        text: 'Legacy Markdown fallback',
        ts: 1,
        planRevisions: [{
          revisionId: 'revision-2',
          planId: 'plan-1',
          title: 'Ship plan mode',
          markdown: 'A complete plan.',
          steps: [{ stepId: 'inspect', title: 'Inspect' }],
          current: false,
        }],
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'submit-1',
            name: 'submit_plan',
            input: { title: 'Ship plan mode' },
          },
          {
            type: 'tool_result',
            tool_use_id: 'submit-1',
            name: 'submit_plan',
            result: '{"status":"plan_submitted"}',
            is_error: false,
          },
          {
            type: 'tool_use',
            tool_use_id: 'list-dir-1',
            name: 'list_dir',
            input: { path: '.' },
          },
          {
            type: 'tool_result',
            tool_use_id: 'list-dir-1',
            name: 'list_dir',
            result: 'README.md',
            is_error: false,
          },
          {
            type: 'plan',
            snapshot: { revisionId: 'revision-2' },
          },
        ],
      }]),
      sessionKey: ref('agent:main:webchat:test'),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false),
      routerVisualMode: ref('real_candidates'),
      currentPlanRevisionId,
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const message = api.renderedMessages.value[0]
    expect(message.text).toBe('')
    expect(message.timelineItems).toEqual([
      expect.objectContaining({
        type: 'tool-group',
        group: expect.objectContaining({
          calls: [expect.objectContaining({ name: 'list_dir', toolId: 'list-dir-1', status: 'success' })],
        }),
      }),
    ])
    expect(message.toolCalls).toEqual([
      expect.objectContaining({ name: 'list_dir', toolId: 'list-dir-1', status: 'success' }),
    ])
    expect(message.planRevisions?.[0]?.current).toBe(true)
    expect(message.parts).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'plan',
        plan: expect.objectContaining({ revisionId: 'revision-2', current: true }),
      }),
      expect.objectContaining({ type: 'tool', toolName: 'list_dir' }),
    ]))
    expect(message.parts?.filter(part => part.type === 'plan')).toHaveLength(1)
    expect(message.parts?.some(part => part.type === 'text')).toBe(false)

    currentPlanRevisionId.value = 'revision-3'
    expect(api.renderedMessages.value[0]?.planRevisions?.[0]?.current).toBe(false)
  })
})

describe('useChatRenderedMessages internal control turns', () => {
  it('hides a blank user projection while preserving its turn identity', () => {
    const api = renderedMessagesFor([
      { role: 'user', text: 'Create a plan', ts: 1, messageId: 'user-plan' },
      { role: 'assistant', text: 'Plan ready', ts: 2, messageId: 'assistant-plan' },
      { role: 'user', text: '', ts: 3, messageId: 'internal-implementation' },
      { role: 'assistant', text: 'Implementing now', ts: 4, messageId: 'assistant-run' },
    ])

    expect(api.renderedMessages.value.map(message => message.text)).toEqual([
      'Create a plan',
      'Plan ready',
      'Implementing now',
    ])
    expect(api.renderedMessages.value.filter(message => message.displayRole === 'user')).toHaveLength(1)
    expect(api.renderedMessages.value[1]?.turnKey).toBe('turn:user-plan')
    expect(api.renderedMessages.value[2]?.turnKey).toBe('turn:internal-implementation')
  })

  it('keeps an attachment-only user turn visible', () => {
    const api = renderedMessagesFor([{
      role: 'user',
      text: '',
      ts: 1,
      messageId: 'attachment-turn',
      attachments: [{
        kind: 'file',
        displayId: 'attachment-1',
        renderKey: 'attachment-1',
        name: 'requirements.md',
        mime: 'text/markdown',
      }],
    }])

    expect(api.renderedMessages.value).toHaveLength(1)
    expect(api.renderedMessages.value[0]?.hasAttachments).toBe(true)
  })

  it('keeps subagent completion control rows out while retaining the parent creation route', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'Create a child chat', ts: 1, turnId: 'parent-turn' },
      {
        role: 'router',
        text: '',
        ts: 2,
        turnId: 'parent-turn',
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c0',
          model: 'deepseek-v4-flash',
          source: 'heuristic',
        },
      },
      {
        role: 'system',
        text: '{"type":"subagent_completion","child_session_key":"agent:main:subagent:child1"}',
        ts: 3,
        provenanceKind: 'internal_system',
        provenanceSourceTool: 'subagent_completion',
        provenanceSourceSessionKey: 'agent:main:subagent:child1',
      },
      {
        role: 'assistant',
        text: '',
        ts: 4,
        turnId: 'parent-turn',
        usage: {
          routed_tier: 'c0',
          routed_model: 'deepseek-v4-flash',
          routing_source: 'heuristic',
        },
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'spawn-1',
            name: 'sessions_spawn',
            input: { task: 'Do the work' },
          },
          {
            type: 'tool_result',
            tool_use_id: 'spawn-1',
            name: 'sessions_spawn',
            result: '{"session_key":"agent:main:subagent:child1"}',
            is_error: false,
          },
        ],
      },
      { role: 'assistant', text: 'Child completed', ts: 5, turnId: 'parent-resume' },
    ]
    const api = useChatRenderedMessages({
      messages: ref(messages),
      sessionKey: ref('agent:main:webchat:parent'),
      routerSlots: ref(['c0', 'c1']),
      routerModels: ref({ c0: 'deepseek-v4-flash', c1: 'deepseek-v4-pro' }),
      routerTierConfigs: ref({
        c0: { model: 'deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek-v4-pro', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: (role, text, message) => (
        role === 'system'
        && (
          message?.provenanceSourceTool === 'subagent_completion'
          || text.includes('"type":"subagent_completion"')
        )
      ),
    })

    expect(api.renderedMessages.value.find(message => message.isRouterStrip)?.gridCells)
      .toEqual(expect.arrayContaining([expect.objectContaining({ model: 'deepseek-v4-flash' })]))
    expect(api.renderedMessages.value.some(message => message.displayRole === 'subagent')).toBe(false)
    expect(api.renderedMessages.value.some(message => message.text.includes('subagent_completion'))).toBe(false)
    expect(api.renderedMessages.value.find(message => (
      message.sourceIndex === 3 && message.displayRole === 'assistant'
    ))?.toolCalls)
      .toEqual(expect.arrayContaining([expect.objectContaining({ name: 'sessions_spawn' })]))
    expect(api.renderedMessages.value.find(message => (
      message.sourceIndex === 3 && message.displayRole === 'assistant'
    ))?.createdSessionLinks)
      .toEqual([])
    const parentReply = api.renderedMessages.value[api.renderedMessages.value.length - 1]
    expect(parentReply?.text).toBe('Child completed')
    expect(parentReply?.createdSessionLinks).toEqual([{
      callId: 'spawn-1',
      sessionKey: 'agent:main:subagent:child1',
    }])
  })

  it('shows the actual inherited model route on the child session', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'Do the work', ts: 1 },
        {
          role: 'assistant',
          text: 'Done',
          ts: 2,
          restoredFromHistory: true,
          usage: {
            model: 'deepseek-v4-pro',
            routed_model: 'deepseek-v4-pro',
            routing_source: 'none',
            routing_applied: true,
          },
        },
      ]),
      sessionKey: ref('agent:main:subagent:child1'),
      routerSlots: ref(['c0', 'c1']),
      routerModels: ref({ c0: 'deepseek-v4-flash', c1: 'deepseek-v4-pro' }),
      routerTierConfigs: ref({
        c0: { model: 'deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek-v4-pro', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerSource).toBe('session_model')
    expect(strip?.gridCells?.[strip.winnerIdx ?? -1]?.model).toBe('deepseek-v4-pro')
    expect(api.renderedMessages.value[api.renderedMessages.value.length - 1]?.text).toBe('Done')
  })

  it('keeps the card at its source when completion identity is missing', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        {
          role: 'assistant',
          text: '',
          ts: 1,
          tool_calls: [{
            type: 'tool_result',
            tool_use_id: 'spawn-1',
            name: 'sessions_spawn',
            result: '{"session_key":"agent:main:subagent:child1"}',
            is_error: false,
          }],
        },
        {
          role: 'system',
          text: '{"type":"subagent_completion"}',
          ts: 2,
          provenanceSourceTool: 'subagent_completion',
        },
        { role: 'assistant', text: 'Unrelated parent reply', ts: 3 },
      ]),
      sessionKey: ref('agent:main:webchat:parent'),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: (role, text, message) => (
        role === 'system'
        && (message?.provenanceSourceTool === 'subagent_completion'
          || text.includes('"type":"subagent_completion"'))
      ),
    })

    expect(api.renderedMessages.value.find(message => message.sourceIndex === 0)?.createdSessionLinks)
      .toEqual([{ callId: 'spawn-1', sessionKey: 'agent:main:subagent:child1' }])
    expect(api.renderedMessages.value.find(message => message.sourceIndex === 2)?.createdSessionLinks)
      .toEqual([])
  })

  it('does not rehome a completed card across the next visible user turn', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        {
          role: 'assistant',
          text: '',
          ts: 1,
          tool_calls: [{
            type: 'tool_result',
            tool_use_id: 'spawn-1',
            name: 'sessions_spawn',
            result: '{"session_key":"agent:main:subagent:child1"}',
            is_error: false,
          }],
        },
        {
          role: 'system',
          text: '{"type":"subagent_completion","child_session_key":"agent:main:subagent:child1"}',
          ts: 2,
          provenanceSourceTool: 'subagent_completion',
          provenanceSourceSessionKey: 'agent:main:subagent:child1',
        },
        { role: 'user', text: 'A separate question', ts: 3 },
        { role: 'assistant', text: 'A separate answer', ts: 4 },
      ]),
      sessionKey: ref('agent:main:webchat:parent'),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: (role, text, message) => (
        role === 'system'
        && (message?.provenanceSourceTool === 'subagent_completion'
          || text.includes('"type":"subagent_completion"'))
      ),
    })

    expect(api.renderedMessages.value.find(message => message.sourceIndex === 0)?.createdSessionLinks)
      .toEqual([{ callId: 'spawn-1', sessionKey: 'agent:main:subagent:child1' }])
    expect(api.renderedMessages.value.find(message => message.sourceIndex === 3)?.createdSessionLinks)
      .toEqual([])
  })

  it.each([
    ['agent:main:subagent:child1', 'none'],
    ['subagent:agent:main:webchat:parent', 'session_model'],
  ])('shows a fixed route for %s with a non-slot model', (childSessionKey, routingSource) => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([{
        role: 'assistant',
        text: 'Done',
        ts: 1,
        usage: {
          routed_model: 'provider/custom-child-model',
          routing_source: routingSource,
        },
      }]),
      sessionKey: ref(childSessionKey),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerSource).toBe('session_model')
    expect(strip?.gridCells).toHaveLength(1)
    expect(strip?.gridCells?.[strip.winnerIdx ?? -1]?.model)
      .toBe('provider/custom-child-model')
  })

  it('does not infer a fixed-model route for a regular parent session', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([{
        role: 'assistant',
        text: 'Done',
        ts: 1,
        usage: {
          routed_model: 'deepseek-v4-pro',
          routing_source: 'none',
        },
      }]),
      sessionKey: ref('agent:main:webchat:parent'),
      routerSlots: ref(['c0', 'c1']),
      routerModels: ref({ c0: 'deepseek-v4-flash', c1: 'deepseek-v4-pro' }),
      routerTierConfigs: ref({
        c0: { model: 'deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek-v4-pro', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    expect(api.renderedMessages.value.some(message => message.isRouterStrip)).toBe(false)
  })

  it('keeps parent routing visible when session creation fails or returns no child key', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'Create a child chat', ts: 1 },
        {
          role: 'assistant',
          text: '',
          ts: 2,
          usage: {
            routed_tier: 'c0',
            routed_model: 'deepseek-v4-flash',
            routing_source: 'heuristic',
          },
          tool_calls: [{
            type: 'tool_result',
            tool_use_id: 'spawn-failed',
            name: 'sessions_spawn',
            result: '{"status":"error"}',
            is_error: true,
          }],
        },
      ]),
      sessionKey: ref('agent:main:webchat:parent'),
      routerSlots: ref(['c0', 'c1']),
      routerModels: ref({ c0: 'deepseek-v4-flash', c1: 'deepseek-v4-pro' }),
      routerTierConfigs: ref({
        c0: { model: 'deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek-v4-pro', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    expect(api.renderedMessages.value.find(message => message.isRouterStrip)?.routerSource)
      .toBe('heuristic')
  })
})

describe('useChatRenderedMessages silent sentinel compatibility', () => {
  it('preserves presentation from explicit and legacy persisted timelines', () => {
    const explicit = renderedMessagesFor([{
      role: 'assistant',
      text: 'Working note.Final answer.',
      ts: 1,
      timeline: [
        { type: 'text', raw: 'Working note.', presentation: 'intermediate' },
        { type: 'text', raw: 'Final answer.', presentation: 'answer' },
      ],
    }]).renderedMessages.value[0]!
    const legacy = renderedMessagesFor([{
      role: 'assistant',
      text: 'Working note.Final answer.',
      ts: 1,
      tool_calls: [
        { type: 'text', text: 'Working note.', presentation: 'intermediate' },
        { type: 'text', text: 'Final answer.', presentation: 'answer' },
      ],
    }]).renderedMessages.value[0]!

    expect(explicit.timelineItems?.map(item => (
      item.type === 'text' ? item.presentation : item.type
    ))).toEqual(['intermediate', 'answer'])
    expect(legacy.timelineItems?.map(item => (
      item.type === 'text' ? item.presentation : item.type
    ))).toEqual(['intermediate', 'answer'])
  })

  it('projects mixed legacy text and explicit timeline markers without mutating history', () => {
    const source: ChatMessage = {
      role: 'assistant',
      text: 'NO_REPLY\nVisible answer.\nHEARTBEAT_OK',
      ts: 1,
      turnRunKind: 'goal',
      timeline: [
        { type: 'text', raw: 'NO_REPLY\nFirst' },
        { type: 'text', raw: 'NO_REPLY' },
        { type: 'text', raw: 'Last\nHEARTBEAT_OK' },
      ],
    }
    const api = renderedMessagesFor([source])
    const message = api.renderedMessages.value[0]!

    expect(message.text).toBe('Visible answer.')
    expect(message.turnRunKind).toBe('goal')
    expect(message.timelineItems?.map(item => item.type === 'text' ? item.rawText : item.type))
      .toEqual(['First', 'NO_REPLY', 'Last'])
    expect(message.parts?.filter(part => part.type === 'text').map(part => part.rawText))
      .toEqual(['First', 'NO_REPLY', 'Last'])
    expect(source.text).toBe('NO_REPLY\nVisible answer.\nHEARTBEAT_OK')
    expect(source.timeline?.[0]?.raw).toBe('NO_REPLY\nFirst')
  })

  it('projects persisted text segments while preserving their tool group', () => {
    const source: ChatMessage = {
      role: 'assistant',
      text: 'HEARTBEAT_OK\nDone.',
      ts: 1,
      turnInputMode: 'system_event',
      tool_calls: [
        { type: 'text', text: 'HEARTBEAT_OK' },
        { type: 'tool_use', tool_use_id: 'tool-1', name: 'web_search', input: '{}' },
        { type: 'tool_result', tool_use_id: 'tool-1', name: 'web_search', result: 'found' },
        { type: 'text', text: 'Done.' },
      ],
    }
    const api = renderedMessagesFor([source])
    const message = api.renderedMessages.value[0]!

    expect(message.text).toBe('Done.')
    expect(message.timelineItems?.map(item => item.type === 'text' ? item.rawText : item.type))
      .toEqual(['tool-group', 'Done.'])
    expect(message.parts?.some(part => part.type === 'tool')).toBe(true)
    expect(source.tool_calls?.[0]?.text).toBe('HEARTBEAT_OK')
  })

  it('projects narration and parallel legacy calls into one ordered activity timeline', () => {
    const source: ChatMessage = {
      role: 'assistant',
      text: '',
      ts: 1,
      tool_calls: [
        { type: 'text', text: 'Inspect the source.' },
        { type: 'tool_use', tool_use_id: 'call-read', name: 'read_file', input: {} },
        { type: 'text', text: 'Compare the directory.' },
        { type: 'tool_use', tool_use_id: 'call-list', name: 'list_dir', input: {} },
        { type: 'tool_result', tool_use_id: 'call-read', name: 'read_file', result: 'source payload' },
        { type: 'tool_result', tool_use_id: 'call-list', name: 'list_dir', result: 'directory payload' },
      ],
    }

    const message = renderedMessagesFor([source]).renderedMessages.value[0]!

    expect(message.timelineItems?.map(item => item.type === 'text' ? item.rawText : item.type))
      .toEqual(['Inspect the source.', 'tool-group', 'Compare the directory.', 'tool-group'])
    const calls = message.timelineItems?.flatMap(item =>
      item.type === 'tool-group' ? item.group.calls : [],
    ) ?? []
    expect(calls.map(call => ({ id: call.toolId, result: call.result }))).toEqual([
      { id: 'call-read', result: 'source payload' },
      { id: 'call-list', result: 'directory payload' },
    ])
    expect(message.parts?.map(part => part.type)).toEqual([
      'text',
      'tool',
      'text',
      'tool',
    ])
    expect(source.tool_calls?.[0]?.text).toBe('Inspect the source.')
  })

  it('preserves mixed sentinel-looking text on an ordinary direct-user turn', () => {
    const source: ChatMessage = {
      role: 'assistant',
      text: 'NO_REPLY\nThis is a literal explanation.',
      ts: 1,
      turnInputMode: 'user',
      turnRunKind: 'default',
      timeline: [
        { type: 'text', raw: 'NO_REPLY' },
        { type: 'text', raw: 'This is a literal explanation.' },
      ],
    }

    const message = renderedMessagesFor([source]).renderedMessages.value[0]!

    expect(message.text).toBe('NO_REPLY\nThis is a literal explanation.')
    expect(message.timelineItems?.map(item => item.type === 'text' ? item.rawText : item.type))
      .toEqual(['NO_REPLY', 'This is a literal explanation.'])
    expect(message.turnInputMode).toBe('user')
    expect(message.turnRunKind).toBe('default')
  })

  it('omits an exact legacy sentinel row but keeps rows with durable output', () => {
    const api = renderedMessagesFor([
      { role: 'assistant', text: 'NO_REPLY', ts: 1 },
      {
        role: 'assistant',
        text: 'HEARTBEAT_OK',
        ts: 2,
        artifacts: [{ id: 'artifact-1', name: 'result.txt' }],
      },
    ])

    expect(api.renderedMessages.value).toHaveLength(1)
    expect(api.renderedMessages.value[0]).toMatchObject({
      text: '',
      artifacts: [{ id: 'artifact-1', name: 'result.txt' }],
    })
  })
})

describe('useChatRenderedMessages immutable route history', () => {
  it('keeps a restored steered route card on its physical answer segment', () => {
    const routerUsage = {
      routed_tier: 'c1',
      routed_model: 'provider/initial',
      routing_source: 'squilla_router',
      router_model_call_id: '1.0',
      router_iteration: 1,
      input_tokens: 12,
      output_tokens: 3,
      cost_usd: 0.004,
    }
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'question', ts: 1, turnId: 'turn-steer' },
        {
          role: 'assistant',
          text: 'first segment',
          ts: 2,
          turnId: 'turn-steer',
          clientId: 'history-prefix',
          routerUsage,
          routerModelCallId: '1.0',
          restoredFromHistory: true,
        },
        {
          role: 'user',
          text: 'guide it',
          ts: 3,
          turnId: 'turn-steer',
          inputDisposition: 'applied',
        },
        {
          role: 'assistant',
          text: 'continued segment',
          ts: 4,
          turnId: 'turn-steer',
          messageId: 'assistant-final',
          usage: routerUsage,
          restoredFromHistory: true,
        },
      ]),
      sessionKey: ref('agent:main:webchat:steer-router'),
      routerSlots: ref(['c0', 'c1']),
      routerModels: ref({ c0: 'provider/fast', c1: 'provider/initial' }),
      routerTierConfigs: ref({
        c0: { model: 'provider/fast', supportsImage: false, imageOnly: false },
        c1: { model: 'provider/initial', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const rendered = api.renderedMessages.value
    expect(rendered.map(message => message.displayRole)).toEqual([
      'user',
      'router',
      'assistant',
      'user',
      'assistant',
    ])
    expect(rendered.filter(message => message.isRouterStrip)).toHaveLength(1)
    expect(rendered[1]).toMatchObject({
      routerModelCallId: '1.0',
      routerIteration: 1,
      routerTurnKey: 'router-call:turn-steer:1.0',
    })
    expect(rendered[2]?.meta).toBeUndefined()
    expect(rendered[4]?.meta).toMatchObject({
      input: 12,
      output: 3,
      costUsd: 0.004,
    })
  })

  it('keeps the logical RoutePlan model after a provider fallback leg', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'Solve this', ts: 1, turnId: 'turn-route' },
        {
          role: 'assistant',
          text: 'Solved',
          ts: 2,
          turnId: 'turn-route',
          usage: {
            routed_tier: 'c2',
            routed_model: 'provider/fallback-model',
            routing_source: 'classifier',
            routing_applied: true,
            route_plan: {
              tier: 'c2',
              model: 'provider/original-model',
              source: 'classifier',
              routing_applied: true,
            },
            execution_legs: [
              { kind: 'primary', model: 'provider/original-model' },
              { kind: 'provider_fallback', model: 'provider/fallback-model' },
            ],
          },
        },
      ]),
      sessionKey: ref('agent:main:webchat:test'),
      routerSlots: ref(['c1', 'c2']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        c1: { model: 'provider/fast-model', supportsImage: false, imageOnly: false },
        c2: { model: '', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    const winner = strip?.gridCells?.[strip.winnerIdx ?? -1]
    expect(winner?.model).toBe('provider/original-model')
    expect(strip?.routerSource).toBe('classifier')
  })
})

describe('useChatRenderedMessages router visual mode', () => {
  it('keeps real-candidates mode limited to callable router tiers', () => {
    const api = renderedMessagesForRouterVisualMode('real_candidates')

    const cells = api.routerDecisionCells({
      tier: 'balanced',
      model: 'anthropic/claude-sonnet-4.6',
    })

    expect(cells).toHaveLength(3)
    expect(cells.every(cell => cell.kind === 'real')).toBe(true)
  })

  it('renders legacy-grid mode as a 15-cell visual panel without moving the real winner', () => {
    const api = renderedMessagesForRouterVisualMode('legacy_grid')

    const cells = api.routerDecisionCells({
      tier: 'balanced',
      model: 'anthropic/claude-sonnet-4.6',
    })
    const winnerIdx = api.routerWinnerCellIndex(cells, 'balanced')

    expect(cells).toHaveLength(15)
    expect(cells.some(cell => cell.kind === 'decoy')).toBe(true)
    expect(cells.filter(cell => cell.kind === 'real')).toHaveLength(3)
    expect(cells.map(cell => cell.displayName)).toEqual(
      expect.arrayContaining([
        'gpt-5.5',
        'gemini-3.5-flash',
        'qwen3-coder-plus',
        'grok-4.3',
        'gpt-5.4-mini',
        'kimi-k2.6',
      ]),
    )
    expect(cells.map(cell => cell.displayName)).not.toEqual(
      expect.arrayContaining([
        'gpt-4.1-mini',
        'gpt-4o-mini',
        'o4-mini',
        'deepseek-chat',
        'mistral-medium',
        'grok-code-fast',
        'qwen3-coder',
      ]),
    )
    expect(winnerIdx).toBeGreaterThanOrEqual(0)
    expect(cells[winnerIdx].kind).toBe('real')
    expect(cells[winnerIdx].tiers).toContain('balanced')
  })

  it('keeps a restored single-model turn as the grid while ensemble mode is on', () => {
    // A restored history (squilla_router) turn must render as the normal candidate
    // grid even while the global LLM-ensemble toggle happens to be on — the active
    // mode only tags the live turn, never restored history.
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          restoredFromHistory: true,
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-ensemble-active-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: {
          model: 'anthropic/claude-sonnet-4.6',
          supportsImage: false,
          imageOnly: false,
          ensembleEnabled: true,
        },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('real-candidates')
    expect((strip?.gridCells || []).length).toBeGreaterThan(1)
  })

  it('keeps the accepted ensemble strip stable when the next-turn mode changes', () => {
    const nextTurnMode = ref<ModelRoutingMode>('llm_ensemble')
    const messages = ref<ChatMessage[]>([
      { role: 'user', text: 'hard question', ts: 0 },
      {
        role: 'router',
        text: '',
        ts: 1,
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'balanced',
          model: 'anthropic/claude-sonnet-4.6',
          source: 'squilla_router',
          accepted_routing_mode: 'ensemble',
        },
      },
    ])
    const withMessages = useChatRenderedMessages({
      messages,
      sessionKey: ref('router-ensemble-live-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: nextTurnMode,
    })

    const before = withMessages.renderedMessages.value
      .find(message => message.isRouterStrip)
    nextTurnMode.value = 'off'
    messages.value[1].restoredFromHistory = true
    const after = withMessages.renderedMessages.value
      .find(message => message.isRouterStrip)

    expect(before?.routerPanel).toBe('llm-ensemble')
    expect(before?.gridCells || []).toHaveLength(0)
    expect(after?.routerPanel).toBe('llm-ensemble')
    expect(after?.routerTurnKey).toBe(before?.routerTurnKey)
  })

  it('restores an accepted ensemble panel from durable cold-history outcome metadata', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0, turnId: 'turn-cold' },
        {
          role: 'assistant',
          text: 'answer',
          ts: 1,
          turnId: 'turn-cold',
          restoredFromHistory: true,
          turnOutcome: {
            turnId: 'turn-cold',
            status: 'succeeded',
            acceptedRoutingMode: 'ensemble',
          },
          usage: {
            routing_source: 'squilla_router',
            routed_tier: 'balanced',
            routed_model: 'anthropic/claude-sonnet-4.6',
            model_usage_breakdown: [
              { role: 'primary', provider: 'openrouter', model: 'openai/gpt-5.5' },
              { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'default',
              mode: 'custom_b5',
              llm_request_count: 2,
              total_candidates: 2,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-cold-history-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('off'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerTurnKey).toBe('router-event:turn-cold:1-ensemble-router')
  })

  it('does not reclassify a live router turn from the next-turn session mode', () => {
    const nextTurnMode = ref<ModelRoutingMode>('squilla_router')
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'squilla_router',
            accepted_routing_mode: 'router',
          },
        },
      ]),
      sessionKey: ref('router-next-turn-change-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: nextTurnMode,
    })

    const before = withMessages.renderedMessages.value
      .find(message => message.isRouterStrip)
    nextTurnMode.value = 'llm_ensemble'
    const after = withMessages.renderedMessages.value
      .find(message => message.isRouterStrip)

    expect(before?.routerPanel).toBe('real-candidates')
    expect(after?.routerPanel).toBe('real-candidates')
    expect(after?.routerTurnKey).toBe(before?.routerTurnKey)
  })

  it('keeps the tier router and appends ensemble execution when C3 owns fusion', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'c3',
            model: 'anthropic/claude-opus-4.8',
            source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-c3-ensemble-live-test'),
      routerSlots: ref(['c0', 'c1', 'c2', 'c3']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        c0: { model: 'qwen/qwen3.7-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek/deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c2: { model: 'z-ai/glm-5.2', supportsImage: false, imageOnly: false },
        c3: {
          model: 'anthropic/claude-opus-4.8',
          supportsImage: false,
          imageOnly: false,
          ensembleEnabled: true,
        },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('squilla_router'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    const c3Index = strip?.gridCells?.findIndex(cell => cell.tiers.includes('c3')) ?? -1
    expect(strip?.routerPanel).toBe('router-ensemble-sequence')
    expect(strip?.routerMode).toBe('squilla_router')
    expect(strip?.gridCells || []).toHaveLength(4)
    expect(c3Index).toBeGreaterThanOrEqual(0)
    expect(strip?.winnerIdx).toBe(c3Index)
    expect(strip?.gridCells?.[c3Index]).toMatchObject({
      executionKind: 'ensemble',
      tiers: ['c3'],
    })
  })

  it('keeps the original C3 route decision when live ensemble members arrive', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'c3',
            model: 'anthropic/claude-opus-4.8',
            source: 'squilla_router',
          },
          ensemble: {
            profile: 'default',
            modelCount: 1,
            totalCandidates: 4,
            requestCount: 1,
            fallbackUsed: false,
            fallbackReason: '',
            costUsd: 0,
            savedUsd: 0,
            savedPct: 0,
            models: [{
              role: 'anchor',
              label: 'anchor',
              provider: 'openrouter',
              model: 'qwen/qwen3.7-plus',
              modelShort: 'qwen3.7-plus',
              input: 0,
              output: 0,
              costUsd: 0,
              status: 'running',
            }],
          },
        },
      ]),
      sessionKey: ref('router-c3-ensemble-progress-test'),
      routerSlots: ref(['c0', 'c1', 'c2', 'c3']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        c0: { model: 'qwen/qwen3.7-flash', supportsImage: false, imageOnly: false },
        c1: { model: 'deepseek/deepseek-v4-flash', supportsImage: false, imageOnly: false },
        c2: { model: 'z-ai/glm-5.2', supportsImage: false, imageOnly: false },
        c3: {
          model: 'anthropic/claude-opus-4.8',
          supportsImage: false,
          imageOnly: false,
          ensembleEnabled: true,
        },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('squilla_router'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('router-ensemble-sequence')
    expect(strip?.routerSource).toBe('squilla_router')
    expect(strip?.gridCells || []).toHaveLength(4)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual(['qwen3.7-plus'])
  })

  it('labels an ensemble-enabled C3 as fusion inside a lower-tier routing decision', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'quick question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'c0',
            model: 'qwen/qwen3.7-flash',
            source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-c3-logical-cell-test'),
      routerSlots: ref(['c0', 'c3']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        c0: { model: 'qwen/qwen3.7-flash', supportsImage: false, imageOnly: false },
        c3: {
          model: 'anthropic/claude-opus-4.8',
          supportsImage: false,
          imageOnly: false,
          ensembleEnabled: true,
        },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('squilla_router'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    const c3Cell = strip?.gridCells?.find(cell => cell.tiers.includes('c3'))
    expect(strip?.routerPanel).toBe('real-candidates')
    expect(c3Cell).toMatchObject({
      displayName: 'claude-opus-4.8',
      executionKind: 'ensemble',
    })
  })

  it('renders the ensemble strip when the decision source is ensemble (per-message)', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
      ]),
      sessionKey: ref('router-ensemble-source-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('off'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.gridCells || []).toHaveLength(0)
  })

  it('keeps the live ensemble strip while its own turn is still streaming', () => {
    // A tool-using ensemble turn emits its breakdown mid-turn; the strip (and any
    // open trace inspector) must survive until the whole turn settles.
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Working on it…',
          ts: 2,
          messageId: 'assistant-ensemble-live',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
              { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'default',
              mode: 'router_dynamic',
              llm_request_count: 2,
              total_candidates: 3,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-live-stream-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming: ref(true),
    })

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip).toBeTruthy()
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'glm-5.2',
    ])
  })

  it('keeps the ensemble strip as a settled trace panel once the assistant answer completes', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Hi there.',
          ts: 2,
          messageId: 'assistant-ensemble',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
              { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'default',
              mode: 'router_dynamic',
              llm_request_count: 2,
              total_candidates: 3,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-completed-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
    })

    const rendered = api.renderedMessages.value
    const assistantIndex = rendered.findIndex(message => message.displayRole === 'assistant')
    const stripIndex = rendered.findIndex(message => message.isRouterStrip)
    const strip = rendered[stripIndex]

    expect(assistantIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeLessThan(assistantIndex)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'glm-5.2',
    ])
    expect(rendered[assistantIndex]?.meta?.ensemble?.modelCount).toBe(2)
  })

  it('replaces an empty live handoff strip with completed assistant usage trace', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerState: 'handoff',
          routerDecision: {
            tier: 'c1',
            model: '',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Done.',
          ts: 2,
          messageId: 'assistant-ensemble-handoff-complete',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'proposer', provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
              { role: 'aggregator', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'static_openrouter_b5',
              llm_request_count: 5,
              total_candidates: 5,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-handoff-complete-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming: ref(false),
    })

    const rendered = api.renderedMessages.value
    const strip = rendered.find(message => message.isRouterStrip)
    const assistantIndex = rendered.findIndex(message => message.displayRole === 'assistant')
    const stripIndex = rendered.findIndex(message => message.isRouterStrip)

    expect(stripIndex).toBeGreaterThan(-1)
    expect(assistantIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeLessThan(assistantIndex)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.routerState).toBe('settled')
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'deepseek-v4-pro',
      'glm-5.2',
    ])
  })

  it('keeps one router render key when a live strip becomes a settled trace', () => {
    const messages = ref<ChatMessage[]>([
      {
        role: 'user',
        text: 'compare candidates',
        ts: 1,
        turnId: 'turn-live',
        clientId: 'local-user-turn',
        messageId: 'server-user-turn',
      },
      {
        role: 'router',
        text: '',
        ts: 2,
        turnId: 'turn-live',
        messageId: 'router-live-event',
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c1',
          model: 'qwen/qwen3.7-plus',
          source: 'llm_ensemble',
        },
      },
    ])
    const isStreaming = ref(true)
    const api = useChatRenderedMessages({
      messages,
      sessionKey: ref('router-stable-key-test'),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming,
    })

    const liveKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey

    Object.assign(messages.value[1]!, {
      routerModelCallId: '1.0',
      routerIteration: 1,
    })
    const boundKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey

    messages.value.push({
      role: 'assistant',
      text: 'Settled answer.',
      ts: 3,
      turnId: 'turn-live',
      messageId: 'assistant-settled',
      usage: {
        router_model_call_id: '1.0',
        router_iteration: 1,
        model_usage_breakdown: [
          { role: 'proposer', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
          { role: 'aggregator', provider: 'openrouter', model: 'z-ai/glm-5.2' },
        ],
        ensemble_trace: {
          profile: 'default',
          total_candidates: 1,
          llm_request_count: 2,
        },
      },
    })
    isStreaming.value = false

    const settledKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey
    expect(liveKey).toBe('router-event:turn-live:router-live-event')
    expect(boundKey).toBe(liveKey)
    expect(settledKey).toBe(liveKey)

    messages.value = [
      {
        role: 'user',
        text: 'compare candidates',
        ts: 1,
        turnId: 'turn-live',
        messageId: 'server-user-turn',
      },
      {
        role: 'assistant',
        text: 'Partial answer.',
        ts: 2,
        turnId: 'turn-live',
        clientId: 'history-prefix',
        routerUsage: messages.value[2]!.usage,
        routerModelCallId: '1.0',
        restoredFromHistory: true,
      },
      {
        role: 'user',
        text: 'guide it',
        ts: 2.5,
        turnId: 'turn-live',
        inputDisposition: 'applied',
      },
      {
        role: 'assistant',
        text: 'Settled answer.',
        ts: 3,
        turnId: 'turn-live',
        messageId: 'assistant-settled',
        usage: messages.value[2]!.usage,
        restoredFromHistory: true,
      },
    ]
    const restoredKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey
    expect(restoredKey).toBe(liveKey)
  })

  it('settles the exact physical route card without replacing another card in the turn', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'question', ts: 1, turnId: 'turn-multi' },
        {
          role: 'router',
          text: '',
          ts: 2,
          turnId: 'turn-multi',
          messageId: 'router-first',
          provenanceKind: 'router_decision',
          routerModelCallId: '1.0',
          routerDecision: { tier: 'c1', model: 'provider/first', source: 'squilla_router' },
        },
        { role: 'assistant', text: 'first segment', ts: 3, turnId: 'turn-multi' },
        {
          role: 'user',
          text: 'guide it',
          ts: 4,
          turnId: 'turn-multi',
          inputDisposition: 'applied',
        },
        {
          role: 'router',
          text: '',
          ts: 5,
          turnId: 'turn-multi',
          messageId: 'router-second',
          provenanceKind: 'router_decision',
          routerModelCallId: '2.0',
          routerDecision: { tier: 'c2', model: 'provider/second', source: 'squilla_router' },
        },
        {
          role: 'assistant',
          text: 'settled first call',
          ts: 6,
          turnId: 'turn-multi',
          messageId: 'assistant-terminal',
          usage: {
            routed_tier: 'c1',
            routed_model: 'provider/first',
            routing_source: 'squilla_router',
            router_model_call_id: '1.0',
          },
        },
      ]),
      sessionKey: ref('router-exact-settlement-test'),
      routerSlots: ref(['c1', 'c2']),
      routerModels: ref({ c1: 'provider/first', c2: 'provider/second' }),
      routerTierConfigs: ref({
        c1: { model: 'provider/first', supportsImage: false, imageOnly: false },
        c2: { model: 'provider/second', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const strips = api.renderedMessages.value.filter(message => message.isRouterStrip)
    expect(strips).toHaveLength(2)
    expect(strips.map(strip => strip.routerModelCallId)).toEqual(['1.0', '2.0'])
    expect(strips.map(strip => strip.routerTurnKey)).toEqual([
      'router-call:turn-multi:1.0',
      'router-call:turn-multi:2.0',
    ])
    expect(strips.map(strip => strip.gridCells?.[strip.winnerIdx ?? -1]?.model)).toEqual([
      'provider/first',
      'provider/second',
    ])
    expect(strips.map(strip => strip.routerSettled)).toEqual([true, false])
  })

  it('does not reuse an incompatible card key for an anchored ensemble settlement', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'question', ts: 1, turnId: 'turn-ensemble-anchor' },
        {
          role: 'router',
          text: '',
          ts: 2,
          turnId: 'turn-ensemble-anchor',
          messageId: 'router-second-call',
          provenanceKind: 'router_decision',
          routerModelCallId: '2.0',
          routerDecision: { tier: 'c2', model: 'provider/second', source: 'squilla_router' },
        },
        {
          role: 'assistant',
          text: 'settled ensemble call',
          ts: 3,
          turnId: 'turn-ensemble-anchor',
          messageId: 'assistant-terminal',
          usage: {
            routed_tier: 'c1',
            routed_model: 'provider/first',
            routing_source: 'squilla_router',
            router_model_call_id: '1.0',
            model_usage_breakdown: [
              { role: 'proposer', provider: 'provider', model: 'member/one' },
              { role: 'aggregator', provider: 'provider', model: 'member/final' },
            ],
            ensemble_trace: {
              profile: 'default',
              total_candidates: 1,
              llm_request_count: 2,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-anchor-test'),
      routerSlots: ref(['c1', 'c2']),
      routerModels: ref({ c1: 'provider/first', c2: 'provider/second' }),
      routerTierConfigs: ref({
        c1: { model: 'provider/first', supportsImage: false, imageOnly: false },
        c2: { model: 'provider/second', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('squilla_router'),
    })

    const strips = api.renderedMessages.value.filter(message => message.isRouterStrip)
    expect(strips).toHaveLength(2)
    expect(strips.map(strip => strip.routerModelCallId)).toEqual(['2.0', '1.0'])
    expect(strips.map(strip => strip.routerTurnKey)).toEqual([
      'router-call:turn-ensemble-anchor:2.0',
      'router-call:turn-ensemble-anchor:1.0',
    ])
    expect(new Set(strips.map(strip => strip.routerTurnKey)).size).toBe(2)
    expect(strips[1]?.routerPanel).toBe('router-ensemble-sequence')
  })

  it('uses latest-unresolved fallback settlement for gateway events without call anchors', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'question', ts: 1, turnId: 'turn-legacy' },
        {
          role: 'router',
          text: '',
          ts: 2,
          turnId: 'turn-legacy',
          messageId: 'router-first',
          provenanceKind: 'router_decision',
          routerDecision: { tier: 'c1', model: 'provider/first', source: 'squilla_router' },
        },
        { role: 'assistant', text: 'first segment', ts: 3, turnId: 'turn-legacy' },
        {
          role: 'router',
          text: '',
          ts: 4,
          turnId: 'turn-legacy',
          messageId: 'router-second',
          provenanceKind: 'router_decision',
          routerDecision: { tier: 'c2', model: 'provider/second', source: 'squilla_router' },
        },
        {
          role: 'assistant',
          text: 'legacy terminal',
          ts: 5,
          turnId: 'turn-legacy',
          messageId: 'assistant-terminal',
          usage: {
            routed_tier: 'c2',
            routed_model: 'provider/second',
            routing_source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-fallback-settlement-test'),
      routerSlots: ref(['c1', 'c2']),
      routerModels: ref({ c1: 'provider/first', c2: 'provider/second' }),
      routerTierConfigs: ref({
        c1: { model: 'provider/first', supportsImage: false, imageOnly: false },
        c2: { model: 'provider/second', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const strips = api.renderedMessages.value.filter(message => message.isRouterStrip)
    expect(strips).toHaveLength(2)
    expect(strips.map(strip => strip.routerTurnKey)).toEqual([
      'router-event:turn-legacy:router-first',
      'router-event:turn-legacy:router-second',
    ])
    expect(strips.map(strip => strip.gridCells?.[strip.winnerIdx ?? -1]?.model)).toEqual([
      'provider/first',
      'provider/second',
    ])
    expect(strips.map(strip => strip.routerSettled)).toEqual([false, true])
  })

  it('keeps same-turn steer rows under one explicit turn and one Router strip', () => {
    const messages = ref<ChatMessage[]>([{
        role: 'user',
        text: 'original request',
        ts: 1,
        messageId: 'user-original',
        turnId: 'turn-1',
      },
      {
        role: 'router',
        text: '',
        ts: 2,
        messageId: 'router-turn-1',
        turnId: 'turn-1',
        provenanceKind: 'router_decision',
        routerModelCallId: '1.0',
        routerIteration: 1,
        routerDecision: {
          tier: 'c1',
          model: 'qwen/qwen3.7-plus',
          source: 'squilla_router',
        },
      },
      {
        role: 'assistant',
        text: 'first segment',
        ts: 3,
        messageId: 'assistant-first',
        turnId: 'turn-1',
      },
      {
        role: 'user',
        text: 'focus on compatibility',
        ts: 4,
        messageId: 'user-steer',
        turnId: 'turn-1',
        inputDisposition: 'applied',
      },
      {
        role: 'assistant',
        text: 'continued segment',
        ts: 5,
        messageId: 'assistant-continuation',
        turnId: 'turn-1',
      },
    ])
    const api = useChatRenderedMessages({
      messages,
      sessionKey: ref('same-turn-steer-router'),
      routerSlots: ref(['fast', 'balanced']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'qwen/qwen3.7-plus', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      modelRoutingMode: ref('squilla_router'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    const rendered = api.renderedMessages.value
    expect(rendered.map(message => message.displayRole)).toEqual([
      'user',
      'router',
      'assistant',
      'user',
      'assistant',
    ])
    expect(rendered.filter(message => message.isRouterStrip)).toHaveLength(1)
    expect(rendered[1]).toMatchObject({
      routerModelCallId: '1.0',
      routerIteration: 1,
      routerTurnKey: 'router-call:turn-1:1.0',
    })
    expect(rendered.filter(message => !message.isRouterStrip).map(message => message.turnKey))
      .toEqual([
        'turn:turn-1',
        'turn:turn-1',
        'turn:turn-1',
        'turn:turn-1',
      ])
    expect(rendered.find(message => message.messageId === 'user-steer')).toMatchObject({
      inputDisposition: 'applied',
      turnKey: 'turn:turn-1',
    })
  })

  it('uses explicit turn ids from assistant and Router rows without an adjacent user anchor', () => {
    const messages = ref<ChatMessage[]>([
      {
        role: 'assistant',
        text: 'completed old turn',
        ts: 1,
        messageId: 'assistant-old',
        turnId: 'turn-old',
      },
      {
        role: 'router',
        text: '',
        ts: 2,
        messageId: 'router-new',
        turnId: 'turn-new',
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c1',
          model: 'qwen/qwen3.7-plus',
          source: 'squilla_router',
        },
      },
      {
        role: 'assistant',
        text: 'continued new turn',
        ts: 3,
        messageId: 'assistant-new',
        turnId: 'turn-new',
      },
    ])
    const api = useChatRenderedMessages({
      messages,
      sessionKey: ref('explicit-role-turns'),
      routerSlots: ref(['fast', 'balanced']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'qwen/qwen3.7-plus', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      modelRoutingMode: ref('squilla_router'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    expect(api.renderedMessages.value.map(message => [
      message.messageId,
      message.turnKey,
    ])).toEqual([
      ['assistant-old', 'turn:turn-old'],
      ['router-new', 'turn:turn-new'],
      ['assistant-new', 'turn:turn-new'],
    ])
  })

  it('adopts a late explicit Router turn id onto its optimistic user row', () => {
    const messages = ref<ChatMessage[]>([
      {
        role: 'user',
        text: 'optimistic request',
        ts: 1,
        clientId: 'local-user',
      },
      {
        role: 'router',
        text: '',
        ts: 2,
        messageId: 'router-server',
        turnId: 'turn-server',
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c1',
          model: 'qwen/qwen3.7-plus',
          source: 'squilla_router',
        },
      },
      {
        role: 'assistant',
        text: 'server output',
        ts: 3,
        turnId: 'turn-server',
      },
    ])
    const api = useChatRenderedMessages({
      messages,
      sessionKey: ref('late-explicit-turn'),
      routerSlots: ref(['fast', 'balanced']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'qwen/qwen3.7-plus', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      modelRoutingMode: ref('squilla_router'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })

    expect(api.renderedMessages.value.map(message => message.turnKey)).toEqual([
      'turn:turn-server',
      'turn:turn-server',
      'turn:turn-server',
    ])
    expect(api.renderedMessages.value[1]?.routerTurnKey).toBe(
      'router-event:turn-server:router-server',
    )
  })
})

describe('useChatRenderedMessages per-turn usage', () => {
  it('marks partial assistant output when a following error terminates the turn', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
      },
      {
        role: 'assistant',
        text: 'Partial result',
        ts: 2,
        messageId: 'assistant-partial',
        tool_calls: [{
          id: 'read-1',
          name: 'read_file',
          input: { path: '/repo/file.ts' },
          result: 'content',
        }],
      },
      {
        role: 'error',
        text: 'Provider failed',
        ts: 3,
        messageId: 'error-1',
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBe(true)
    expect(assistant?.text).toBe('Partial result')
  })

  it('marks partial assistant output failed after a durable terminal system row is restored', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
        restoredFromHistory: true,
      },
      {
        role: 'assistant',
        text: 'Partial result',
        ts: 2,
        messageId: 'assistant-partial',
        restoredFromHistory: true,
        turnId: 'turn-1',
      },
      {
        role: 'system',
        text: 'localized durable terminal detail',
        ts: 3,
        messageId: 'system-terminal-1',
        restoredFromHistory: true,
        turnId: 'turn-1',
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBe(true)
  })

  it('does not treat ordinary local or provenance-tagged system rows as terminal failures', () => {
    const local = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete',
      },
      {
        role: 'system',
        text: 'ordinary local notice',
        ts: 3,
      },
    ])
    const durableCron = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-2',
        restoredFromHistory: true,
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete-2',
        restoredFromHistory: true,
      },
      {
        role: 'system',
        text: 'ordinary durable notice',
        ts: 3,
        messageId: 'system-cron-1',
        restoredFromHistory: true,
        provenanceKind: 'cron',
      },
    ])

    const localAssistant = local.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    const cronAssistant = durableCron.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(localAssistant?.terminalFailure).toBeUndefined()
    expect(cronAssistant?.terminalFailure).toBeUndefined()
  })

  it('does not treat an unprovenanced durable system row without matching turn identity as terminal', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-3',
        restoredFromHistory: true,
        turnId: 'turn-3',
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete-3',
        restoredFromHistory: true,
        turnId: 'turn-3',
      },
      {
        role: 'system',
        text: 'ordinary injected durable notice',
        ts: 3,
        messageId: 'system-injected-1',
        restoredFromHistory: true,
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBeUndefined()
  })

  it('keeps each assistant message token counts independent', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'first answer',
        ts: 1,
        messageId: 'assistant-1',
        usage: { input_tokens: 11, output_tokens: 3 },
      },
      {
        role: 'user',
        text: 'next question',
        ts: 2,
        messageId: 'user-2',
      },
      {
        role: 'assistant',
        text: 'second answer',
        ts: 3,
        messageId: 'assistant-3',
        usage: { input_tokens: 22, output_tokens: 5 },
      },
    ])

    const assistantMessages = api.renderedMessages.value.filter(
      message => message.displayRole === 'assistant',
    )
    expect(assistantMessages.map(message => message.meta?.input)).toEqual([11, 22])
    expect(assistantMessages.map(message => message.meta?.output)).toEqual([3, 5])
  })

  it('normalizes additive per-turn coverage while older usage remains exact', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'known subtotal',
        ts: 1,
        usage: {
          input_tokens: 11,
          output_tokens: 3,
          cost_usd: 0.001,
          coverage_status: 'usage_unknown',
          usage_unknown: true,
          unknown_usage_events: 1,
        },
      },
      {
        role: 'assistant',
        text: 'unknown only',
        ts: 2,
        usage: {
          coverageStatus: 'usage_unknown',
          usageUnknown: true,
          unknownUsageEvents: 2,
        },
      },
      {
        role: 'assistant',
        text: 'legacy exact usage',
        ts: 3,
        usage: { input_tokens: 7, output_tokens: 2 },
      },
    ])

    const [partial, unknownOnly, legacy] = api.renderedMessages.value
    expect(partial?.meta).toMatchObject({
      coverageStatus: 'usage_unknown',
      usageUnknown: true,
      unknownUsageEvents: 1,
      hasKnownUsage: true,
    })
    expect(unknownOnly?.meta).toMatchObject({
      coverageStatus: 'usage_unknown',
      usageUnknown: true,
      unknownUsageEvents: 2,
      hasKnownUsage: false,
    })
    expect(legacy?.meta).toMatchObject({
      usageUnknown: false,
      unknownUsageEvents: 0,
      hasKnownUsage: true,
    })
    expect(legacy?.meta?.coverageStatus).toBeUndefined()
  })

  it('does not reinterpret missing cost entries as a presentation state', () => {
    const api = renderedMessagesFor([{
      role: 'assistant',
      text: 'restored cost',
      ts: 1,
      usage: {
        input_tokens: 11,
        output_tokens: 3,
        cost_usd: 0.0123,
        missing_cost_entries: 1,
      },
    }])

    expect(api.renderedMessages.value[0]?.meta).toMatchObject({
      usageUnknown: false,
      costUsd: 0.0123,
    })
    expect(api.renderedMessages.value[0]?.meta).not.toHaveProperty('costKnown')
    expect(api.renderedMessages.value[0]?.meta).not.toHaveProperty('costIncomplete')
    expect(api.renderedMessages.value[0]?.meta).not.toHaveProperty('missingCostEntries')
  })
})

describe('useChatRenderedMessages clarify history recovery', () => {
  it('restores a clarify interrupt from persisted meta-step tool input', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'Please reply with these fields.',
        ts: 0,
        messageId: 'm-clarify',
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            input: {
              kind: 'user_input',
              paused: true,
              step: 'project_clarify',
              run_id: 'run-1',
              clarify_schema: {
                mode: 'form',
                intro: 'A few details.',
                fields: [
                  {
                    name: 'topic',
                    type: 'string',
                    required: true,
                    prompt: 'Project topic',
                  },
                  {
                    name: 'age_band',
                    type: 'enum',
                    required: true,
                    prompt: 'Child age band',
                    choices: ['PRE_K', 'EARLY_GRADE'],
                  },
                ],
              },
            },
          },
          {
            type: 'tool_result',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            result: "paused: awaiting user input (step 'project_clarify')",
          },
        ],
      },
    ])

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify).toBeTruthy()
    expect(clarify?.key).toBe('m-clarify:interrupt:run-1|project_clarify')
    expect(clarify?.clarify?.intro).toBe('A few details.')
    expect(clarify?.clarify?.fields.map(field => field.name)).toEqual([
      'topic',
      'age_band',
    ])
    expect(clarify?.clarify?.fields[1].choices).toEqual(['PRE_K', 'EARLY_GRADE'])
  })

  it('restores request_user_input from persisted tool_result JSON without arguments', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: '',
        ts: 0,
        messageId: 'm-request-user-input',
        tool_calls: [
          {
            type: 'tool_result',
            tool_use_id: 'request-input-2',
            name: 'request_user_input',
            result: JSON.stringify({
              kind: 'user_input',
              paused: true,
              run_id: 'plan-run-2',
              step: 'choose_target',
              clarify_schema: {
                intro: 'Choose where to implement.',
                fields: [{
                  name: 'target',
                  type: 'enum',
                  prompt: 'Implementation target',
                  choices: ['current', 'new'],
                }],
              },
            }),
          },
        ],
      },
    ])

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify?.key).toBe('m-request-user-input:interrupt:plan-run-2|choose_target')
    expect(clarify?.clarify).toEqual({
      intro: 'Choose where to implement.',
      fields: [{
        name: 'target',
        type: 'enum',
        prompt: 'Implementation target',
        required: false,
        defaultValue: '',
        choices: ['current', 'new'],
      }],
      runId: 'plan-run-2',
      step: 'choose_target',
    })
  })

  it('restores and settles a terminal request from its preserved request payload', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: '',
        ts: 0,
        messageId: 'm-terminal-request-user-input',
        tool_calls: [
          {
            type: 'tool_result',
            tool_use_id: 'request-input-terminal',
            name: 'request_user_input',
            user_input_request: {
              status: 'input_required',
              kind: 'user_input',
              paused: true,
              request_id: 'request-terminal-1',
              run_id: 'plan-run-2',
              step: 'choose_target',
              clarify_schema: {
                mode: 'form',
                presentation: 'plan_questionnaire_v1',
                fields: [{
                  name: 'target',
                  type: 'enum',
                  required: true,
                  choices: ['current', 'new'],
                }],
              },
            },
            result: JSON.stringify({
              status: 'answered',
              kind: 'user_input',
              paused: false,
              request_id: 'request-terminal-1',
              answers: { target: 'current' },
            }),
          },
        ],
      },
    ])

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify?.key).toBe(
      'm-terminal-request-user-input:interrupt:request-terminal-1',
    )
    expect(clarify?.resolution).toBe('replied')
    expect(clarify?.clarify?.presentation).toBe('plan_questionnaire_v1')
  })

  it('keeps consecutive requests distinct by requestId', () => {
    const request = (requestId: string) => ({
      status: 'input_required',
      kind: 'user_input',
      paused: true,
      request_id: requestId,
      run_id: 'same-run',
      step: 'same-step',
      clarify_schema: {
        fields: [{ name: 'scope', type: 'string' }],
      },
    })
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: '',
        ts: 0,
        messageId: 'm-consecutive-requests',
        tool_calls: [
          {
            type: 'tool_result',
            tool_use_id: 'request-input-1',
            result: request('request-1'),
          },
          {
            type: 'tool_result',
            tool_use_id: 'request-input-2',
            result: request('request-2'),
          },
        ],
      },
    ])

    const keys = api.renderedMessages.value[0].parts
      ?.filter(part => part.type === 'interrupt')
      .map(part => part.key)
    expect(keys).toEqual([
      'm-consecutive-requests:interrupt:request-1',
      'm-consecutive-requests:interrupt:request-2',
    ])
  })

  it('applies clarify submit state to recovered historical interrupt cards', () => {
    const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map([
      ['run-1|project_clarify', {
        resolution: 'replied',
        busy: true,
        error: '',
      }],
    ]))
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'Please reply with these fields.',
        ts: 0,
        messageId: 'm-clarify',
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            input: {
              kind: 'user_input',
              paused: true,
              step: 'project_clarify',
              run_id: 'run-1',
              clarify_schema: {
                mode: 'form',
                fields: [
                  {
                    name: 'topic',
                    type: 'string',
                    required: true,
                  },
                ],
              },
            },
          },
        ],
      },
    ], interruptState)

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify?.resolution).toBe('replied')
    expect(clarify?.busy).toBe(true)
  })
})

describe('useChatRenderedMessages ensemble metadata', () => {
  it('reconstructs a settled ensemble strip from completed assistant usage', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'Compare multiple recent AI policy updates.',
        ts: 0,
      },
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 1,
        messageId: 'm-ensemble-strip',
        usage: {
          model_usage_breakdown: [
            { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
            { role: 'research', provider: 'openrouter', model: 'moonshotai/kimi-k2.6' },
            { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 3,
            total_candidates: 8,
            fallback_used: false,
          },
        },
      },
    ], undefined, true)

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    const assistant = api.renderedMessages.value.find(message => message.displayRole === 'assistant')

    expect(strip).toBeTruthy()
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.ensemble?.modelCount).toBe(3)
    expect(assistant?.meta?.ensemble?.modelCount).toBe(3)
    expect(assistant?.meta?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'kimi-k2.6',
      'glm-5.2',
    ])
  })

  it('normalizes ensemble model breakdown, cost, and savings into assistant meta', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        messageId: 'm-ensemble',
        usage: {
          model: 'z-ai/glm-5.2',
          cost_usd: 0.123456,
          total_savings_usd: 0.045,
          total_savings_pct: 26,
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'Proposer 1',
              provider: 'openrouter',
              model: 'deepseek/deepseek-v4-pro',
              input_tokens: 10,
              output_tokens: 2,
              billed_cost: 0.01,
              elapsed_ms: 105_000,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'z-ai/glm-5.2',
              input_tokens: 20,
              output_tokens: 8,
              billed_cost: 0.02,
              elapsed_ms: 12_000,
            },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 2,
            fallback_used: false,
          },
        },
      },
    ])

    const [message] = api.renderedMessages.value
    expect(message.meta?.ensemble).toMatchObject({
      profile: 'default',
      modelCount: 2,
      requestCount: 2,
      costUsd: 0.123456,
      savedUsd: 0.045,
      savedPct: 26,
      fallbackUsed: false,
    })
    expect(message.meta?.ensemble?.models.map(model => model.model)).toEqual([
      'deepseek/deepseek-v4-pro',
      'z-ai/glm-5.2',
    ])
    expect(message.meta?.ensemble?.models.map(model => model.elapsedMs)).toEqual([
      105_000,
      12_000,
    ])
  })

  it.each([
    ['current API', 'aggregator'],
    ['legacy API', 'primary_aggregator'],
  ])('folds a %s continuation into the logical aggregator member', (_, continuationRole) => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'tool-assisted fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'primary',
              provider: 'tokenrhythm',
              model: 'deepseek-v4-flash-0731',
              input_tokens: 10,
              output_tokens: 2,
              cost_usd: 0.01,
              elapsed_ms: 1_000,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'tokenrhythm',
              model: 'deepseek-v4-flash-0731',
              input_tokens: 20,
              output_tokens: 4,
              cost_usd: 0.02,
              elapsed_ms: 2_000,
            },
            {
              role: continuationRole,
              label: continuationRole,
              provider: 'tokenrhythm',
              model: 'deepseek-v4-flash-0731',
              input_tokens: 30,
              output_tokens: 6,
              cost_usd: 0.03,
              elapsed_ms: 3_000,
            },
          ],
          ensemble_trace: {
            profile: 'custom_b5',
            llm_request_count: 3,
            fallback_used: false,
          },
        },
      },
    ])

    const ensemble = api.renderedMessages.value[0].meta?.ensemble
    expect(ensemble?.requestCount).toBe(3)
    expect(ensemble?.modelCount).toBe(2)
    expect(ensemble?.models.map(model => model.role)).toEqual([
      'proposer',
      'aggregator',
    ])
    expect(ensemble?.models[1]).toMatchObject({
      role: 'aggregator',
      label: 'aggregator',
      provider: 'tokenrhythm',
      model: 'deepseek-v4-flash-0731',
      input: 50,
      output: 10,
      costUsd: 0.05,
      elapsedMs: 5_000,
    })
  })

  it('keeps the numeric aggregator subtotal without adding display-only completeness state', () => {
    const api = renderedMessagesFor([{
      role: 'assistant',
      text: 'tool-assisted fused answer',
      ts: 0,
      usage: {
        model_usage_breakdown: [
          {
            role: 'proposer',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.01,
            cost_source: 'provider_billed',
          },
          {
            role: 'aggregator',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.02,
            cost_source: 'provider_billed',
          },
          {
            role: 'primary_aggregator',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0,
            cost_source: 'unavailable',
            missing_cost_entries: 1,
          },
        ],
        ensemble_trace: {
          profile: 'custom_b5',
          llm_request_count: 3,
          fallback_used: false,
        },
      },
    }])

    const message = api.renderedMessages.value[0]
    expect(message.meta).toMatchObject({
      usageUnknown: false,
    })
    expect(message.meta?.ensemble?.models[1]).toMatchObject({
      role: 'aggregator',
      costUsd: 0.02,
    })
    expect(message.meta).not.toHaveProperty('costIncomplete')
    expect(message.meta?.ensemble?.models[1]).not.toHaveProperty('costIncomplete')
  })

  it('normalizes legacy candidate labels to one public proposer role', () => {
    const api = renderedMessagesFor([{
      role: 'assistant',
      text: 'fused answer',
      ts: 0,
      usage: {
        model_usage_breakdown: ['primary', 'contrast', 'fast_check', 'critic'].map((label, index) => ({
          role: 'proposer',
          label,
          provider: 'tokenrhythm',
          model: `model-${index + 1}`,
          input_tokens: 10,
          output_tokens: 2,
        })),
        ensemble_trace: {
          profile: 'custom_b5',
          llm_request_count: 4,
          fallback_used: false,
        },
      },
    }])

    expect(api.renderedMessages.value[0].meta?.ensemble?.models.map(model => ({
      role: model.role,
      label: model.label,
    }))).toEqual([
      { role: 'proposer', label: 'proposer' },
      { role: 'proposer', label: 'proposer' },
      { role: 'proposer', label: 'proposer' },
      { role: 'proposer', label: 'proposer' },
    ])
  })

  it('keeps an authoritative zero ledger total over stale breakdown subtotals', () => {
    const api = renderedMessagesFor([{
      role: 'assistant',
      text: 'settled free turn',
      ts: 0,
      usage: {
        // Authoritative ledger total for the turn: genuinely $0.
        cost_usd: 0,
        // A richer historical projection can still carry non-zero per-row
        // subtotals from an earlier receipt. They must not win.
        model_usage_breakdown: [
          {
            role: 'proposer',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.01,
            cost_source: 'provider_billed',
          },
          {
            role: 'aggregator',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.02,
            cost_source: 'provider_billed',
          },
        ],
        ensemble_trace: {
          profile: 'custom_b5',
          llm_request_count: 2,
          fallback_used: false,
        },
      },
    }])

    expect(api.renderedMessages.value[0].meta?.ensemble?.costUsd).toBe(0)
  })

  it('falls back to breakdown subtotals when the ledger omits a cost field', () => {
    const api = renderedMessagesFor([{
      role: 'assistant',
      text: 'legacy row without a ledger total',
      ts: 0,
      usage: {
        // No cost_usd/costUsd key at all — legacy shape, so the per-row
        // subtotals remain the only available signal.
        model_usage_breakdown: [
          {
            role: 'proposer',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.01,
            cost_source: 'provider_billed',
          },
          {
            role: 'aggregator',
            provider: 'tokenrhythm',
            model: 'deepseek-v4-flash-0731',
            cost_usd: 0.02,
            cost_source: 'provider_billed',
          },
        ],
        ensemble_trace: {
          profile: 'custom_b5',
          llm_request_count: 2,
          fallback_used: false,
        },
      },
    }])

    expect(api.renderedMessages.value[0].meta?.ensemble?.costUsd).toBeCloseTo(0.03, 10)
  })

  it('preserves candidate terminal status from the ensemble trace after settlement', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'proposer_1',
              provider: 'openrouter',
              model: 'deepseek/deepseek-v4-pro',
              sample_index: 0,
              input_tokens: 10,
              output_tokens: 2,
              elapsed_ms: 5_700,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'z-ai/glm-5.2',
              input_tokens: 20,
              output_tokens: 8,
              elapsed_ms: 12_000,
            },
          ],
          ensemble_trace: {
            profile: 'default',
            total_candidates: 3,
            candidates: [
              {
                label: 'proposer_1',
                provider: 'openrouter',
                model: 'deepseek/deepseek-v4-pro',
                sample_index: 0,
                ok: true,
                elapsed_ms: 5_700,
              },
              {
                label: 'proposer_2',
                provider: 'openrouter',
                model: 'z-ai/glm-5.2',
                sample_index: 0,
                ok: false,
                elapsed_ms: 21_000,
                error: 'proposer cancelled after 5s ensemble quorum grace',
                error_code: 'quorum_cancelled',
              },
              {
                label: 'proposer_3',
                provider: 'openrouter',
                model: 'moonshotai/kimi-k2.7',
                sample_index: 0,
                ok: false,
                elapsed_ms: 7_000,
                error: 'provider timed out',
                error_code: 'timeout',
              },
            ],
          },
        },
      },
    ])

    const models = api.renderedMessages.value[0].meta?.ensemble?.models
    expect(models).toHaveLength(4)
    expect(models?.[0]).toMatchObject({
      model: 'deepseek/deepseek-v4-pro',
      input: 10,
      output: 2,
      status: 'done',
    })
    expect(models?.[1]).toMatchObject({
      model: 'z-ai/glm-5.2',
      status: 'skipped',
      errorCode: 'quorum_cancelled',
      elapsedMs: 21_000,
    })
    expect(models?.[2]).toMatchObject({
      model: 'moonshotai/kimi-k2.7',
      status: 'failed',
      errorCode: 'timeout',
    })
    expect(models?.[3]).toMatchObject({
      role: 'aggregator',
      model: 'z-ai/glm-5.2',
      input: 20,
      output: 8,
    })
    expect(models?.[3]?.status).toBeUndefined()
  })

  it('does not undercount requests when a turn has multiple ensemble breakdown rows', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            { role: 'proposer', provider: 'openrouter', model: 'p1' },
            { role: 'aggregator', provider: 'openrouter', model: 'a1' },
            { role: 'proposer', provider: 'openrouter', model: 'p2' },
            { role: 'aggregator', provider: 'openrouter', model: 'a2' },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 2,
          },
        },
      },
    ])

    expect(api.renderedMessages.value[0].meta?.ensemble?.requestCount).toBe(4)
  })

  it('does not count an unavailable trace candidate as a physical request', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'proposer_1',
              provider: 'openrouter',
              model: 'p1',
            },
            {
              role: 'proposer',
              label: 'proposer_2',
              provider: 'openrouter',
              model: 'p2',
            },
            {
              role: 'proposer',
              label: 'proposer_3',
              provider: 'openrouter',
              model: 'p3',
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'agg',
            },
          ],
          ensemble_trace: {
            profile: 'default',
            total_candidates: 4,
            llm_request_count: 4,
            candidates: [
              {
                label: 'proposer_1',
                provider: 'openrouter',
                model: 'p1',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_2',
                provider: 'openrouter',
                model: 'p2',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_3',
                provider: 'openrouter',
                model: 'p3',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_4',
                provider: 'openrouter',
                model: 'unavailable',
                request_started: false,
                ok: false,
                error: 'proposer deployment is not ready',
                error_code: 'deployment_unavailable',
              },
            ],
          },
        },
      },
    ])

    const ensemble = api.renderedMessages.value[0].meta?.ensemble
    expect(ensemble?.models).toHaveLength(5)
    expect(ensemble?.requestCount).toBe(4)
  })
})
