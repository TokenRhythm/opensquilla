import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import compactionEventSource from '../components/chat/CompactionEvent.vue?raw'
import chatViewSource from './ChatView.vue?raw'

const chatViewStyles = readFileSync(
  new URL('../styles/chat-view.css', import.meta.url),
  'utf8',
)

describe('compaction maintenance presentation', () => {
  it('renders one neutral transcript event without a fabricated progress surface', () => {
    expect(chatViewSource).toContain('class="chat-compaction-event"')
    expect(chatViewSource).toContain('data-testid="compaction-event"')
    expect(chatViewSource).toContain('data-placement="turn-boundary"')
    expect(chatViewSource).not.toContain('class="chat-compact-status"')
    expect(chatViewSource).not.toContain('role="progressbar"')
    expect(chatViewStyles).not.toContain('.chat-compact-status__gauge')
    expect(chatViewStyles).not.toContain('compactGaugeIndeterminate')
  })

  it('uses only a small running marker and respects reduced motion', () => {
    // The transcript event is rendered through ChatMessageList, outside
    // ChatView's scoped-style boundary, so the component must own these rules.
    expect(compactionEventSource).toContain('<style scoped>')
    expect(compactionEventSource).toMatch(
      /\.chat-compaction-event\s*\{[^}]*min-height:\s*1\.75rem[^}]*font-size:\s*var\(--fs-xs\)/s,
    )
    expect(compactionEventSource).toMatch(
      /\.chat-compaction-event--running \.chat-compaction-event__marker\s*\{[^}]*animation:\s*compactionEventSpin/s,
    )
    expect(compactionEventSource).toMatch(/prefers-reduced-motion:\s*reduce[\s\S]*chat-compaction-event--running[\s\S]*animation:\s*none/)
    expect(compactionEventSource).not.toMatch(/\.chat-compaction-event\s*\{[^}]*box-shadow/s)
  })
})
