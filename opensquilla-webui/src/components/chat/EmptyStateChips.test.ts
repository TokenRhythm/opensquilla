// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import EmptyStateChips from './EmptyStateChips.vue'

vi.mock('@/composables/useRpc', () => ({
  useRpcCall: () => ({
    data: ref(null),
    loading: ref(false),
    error: ref(null),
    execute: vi.fn(),
  }),
}))

async function mountChips(props: {
  suppressed?: boolean
  disabled?: boolean
  onPick?: (text: string) => void
} = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(EmptyStateChips, {
    agentId: 'main',
    ...props,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('EmptyStateChips', () => {
  it('hides task actions while suppressed', async () => {
    const { app, el } = await mountChips({
      suppressed: true,
    })

    expect(el.querySelector('.empty-state__chips')).toBeNull()
    expect(el.querySelector('.empty-state__greeting')).not.toBeNull()
    app.unmount()
  })

  it('emits an ordinary suggestion when selected', async () => {
    const onPick = vi.fn()
    const { app, el } = await mountChips({
      onPick,
    })

    el.querySelector<HTMLButtonElement>('.empty-state__chip')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(onPick).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('uses the game label while emitting the full voxel-racing prompt', async () => {
    i18n.global.setLocaleMessage('zh-Hans', zhHans)
    i18n.global.locale.value = 'zh-Hans'
    const onPick = vi.fn()
    const { app, el } = await mountChips({ onPick })
    const game = [...el.querySelectorAll<HTMLButtonElement>('.empty-state__chip')]
      .find(button => button.textContent?.trim() === '帮我做个游戏')

    expect(game).toBeTruthy()
    game?.click()
    await nextTick()

    expect(onPick).toHaveBeenCalledWith(
      '构建一个可玩的浏览器端体素竞速游戏原型，包含响应式操控、加速/刹车、可读赛道、碰撞检测、检查点、圈速计时和重启流程。用代码生成体素风格的赛车和场景，不使用付费素材。运行游戏、测试控制、修复阻塞性问题后交付。我希望风景更好,开放世界。且地图在有opensquilla和 tokenrhythm这两个标识 是赞助商彩蛋',
    )
    app.unmount()
  })

  it('keeps the suggestion row visible but inert while disabled', async () => {
    const onPick = vi.fn()
    const { app, el } = await mountChips({
      disabled: true,
      onPick,
    })

    const task = el.querySelector<HTMLButtonElement>('.empty-state__chip')
    expect(task?.disabled).toBe(true)
    expect(el.querySelector('.empty-state__chips')).not.toBeNull()
    expect(el.querySelector('.empty-state__meta')).toBeNull()

    task?.click()
    await nextTick()
    expect(onPick).not.toHaveBeenCalled()
    app.unmount()
  })
})
