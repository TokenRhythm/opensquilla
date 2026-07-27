// @vitest-environment happy-dom
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import ReasoningPart from './ReasoningPart.vue'
import reasoningPartSource from './ReasoningPart.vue?raw'
import type { ChatPart } from '@/types/parts'

type ReasoningChatPart = Extract<ChatPart, { type: 'reasoning' }>

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        // No bare `thinking` key on purpose: the live label must come from the
        // fully localized `thinkingForSeconds` template, not concatenation —
        // regressing to `t('chat.thinking') + '· Ns'` would render the raw
        // missing-key path and fail the live-wording assertion below.
        thinkingForSeconds: 'Thinking · {seconds}s',
        thoughtProcess: 'Thought process',
        thoughtForSeconds: 'Thought for {seconds}s',
        thoughtForMinutes: 'Thought for {minutes}m {seconds}s',
      },
    },
  },
})

function part(text: string, seconds: number): ReasoningChatPart {
  return { type: 'reasoning', key: `reasoning:${seconds}`, text, seconds }
}

function mount(props: { part: ReasoningChatPart; embedded?: boolean; live?: boolean }) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ReasoningPart, props) })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ReasoningPart embedded body height cap', () => {
  it('caps the embedded body like the fold body so long traces scroll', () => {
    const selectorStart = reasoningPartSource.indexOf('.thinking-block__body {')
    const blockEnd = reasoningPartSource.indexOf('}', selectorStart)
    const rule = reasoningPartSource.slice(selectorStart, blockEnd)

    expect(selectorStart).toBeGreaterThanOrEqual(0)
    expect(rule).toContain('max-height: 16rem;')
    expect(rule).toContain('overflow-y: auto;')
    // Keep the embedded block flat — no card chrome around the trace.
    expect(rule).not.toContain('border')
    expect(rule).not.toContain('background')
  })
})

describe('ReasoningPart summary wording', () => {
  it('renders the settled wording for sub-second, seconds, and minutes', () => {
    expect(mount({ part: part('t', 0), embedded: true }).textContent)
      .toContain('Thought process')
    expect(mount({ part: part('t', 4), embedded: true }).textContent)
      .toContain('Thought for 4s')
    expect(mount({ part: part('t', 75), embedded: true }).textContent)
      .toContain('Thought for 1m 15s')
  })

  it('renders the live in-progress wording through the localized template', () => {
    const host = mount({ part: part('streaming trace', 4), live: true })
    const summary = host.querySelector('.thinking-fold__summary')
    expect(summary?.textContent).toContain('Thinking · 4s')
    expect(summary?.textContent).not.toContain('Thought for')
    // A concatenation fallback would surface the missing-key name instead.
    expect(summary?.textContent).not.toContain('chat.thinking')
  })
})

describe('ReasoningPart nested-in-activity variant', () => {
  it('marks the live fold as in-activity so the doubled left rule is dropped', () => {
    const host = mount({ part: part('t', 4), live: true })
    expect(host.querySelector('details.thinking-fold--in-activity')).not.toBeNull()
  })

  it('keeps the standalone compat fold free of the in-activity modifier', () => {
    const host = mount({ part: part('t', 4) })
    expect(host.querySelector('details.thinking-fold')).not.toBeNull()
    expect(host.querySelector('.thinking-fold--in-activity')).toBeNull()
  })

  it('suppresses only the inner rule and its indent in the nested variant', () => {
    const selectorStart = reasoningPartSource.indexOf('.thinking-fold--in-activity > .thinking-fold__body {')
    const blockEnd = reasoningPartSource.indexOf('}', selectorStart)
    const rule = reasoningPartSource.slice(selectorStart, blockEnd)

    expect(selectorStart).toBeGreaterThanOrEqual(0)
    expect(rule).toContain('border-left: none;')
    expect(rule).toContain('padding-left: 0;')
    // The standalone fold body must keep its own 2px rule untouched.
    const baseStart = reasoningPartSource.indexOf('.thinking-fold__body {')
    const baseRule = reasoningPartSource.slice(baseStart, reasoningPartSource.indexOf('}', baseStart))
    expect(baseRule).toContain('border-left: 2px solid var(--border);')
  })
})

describe('ReasoningPart branches', () => {
  it('renders a flat block with the trace body when embedded', () => {
    const host = mount({ part: part('embedded trace', 4), embedded: true })
    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.thinking-block__body')?.textContent)
      .toBe('embedded trace')
  })

  it('renders a self-sufficient details fold when not embedded', () => {
    const host = mount({ part: part('fold trace', 4) })
    const fold = host.querySelector<HTMLDetailsElement>('details.thinking-fold')
    expect(fold).not.toBeNull()
    expect(fold?.open).toBe(false)
    expect(fold?.querySelector('.thinking-fold__chevron')).not.toBeNull()
    expect(fold?.querySelector('.thinking-fold__body')?.textContent)
      .toBe('fold trace')
  })
})
