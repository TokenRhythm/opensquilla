import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

describe('ChatView Meta setup and Goal draft precedence', () => {
  it('disarms Goal draft mode before restoring a Meta launch into the composer', () => {
    const restoreStart = chatViewSource.indexOf('function restoreMetaLaunchDraft(')
    const restoreEnd = chatViewSource.indexOf('\nfunction restoreDeferredMetaDrafts(', restoreStart)
    const restoreSource = chatViewSource.slice(restoreStart, restoreEnd)
    const foreignSessionReturn = restoreSource.indexOf('\n    return\n  }')
    const disarmGoal = restoreSource.indexOf('disarmGoalDraftForMetaRestore()')

    expect(restoreStart).toBeGreaterThanOrEqual(0)
    expect(restoreEnd).toBeGreaterThan(restoreStart)
    expect(foreignSessionReturn).toBeGreaterThanOrEqual(0)
    expect(chatViewSource).toContain('disarmGoalDraftForMetaRestore = disarmGoalMode')
    expect(disarmGoal).toBeGreaterThan(foreignSessionReturn)
    expect(disarmGoal).toBeLessThan(restoreSource.indexOf('const currentDraft'))
    expect(disarmGoal).toBeLessThan(restoreSource.indexOf('inputText.value = restored'))
  })
})
