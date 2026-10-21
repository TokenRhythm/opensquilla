import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'

const URL = '/e2e/fixtures/notification-feedback.html'
const themes = ['light', 'dark', 'arctic', 'crt-green', 'ember', 'miami', 'synthwave', 'terminal', 'vapor']
type Api = {
  clear(): void
  setTheme(id: string): Promise<void>
  showAction(mode?: 'success' | 'pending' | 'reject' | 'stale', duration?: number): void
  notify(message: string, tone?: 'info' | 'ok' | 'warn' | 'danger', duration?: number, dedupeKey?: string): void
  runOrder(order: 'rpc-first' | 'event-first', success?: boolean): Promise<string>
  runReminderOrder(order: 'rpc-first' | 'event-first'): Promise<{ runId: string; countBeforeEvent: number }>
  setMemberReadFailure(value: boolean): void
  emitFinished(event: Record<string, unknown>): string
  resolveAction(ok?: boolean): void
  rejectAction(): void
  snapshot(): { calls: { actions: number; approve: number; setAdmin: number }; destination: string; pairingApproved: boolean; adminGranted: boolean }
}
declare global { interface Window { notificationFeedback: Api; notificationSetKeyboard?(open: boolean): void } }
const pageErrors = new WeakMap<Page, string[]>()
test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  pageErrors.set(page, errors)
  page.on('pageerror', error => errors.push(error.message))
  // The component fixture has no Gateway. Prevent a parallel source edit's
  // Vite HMR reload from replacing the page during fake-clock interaction.
  await page.routeWebSocket(/.*/, () => {})
})
test.afterEach(({ page }) => {
  expect(pageErrors.get(page), 'uncaught browser errors').toEqual([])
})

async function open(page: Page, theme = 'light') {
  await page.goto(`${URL}?theme=${theme}`)
  await expect(page.getByRole('heading', { name: '通知与操作反馈 · 正式组件验收' })).toBeVisible()
  await page.waitForFunction(() => Boolean(window.notificationFeedback))
}

test('ordinary cron completion stays silent and both actual RPC/event orderings notify once', async ({ page }) => {
  await open(page)
  await page.getByRole('button', { name: '普通完成', exact: true }).click()
  await expect(page.getByTestId('toast')).toHaveCount(0)
  for (const order of ['rpc-first', 'event-first'] as const) {
    const runId = await page.evaluate(order => window.notificationFeedback.runOrder(order), order)
    await expect(page.getByTestId('toast')).toHaveCount(1)
    await expect(page.getByTestId('toast')).toHaveClass(/toast--danger/)
    await page.evaluate(runId => window.notificationFeedback.emitFinished({ runId, jobId: 'job-fixture', success: false }), runId)
    await expect(page.getByTestId('toast')).toHaveCount(1)
    await page.locator('.toast__action').click()
    await expect(page.getByTestId('fixture-destination')).toHaveText('job:job-fixture')
    await expect(page.getByTestId('toast')).toHaveCount(0)
  }
  await page.evaluate(() => window.notificationFeedback.runOrder('rpc-first', true))
  await expect(page.getByTestId('toast')).toHaveCount(0)
})

test('due reminders preserve their target and do not replay after manual dismissal', async ({ page }) => {
  await open(page)
  await page.getByRole('button', { name: '到点提醒', exact: true }).click()
  await expect(page.getByTestId('toast')).toContainText('请参加项目会议')
  await expect(page.getByTestId('toast')).toHaveClass(/toast--info/)
  await page.locator('.toast__action').click()
  await expect(page.getByTestId('fixture-destination')).toHaveText('session:fixture-reminder')
  await expect(page.getByTestId('toast')).toHaveCount(0)
  const event = { runId: 'dismissed-reminder', jobId: 'job-fixture', payloadKind: 'reminder', success: true }
  await page.evaluate(event => window.notificationFeedback.emitFinished(event), event)
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await page.locator('.toast__dismiss').click()
  await expect(page.getByTestId('toast')).toHaveCount(0)
  expect(await page.evaluate(event => window.notificationFeedback.emitFinished(event), event)).toBe('duplicate')
  await expect(page.getByTestId('toast')).toHaveCount(0)
})

test('manual reminder RPC never consumes the richer event reminder or its session target', async ({ page }) => {
  await open(page)
  for (const order of ['rpc-first', 'event-first'] as const) {
    const result = await page.evaluate(order => window.notificationFeedback.runReminderOrder(order), order)
    expect(result.countBeforeEvent).toBe(order === 'rpc-first' ? 0 : 1)
    await expect(page.getByTestId('toast')).toHaveCount(1)
    await expect(page.getByTestId('toast')).toContainText('请参加项目会议')
    await page.locator('.toast__action').click()
    await expect(page.getByTestId('fixture-destination')).toHaveText('session:fixture-reminder')
    await expect(page.getByTestId('toast')).toHaveCount(0)
  }
})

test('a visible failed run stays inline while an offscreen failed run notifies', async ({ page }) => {
  await open(page)
  await page.evaluate(() => {
    const entry = document.createElement('div')
    entry.dataset.cronFailedRun = 'visible-failure'
    entry.textContent = 'Existing failure card'
    document.querySelector('main')!.prepend(entry)
  })
  const result = await page.evaluate(() => window.notificationFeedback.emitFinished({ runId: 'visible-failure', jobId: 'job-fixture', success: false }))
  expect(result).toBe('inline')
  await expect(page.getByTestId('toast')).toHaveCount(0)
  await page.evaluate(() => {
    const entry = document.querySelector<HTMLElement>('[data-cron-failed-run]')!
    entry.dataset.cronFailedRun = 'offscreen-failure'
    entry.style.position = 'absolute'
    entry.style.top = '5000px'
  })
  expect(await page.evaluate(() => window.notificationFeedback.emitFinished({ runId: 'offscreen-failure', jobId: 'job-fixture', success: false }))).toBe('notified')
  await expect(page.getByTestId('toast')).toHaveCount(1)
})

test('pending recovery has one action, keeps its card, and dismisses only on success', async ({ page }) => {
  await open(page)
  await page.evaluate(() => window.notificationFeedback.showAction('pending'))
  await page.locator('.toast__action').dblclick()
  await expect(page.getByTestId('toast')).toHaveAttribute('aria-busy', 'true')
  await expect(page.locator('.toast__action')).toHaveAttribute('aria-disabled', 'true')
  expect(await page.evaluate(() => window.notificationFeedback.snapshot().calls.actions)).toBe(1)
  await page.evaluate(() => window.notificationFeedback.resolveAction())
  await expect(page.getByTestId('toast')).toHaveCount(0)
})

test('rejected and stale recovery remain explicit instead of disappearing', async ({ page }) => {
  await open(page)
  await page.evaluate(() => window.notificationFeedback.showAction('pending'))
  await page.locator('.toast__action').click()
  await page.evaluate(() => window.notificationFeedback.rejectAction())
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await expect(page.getByTestId('toast')).toHaveClass(/toast--danger/)
  await expect(page.locator('.toast__action')).toHaveText('重试')
  await page.evaluate(() => window.notificationFeedback.clear())
  await expect(page.getByTestId('toast')).toHaveCount(0)
  await page.evaluate(() => window.notificationFeedback.showAction('stale'))
  await page.locator('.toast__action').click()
  await expect(page.getByTestId('toast')).toContainText('草稿已变化，请从输入框继续。')
  await expect(page.getByTestId('toast')).toHaveClass(/toast--info/)
  await expect(page.locator('.toast__action')).toHaveCount(0)
})

test('hover and keyboard focus pause independent remaining-time clocks', async ({ page }) => {
  await open(page)
  await page.clock.install()
  await page.evaluate(() => window.notificationFeedback.showAction('success', 3000))
  await page.clock.runFor(500)
  await page.getByTestId('toast').hover()
  await page.locator('.toast__action').focus()
  await page.clock.runFor(6000)
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await page.mouse.move(1, 1)
  await page.keyboard.press('Tab')
  await expect(page.locator('.toast__dismiss')).toBeFocused()
  await page.clock.runFor(4000)
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await page.getByRole('button', { name: '普通完成', exact: true }).focus()
  await page.clock.runFor(2200)
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await page.clock.runFor(600)
  await expect(page.getByTestId('toast')).toHaveCount(0)
})

test('same-key updates preserve original lifetime and ordinary notices cannot evict recovery', async ({ page }) => {
  await open(page)
  await page.clock.install()
  await page.evaluate(() => window.notificationFeedback.notify('初次通知', 'info', 3000, 'same-key'))
  await page.clock.runFor(2000)
  await page.evaluate(() => window.notificationFeedback.notify('同一操作更新', 'info', 3000, 'same-key'))
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await page.clock.runFor(1400)
  await expect(page.getByTestId('toast')).toHaveCount(0)
  await page.evaluate(() => {
    window.notificationFeedback.showAction('success')
    for (let i = 0; i < 5; i++) window.notificationFeedback.notify(`常规消息 ${i}`, 'ok')
  })
  await expect(page.getByTestId('toast')).toHaveCount(4)
  await expect(page.locator('.toast__action')).toHaveCount(1)
  await expect(page.getByTestId('toast').filter({ hasText: '常规消息 0' })).toHaveCount(0)
})

test('real member approval produces one partial-success warning and retry grants only admin', async ({ page }) => {
  await open(page)
  await page.getByRole('button', { name: '批准 验收成员 的访问权限', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.locator('.modal__primary').click()
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await expect(page.getByTestId('toast')).toHaveClass(/toast--warn/)
  await expect(page.getByTestId('toast')).toContainText('管理员授权失败')
  const before = await page.evaluate(() => window.notificationFeedback.snapshot())
  expect(before.calls.approve).toBe(1)
  expect(before.pairingApproved).toBe(true)
  expect(before.adminGranted).toBe(false)
  await page.getByRole('button', { name: '将 验收成员 设为渠道管理员', exact: true }).click()
  await page.locator('.modal__primary').click()
  await expect(page.locator('.ch-pairing-status')).toHaveClass(/is-admin/)
  const after = await page.evaluate(() => window.notificationFeedback.snapshot())
  expect(after.calls.approve).toBe(1)
  expect(after.calls.setAdmin).toBe(1)
})

test('failed readback keeps the approved member and the existing admin recovery action', async ({ page }) => {
  await open(page)
  await page.evaluate(() => window.notificationFeedback.setMemberReadFailure(true))
  await page.getByRole('button', { name: '批准 验收成员 的访问权限', exact: true }).click()
  await page.locator('.modal__primary').click()
  await expect(page.getByTestId('toast')).toHaveCount(1)
  await expect(page.getByTestId('toast')).toHaveClass(/toast--warn/)
  await expect(page.locator('.ch-pairing-state.is-error')).toBeVisible()
  await expect(page.locator('.ch-pairing-status')).toHaveClass(/is-approved/)
  const action = page.getByRole('button', { name: '将 验收成员 设为渠道管理员', exact: true })
  await expect(action).toBeVisible()
  await page.evaluate(() => window.notificationFeedback.setMemberReadFailure(false))
  await action.click()
  await page.locator('.modal__primary').click()
  await expect(page.locator('.ch-pairing-status')).toHaveClass(/is-admin/)
  const state = await page.evaluate(() => window.notificationFeedback.snapshot())
  expect(state.calls.approve).toBe(1)
  expect(state.calls.setAdmin).toBe(1)
})

for (const theme of themes) {
  for (const width of [1440, 360]) {
    test(`${theme} at ${width}px retains readable actions and stays within viewport`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 960 })
      await open(page, theme)
      await page.evaluate(() => {
        window.notificationFeedback.showAction('success', 60000)
        window.notificationFeedback.notify('已批准成员，但管理员授权失败，请在成员列表中重试。', 'warn', 60000)
        window.notificationFeedback.notify('项目提醒：请参加会议。', 'info', 60000)
      })
      await expect(page.getByTestId('toast')).toHaveCount(3)
      await expect(page.getByTestId('toast-host')).toHaveAttribute('aria-live', 'polite')
      const surfaces = await page.getByTestId('toast').evaluateAll(elements => elements.map(element => {
        const style = getComputedStyle(element)
        return { background: style.backgroundColor, border: style.borderColor, fontSize: style.fontSize, filter: style.backdropFilter, stripe: getComputedStyle(element, '::before').backgroundColor }
      }))
      expect(new Set(surfaces.map(item => item.background)).size).toBe(1)
      expect(new Set(surfaces.map(item => item.border)).size).toBe(1)
      expect(new Set(surfaces.map(item => item.stripe)).size).toBe(3)
      expect(surfaces.every(item => item.fontSize === '14px' && item.filter === 'none')).toBe(true)
      const boxes = await page.getByTestId('toast').evaluateAll(elements => elements.map(element => {
        const rect = element.getBoundingClientRect()
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, scroll: element.scrollWidth, width: element.clientWidth }
      }))
      for (const box of boxes) {
        expect(box.left).toBeGreaterThanOrEqual(0)
        expect(box.right).toBeLessThanOrEqual(width)
        expect(box.top).toBeGreaterThanOrEqual(0)
        expect(box.bottom).toBeLessThanOrEqual(960)
        expect(box.scroll).toBeLessThanOrEqual(box.width)
      }
      const action = page.locator('.toast__action')
      expect(await action.evaluate(element => getComputedStyle(element).fontWeight)).toBe('600')
      const actionBox = await action.boundingBox()
      expect(actionBox!.height).toBeGreaterThanOrEqual(32)
      expect(actionBox!.width).toBeGreaterThanOrEqual(32)
      await action.focus()
      await expect(action).toBeFocused()
      expect(await action.evaluate(element => getComputedStyle(element).boxShadow)).not.toBe('none')
      if (['light', 'dark', 'miami'].includes(theme)) {
        await page.screenshot({ path: testInfo.outputPath(`notification-${theme}-${width}.png`), fullPage: true })
        await page.getByTestId('toast-host').screenshot({ path: testInfo.outputPath(`notification-cards-${theme}-${width}.png`) })
      }
    })
  }
}

test('reduced motion removes enter leave and move transitions without removing recovery', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await open(page, 'miami')
  await page.evaluate(() => window.notificationFeedback.showAction())
  await expect(page.getByTestId('toast')).toHaveCount(1)
  const durations = await page.getByTestId('toast').evaluate(element => ['toast-stack-enter-active', 'toast-stack-leave-active', 'toast-stack-move'].map(name => {
    element.classList.add(name)
    const duration = getComputedStyle(element).transitionDuration
    element.classList.remove(name)
    return duration
  }))
  // The shared reduced-motion reset uses 0.01ms !important; it is effectively
  // instant and may override the component's literal transition:none.
  expect(durations.every(duration => duration.split(',').every(value => Number.parseFloat(value) <= 0.001))).toBe(true)
  await page.locator('.toast__action').click()
  await expect(page.getByTestId('toast')).toHaveCount(0)
})

test('pending action retains keyboard focus and stale action becomes a neutral notice', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 360, height: 800 })
  await open(page, 'miami')
  await page.evaluate(() => window.notificationFeedback.showAction('pending'))
  const action = page.locator('.toast__action')
  await action.focus()
  await page.keyboard.press('Enter')
  await expect(action).toBeFocused()
  await expect(action).toHaveAttribute('aria-disabled', 'true')
  await expect(page.getByTestId('toast')).toHaveAttribute('aria-busy', 'true')
  await page.getByTestId('toast-host').screenshot({ path: testInfo.outputPath('notification-pending-focus-miami.png') })
  await page.evaluate(() => window.notificationFeedback.resolveAction())
  await expect(page.getByTestId('toast')).toHaveCount(0)
  await page.evaluate(() => window.notificationFeedback.showAction('stale'))
  await page.locator('.toast__action').click()
  await expect(page.getByTestId('toast')).toHaveClass(/toast--info/)
  await expect(page.locator('.toast__action')).toHaveCount(0)
  await page.getByTestId('toast-host').screenshot({ path: testInfo.outputPath('notification-stale-info-miami.png') })
})

test('a tall four-notice stack keeps lower controls free and its last dismiss keyboard reachable', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 640 })
  await open(page, 'dark')
  await page.evaluate(() => {
    for (let index = 0; index < 4; index++) {
      window.notificationFeedback.notify(`${index}: ${'Long notification with recovery details. '.repeat(6)}`, 'warn', 60000)
    }
  })
  const host = page.getByTestId('toast-host')
  await expect(page.getByTestId('toast')).toHaveCount(4)
  const bounds = await host.boundingBox()
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(320)
  expect(await host.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
  const dismiss = page.locator('.toast__dismiss').last()
  await dismiss.focus()
  await expect(dismiss).toBeFocused()
  expect(await dismiss.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return hit === element || element.contains(hit)
  })).toBe(true)
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('toast')).toHaveCount(3)
})

test('the final notice stays inside the bounded stack during its exit animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await open(page, 'light')
  await page.evaluate(() => window.notificationFeedback.notify('Last notice exit animation', 'info', 60000))
  const toast = page.getByTestId('toast')
  await expect(toast).toBeVisible()
  await toast.evaluate(async element => {
    await Promise.all(element.getAnimations().map(animation => animation.finished))
  })
  const exiting = await page.locator('.toast__dismiss').evaluate(button => {
    ;(button as HTMLButtonElement).click()
    return new Promise<{ leaving: boolean; contained: boolean }>(resolve => requestAnimationFrame(() => {
      const element = document.querySelector('.toast-stack-leave-active')
      const row = element?.getBoundingClientRect()
      const stack = element?.parentElement?.getBoundingClientRect()
      resolve({ leaving: Boolean(element), contained: Boolean(row && stack && row.top >= stack.top && row.bottom <= stack.bottom) })
    }))
  })
  expect(exiting).toEqual({ leaving: true, contained: true })
  await expect(toast).toHaveCount(0)
})

test('real App keeps notices above the composer while the 360px keyboard and tab bar change', async ({ page }, testInfo) => {
  // Actual App, router, composer, ToastHost and bottom navigation. Gateway
  // traffic is synthetic; the viewport signal represents IME open/close,
  // without claiming a physical mobile keyboard or installed-client proof.
  await page.setViewportSize({ width: 360, height: 800 })
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-theme', 'light')
    Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
    let keyboardOpen = false
    if (window.visualViewport) {
      Object.defineProperty(window.visualViewport, 'height', { configurable: true, get: () => window.innerHeight - (keyboardOpen ? 300 : 0) })
    }
    window.notificationSetKeyboard = (open: boolean) => {
      keyboardOpen = open
      window.visualViewport?.dispatchEvent(new Event('resize'))
    }
  })
  // Keep even supplementary App HTTP reads inside the offline boundary.
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await installSidebarFixture(page, { 'onboarding.status': { audioConfigured: true } })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'zh-Hans'))
  await page.goto('/control/chat/new')
  await expect(page.locator('.chat-textarea')).toBeVisible()
  await page.getByRole('button', { name: '添加', exact: true }).click()
  await page.getByRole('menuitem', { name: '录制语音输入', exact: true }).click()
  const toast = page.getByTestId('toast')
  const host = page.getByTestId('toast-host')
  const tabbar = page.locator('.mobile-tabbar')
  await expect(toast).toContainText('此浏览器不支持语音输入')
  await page.locator('.toast__dismiss').focus()
  await expect(host).toHaveAttribute('aria-live', 'polite')
  const initialTop = await host.evaluate(element => element.getBoundingClientRect().top)
  expect(initialTop).toBeGreaterThanOrEqual(56)
  const tabbarBox = await tabbar.boundingBox()
  const toastBox = await toast.boundingBox()
  expect(toastBox!.y + toastBox!.height).toBeLessThanOrEqual(tabbarBox!.y - 8)
  await page.screenshot({ path: testInfo.outputPath('notification-real-app-360-tabbar.png') })
  await page.evaluate(() => window.notificationSetKeyboard?.(true))
  await expect(tabbar).toHaveClass(/is-keyboard-open/)
  await expect.poll(() => host.evaluate(element => element.getBoundingClientRect().top)).toBe(initialTop)
  await expect(page.locator('.toast__dismiss')).toBeFocused()
  await expect.poll(() => tabbar.evaluate(element => element.getBoundingClientRect().top)).toBeGreaterThanOrEqual(800)
  await page.screenshot({ path: testInfo.outputPath('notification-real-app-360-keyboard-state.png') })
  await page.evaluate(() => window.notificationSetKeyboard?.(false))
  await expect(tabbar).not.toHaveClass(/is-keyboard-open/)
  await expect.poll(() => host.evaluate(element => element.getBoundingClientRect().top)).toBe(initialTop)
  const composerBox = await page.locator('.chat-composer').boundingBox()
  expect((await toast.boundingBox())!.y + (await toast.boundingBox())!.height).toBeLessThan(composerBox!.y)
  await expect(page.locator('.toast__dismiss')).toBeFocused()
})
