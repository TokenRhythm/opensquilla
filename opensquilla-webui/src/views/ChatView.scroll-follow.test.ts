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
  it('invalidates deferred work and pins a switched session only once', () => {
    expect(chatViewSource).toContain('const scrollEpoch = ref(0)')
    expect(chatViewSource).toContain('watch(sessionKey, beginSessionScrollEpoch, { flush: \'sync\' })')
    expect(chatViewSource).toContain('clearProgrammaticScroll(threadRef.value)')
    expect(chatViewSource).toContain('scroll-epoch="scrollEpoch"')
    expect(chatViewSource).toContain('scheduleInitialSessionPin(scrollEpoch.value)')
    expect(chatViewSource).toContain('sessionScrollInputEpoch === epoch')
  })

  it('cancels pending layout pins for explicit reader input', () => {
    expect(chatViewSource).toContain('@touchcancel.passive="onThreadTouchEnd"')
    expect(chatViewSource).toContain('@pointercancel="onThreadPointerEnd"')
    expect(threadWheelHandlerSource()).toContain('cancelTailLayoutPin()')
    expect(threadScrollHandlerSource()).toContain('if (!scrollMutation?.matched) cancelTailLayoutPin()')
  })

  it('pauses follow after a source-less native scroll during session landing', () => {
    const source = threadScrollHandlerSource()

    expect(source).toContain('const gap = metrics.height - metrics.top - metrics.clientHeight')
    expect(source).toContain('readerMovingAway = true')
    expect(source).toContain('autoScroll.value = false')
  })

  it('marks questionnaire edge handoff as reader input before writing scrollTop', () => {
    const start = chatViewSource.indexOf('function handlePlanQuestionnaireWheel(')
    const end = chatViewSource.indexOf('\nfunction onPlanQuestionnaireTouchStart(', start)
    const source = chatViewSource.slice(start, end)

    expect(source).toContain('markThreadScrollIntent(direction)')
    expect(source.indexOf('markThreadScrollIntent(direction)')).toBeLessThan(
      source.indexOf('handoffPlanQuestionnaireWheel(event, thread)'),
    )
    // The touch helper may call preventDefault when transferring a sibling
    // gesture, so this listener must remain non-passive.
    expect(chatViewSource).toContain('@touchmove="onPlanQuestionnaireTouchMove"')
    expect(chatViewSource).not.toContain('@touchmove.passive="onPlanQuestionnaireTouchMove"')
  })

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
      'if (!programmatic && historyNavigationScrollLock.locked)',
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

  it('cancels navigation for source-less native scrollbar movement', () => {
    const source = threadScrollHandlerSource()

    expect(chatViewSource).toContain('sourceLessScrollPointerId')
    expect(source).toContain('sourceLessScrollPointerId !== null && moved')
    expect(chatViewSource).toContain("window.addEventListener('pointerup', onThreadPointerEnd)")
    expect(chatViewSource).toContain("window.removeEventListener('pointerup', onThreadPointerEnd)")
  })

  it('does not steal modified keyboard shortcuts or IME composition', () => {
    expect(chatViewSource).toContain(
      'if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return',
    )
  })
})
