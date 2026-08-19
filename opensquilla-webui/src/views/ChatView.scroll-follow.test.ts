import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

function threadScrollHandlerSource(): string {
  const start = chatViewSource.indexOf('function onThreadScroll()')
  const end = chatViewSource.indexOf('\nfunction onThreadWheel(', start)
  if (start < 0 || end < 0) throw new Error('Unable to locate ChatView thread scroll handler')
  return chatViewSource.slice(start, end)
}

function historyNavigationEndSource(): string {
  const start = chatViewSource.indexOf('function onHistoryNavigateEnd()')
  const end = chatViewSource.indexOf('\n// Show the jump-to-latest', start)
  if (start < 0 || end < 0) throw new Error('Unable to locate history navigation end handler')
  return chatViewSource.slice(start, end)
}

function threadWheelHandlerSource(): string {
  const start = chatViewSource.indexOf('function onThreadWheel(')
  const end = chatViewSource.indexOf('\nfunction threadConsumesWheel(', start)
  if (start < 0 || end < 0) throw new Error('Unable to locate ChatView thread wheel handler')
  return chatViewSource.slice(start, end)
}

describe('ChatView scroll ownership wiring', () => {
  it('removes stale reader intent from application-owned composer samples', () => {
    const source = threadScrollHandlerSource()

    expect(source).toContain(
      'const intent = programmatic ? null : currentThreadScrollIntent()',
    )
    expect(source).toContain('intent,')
    expect(source).not.toContain('intent: currentThreadScrollIntent()')
  })

  it('preserves reader pause when a history navigation is interrupted', () => {
    const scrollSource = threadScrollHandlerSource()
    const endSource = historyNavigationEndSource()
    const wheelSource = threadWheelHandlerSource()

    expect(scrollSource).toContain(
      'historyNavigationScrollLock.locked && intent !== null',
    )
    expect(scrollSource).toContain('interruptHistoryNavigationForReader()')
    expect(endSource).toContain(
      'const navigationInterrupted = historyNavigationScrollLock.finish()',
    )
    expect(endSource).toContain(
      'syncComposerRetractionFromThread(!navigationInterrupted)',
    )
    expect(wheelSource.indexOf('interruptHistoryNavigationForReader()')).toBeLessThan(
      wheelSource.indexOf('threadConsumesWheel(event, el)'),
    )
    expect(chatViewSource).toContain(
      'conversationMinimapRef.value?.cancelNavigation()',
    )
  })
})
