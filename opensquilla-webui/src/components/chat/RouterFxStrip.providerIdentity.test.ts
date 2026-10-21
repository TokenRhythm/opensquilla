// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import RouterFxStrip from './RouterFxStrip.vue'

afterEach(() => vi.unstubAllGlobals())

describe('router execution candidate identity', () => {
  it.each([false, true])('avoids guessing a provider for ambiguous execution evidence (history: %s)', async restored => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })))
    const message = reactive<ChatRenderedMessage>({
      id: 'synthetic-provider-identity', messageId: 'synthetic-provider-identity',
      role: 'router', displayRole: 'router', roleLabel: 'Router', text: '',
      timeStr: '', ts: null, showHeader: false, isRouterStrip: true,
      routerPanel: 'real-candidates', routerSource: 'classifier',
      routerStatic: restored, routerSettled: !restored,
      routerSelectedModel: 'primary-model', routerExecutionModel: 'shared-model', winnerIdx: 0,
      gridCells: [
        { kind: 'real', tier: 'c0', tiers: ['c0'], model: 'primary-model', displayName: 'primary-model' },
        { kind: 'real', tier: 'c1', tiers: ['c1'], provider: 'provider-a',
          model: 'shared-model', displayName: 'shared-model · provider-a' },
        { kind: 'real', tier: 'c2', tiers: ['c2'], provider: 'provider-b',
          model: 'shared-model', displayName: 'shared-model · provider-b' },
      ],
    })
    const element = document.createElement('div')
    const app = createApp(RouterFxStrip, { message })
    app.use(i18n)
    app.mount(element)
    try {
      await nextTick()
      expect(element.querySelector('.router-fx-cell.win')).toBeNull()
      expect(element.querySelector('[data-testid="router-execution-model"]')?.textContent)
        .toContain('shared-model')

      message.gridCells!.splice(2, 1)
      await nextTick()
      expect(element.querySelector('.router-fx-cell.win')?.getAttribute('data-cell-idx')).toBe('1')
    } finally {
      app.unmount()
    }
  })
})
