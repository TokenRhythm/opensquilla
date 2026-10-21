import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './session-read-fixtures'

// Synthetic retained history, not a copied profile. The v2 snapshot binds each
// prose, reasoning, and tool segment to the same chronology used in production.
export const CONTINUOUS_SESSION = 'agent:main:webchat:continuous-reading-acceptance'
export const CONTINUOUS_MESSAGE = 'continuous-reading-answer'
const TURN = 'continuous-reading-turn'
const TIME = Date.UTC(2026, 9, 10, 2, 0, 0) / 1000
export const INTRO = '我先检查页面结构，再调整文字和工具记录的阅读节奏。保留原来的配色，工具详情按需展开。'
export const FOLLOWUP = '文件已写好。现在检查脚本语法，并确认长命令输出只在卡片内滚动。'
export const ANSWER = [
  '连续阅读验收完成。正文和过程说明保持相同字号，工具记录与用时退到次要层级。',
  '这是一段用于观察段落间距的中文正文。阅读时可以沿着同一条左侧基线浏览说明、工具和结论，不需要在不同缩进之间来回寻找下一段。',
  '### 检查结果',
  '- 原版 Miami 配色保持不变。\n- 工具和思考默认收起，手动展开后保留选择。\n- 长内容保留内部滚动，不撑宽整个页面。',
  '```css\n.message {\n  color: var(--text);\n  line-height: 1.7;\n}\n```',
  '接下来可以继续检查实际会话中的长段落、混合工具和代码。这里使用固定验收数据，没有调用模型或读取用户聊天记录。',
].join('\n\n')
const REASONING = '先确认原来的页面结构和工具调用顺序，再检查正文与输入框的对齐。'
const FILE_CONTENT = '<!doctype html>\n<html lang="zh-CN">\n<head><meta charset="utf-8"><title>连续阅读验收</title></head>\n<body>\n  <main>保留当前主题</main>\n</body>\n</html>'
export const LONG_OUTPUT = Array.from({ length: 45 }, (_, index) =>
  `PASS ${String(index + 1).padStart(2, '0')} ${index === 12 ? 'long_single_line_' + 'x'.repeat(300) : 'layout and interaction acceptance check'}`,
).join('\n')

const texts = [INTRO, FOLLOWUP, ANSWER]
const entries = [
  { type: 'reasoning', id: 'fixture-reasoning', order: 10, block_index: 0,
    started_at: TIME * 1000, ended_at: (TIME + 4) * 1000, status: 'completed',
    content_kind: 'reasoning', text_start_utf16: 0, text_end_utf16: REASONING.length },
  { type: 'segment', id: 'text:0', order: 20, segment_type: 'text', text_index: 0, text_utf16_length: texts[0]!.length, at: (TIME + 4) * 1000, ended_at: (TIME + 8) * 1000 },
  { type: 'segment', id: 'tool:fixture-write', order: 30, segment_type: 'tool', tool_use_id: 'fixture-write', name: 'write_file', is_error: false, started_at: (TIME + 8) * 1000, ended_at: (TIME + 12) * 1000 },
  { type: 'segment', id: 'text:1', order: 40, segment_type: 'text', text_index: 1, text_utf16_length: texts[1]!.length, at: (TIME + 12) * 1000, ended_at: (TIME + 16) * 1000 },
  { type: 'segment', id: 'tool:fixture-shell', order: 50, segment_type: 'tool', tool_use_id: 'fixture-shell', name: 'shell', is_error: false, started_at: (TIME + 16) * 1000, ended_at: (TIME + 80) * 1000 },
  { type: 'segment', id: 'text:2', order: 60, segment_type: 'text', text_index: 2, text_utf16_length: texts[2]!.length, at: (TIME + 80) * 1000, ended_at: (TIME + 84) * 1000 },
]

export const continuousHistory = chatHistoryPayload([
  { role: 'user', id: 'continuous-reading-user', text: '检查页面排版，保留原版 Miami 配色。', timestamp: TIME,
    turn_context: { turn_id: TURN } },
  { role: 'assistant', id: CONTINUOUS_MESSAGE, message_id: CONTINUOUS_MESSAGE,
    text: ANSWER, timestamp: TIME + 84, reasoning_content: REASONING,
    turn_context: { turn_id: TURN },
    tool_calls: [
      { type: 'text', text: INTRO },
      { type: 'tool_use', tool_use_id: 'fixture-write', name: 'write_file', input: { path: 'demo/index.html', content: FILE_CONTENT } },
      { type: 'tool_result', tool_use_id: 'fixture-write', name: 'write_file', result: 'Written demo/index.html', file_snapshot: { kind: 'write', path: 'demo/index.html', content: FILE_CONTENT } },
      { type: 'text', text: FOLLOWUP },
      { type: 'tool_use', tool_use_id: 'fixture-shell', name: 'shell', input: { command: 'npm run test:layout', timeout: 120 } },
      { type: 'tool_result', tool_use_id: 'fixture-shell', name: 'shell', result: `exit_code=0\n${LONG_OUTPUT}` },
      { type: 'text', text: ANSWER },
    ],
  },
], {
  turn_outcomes: [{ turn_id: TURN, task_id: TURN, status: 'succeeded', started_at: TIME, finished_at: TIME + 84, outcome: {},
    activity_snapshot: { version: 2, task_id: TURN, turn_id: TURN, complete: true,
      reasoning_utf16_length: REASONING.length, entries } }],
})

export const continuousPayloads: Record<string, unknown> = {
  'chat.history': continuousHistory,
  'sessions.messages.subscribe': sessionMessagesSubscribePayload(CONTINUOUS_SESSION),
  'sessions.messages.snapshot': sessionMessagesSnapshotPayload(CONTINUOUS_SESSION),
  'sessions.messages.hydrate': sessionMessagesHydratePayload(CONTINUOUS_SESSION),
  'sessions.list': { sessions: [{ key: CONTINUOUS_SESSION, title: '连续阅读验收', updated_at: TIME + 84, message_count: 2 }], count: 1, ts: TIME + 84, has_more: false },
  'sessions.resolve': { key: CONTINUOUS_SESSION, session_key: CONTINUOUS_SESSION },
  'agents.list': { agents: [] },
  'commands.list_for_surface': { commands: [] },
  'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
  'onboarding.status': { audioConfigured: false },
  'sandbox.run_mode.preference.get': { runMode: 'safe', source: 'config' },
  'sandbox.capability.status': { available: false },
  'usage.status': { sessions: [] },
}
