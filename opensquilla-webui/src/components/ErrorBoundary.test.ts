// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'

const mocks = vi.hoisted(() => ({
  copyText: vi.fn(),
  revealLog: vi.fn(),
  platform: {
    gateway: {} as { revealLog?: () => Promise<boolean> },
  },
  messages: {
    'errorBoundary.title': 'Something went wrong',
    'errorBoundary.defaultMessage': 'An unexpected error occurred.',
    'errorBoundary.reload': 'Reload page',
    'errorBoundary.dismiss': 'Dismiss',
    'errorBoundary.detailsLabel': 'Error details',
    'errorBoundary.copyDetails': 'Copy details',
    'errorBoundary.copied': 'Copied',
    'errorBoundary.openLogs': 'Open logs folder',
    'errorBoundary.privacyHint': 'Review before sharing.',
    'errorBoundary.copyFailed': 'Copy failed. Select the details and copy them manually.',
    'errorBoundary.openLogsFailed': 'Could not open the logs folder.',
  } as Record<string, string>,
}))

vi.mock('@/platform', () => ({
  usePlatform: () => mocks.platform,
}))

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: mocks.copyText,
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string) => mocks.messages[key] ?? key,
  }),
}))

import ErrorBoundary from './ErrorBoundary.vue'

const mountedApps: Array<{ app: App; el: HTMLElement }> = []

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
  await nextTick()
}

async function mountBoundary(error: Error) {
  const ThrowingChild = defineComponent({
    name: 'ThrowingChild',
    setup() {
      return () => {
        throw error
      }
    },
  })
  const Root = defineComponent({
    setup() {
      return () => h(ErrorBoundary, null, { default: () => h(ThrowingChild) })
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Root)
  app.mount(el)
  mountedApps.push({ app, el })
  await flush()
  return el
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  mocks.copyText.mockResolvedValue(undefined)
  mocks.revealLog.mockResolvedValue(true)
  mocks.platform.gateway = {}
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  while (mountedApps.length) {
    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()
  }
  vi.restoreAllMocks()
})

describe('ErrorBoundary', () => {
  it('shows a sanitized fallback and keeps the desktop-only action off the web', async () => {
    const error = new Error('Failed at /Users/alice/project?token=secret-value')
    error.stack = 'Error: Failed at /Users/alice/project?token=secret-value\n    at render (app.js:1)'
    const el = await mountBoundary(error)

    expect(el.textContent).toContain('Something went wrong')
    expect(el.textContent).toContain('/Users/[user]/project?token=[redacted]')
    expect(el.textContent).not.toContain('alice')
    expect(el.textContent).not.toContain('secret-value')
    expect(el.textContent).toContain('Review before sharing.')
    expect(el.querySelector('[data-testid="error-boundary-open-logs"]')).toBeNull()
    expect(console.error).toHaveBeenCalledWith(
      '[ErrorBoundary]',
      'Error: Failed at /Users/[user]/project?token=[redacted]\n    at render (app.js:1)',
    )
  })

  it('copies only sanitized details and confirms success', async () => {
    const error = new Error('API key api_key=sk-private')
    error.stack = 'Error: API key api_key=sk-private\n    at send (client.ts:4)'
    const el = await mountBoundary(error)

    el.querySelector<HTMLButtonElement>('[data-testid="error-boundary-copy"]')!.click()
    await flush()

    expect(mocks.copyText).toHaveBeenCalledWith(
      'Error: API key api_key=[redacted]\n    at send (client.ts:4)',
    )
    expect(el.querySelector('[data-testid="error-boundary-copy"]')?.textContent).toContain('Copied')
  })

  it('reports a clipboard failure without throwing out of the fallback', async () => {
    mocks.copyText.mockRejectedValue(new Error('permission denied'))
    const el = await mountBoundary(new Error('boom'))

    el.querySelector<HTMLButtonElement>('[data-testid="error-boundary-copy"]')!.click()
    await flush()

    expect(el.querySelector('[data-testid="error-boundary-action-error"]')?.textContent)
      .toContain('Copy failed')
  })

  it('offers desktop logs and reports a reveal failure inline', async () => {
    mocks.revealLog.mockRejectedValue(new Error('shell unavailable'))
    mocks.platform.gateway = { revealLog: mocks.revealLog }
    const el = await mountBoundary(new Error('boom'))

    el.querySelector<HTMLButtonElement>('[data-testid="error-boundary-open-logs"]')!.click()
    await flush()

    expect(mocks.revealLog).toHaveBeenCalledOnce()
    expect(el.querySelector('[data-testid="error-boundary-action-error"]')?.textContent)
      .toContain('Could not open the logs folder')
  })
})
