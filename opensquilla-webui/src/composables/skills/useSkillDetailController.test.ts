// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h } from 'vue'
import type { Skill, SkillDependencyInstallOutcome } from '@/types/skills'
import {
  useSkillDetailController,
  type SkillDetailController,
} from './useSkillDetailController'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function missingBinaryDetail(name: string): Skill {
  return {
    name,
    status: 'needs_setup',
    missing_bins: ['ffmpeg'],
    install: [{ id: 'ffmpeg', kind: 'brew', bins: ['ffmpeg'] }],
    content: `# ${name}`,
  }
}

function completeOutcome(): SkillDependencyInstallOutcome {
  return {
    success: true,
    complete: true,
    message: 'installed',
    missingStill: { bins: [], env: [], env_any: [] },
  }
}

function mountController(options: Parameters<typeof useSkillDetailController>[0]) {
  let controller!: SkillDetailController
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(defineComponent({
    setup() {
      controller = useSkillDetailController(options)
      return () => h('div')
    },
  }))
  app.mount(host)
  return { controller, unmount: () => app.unmount() }
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useSkillDetailController', () => {
  it('ignores an older skills.get response after another skill is opened', async () => {
    const first = deferred<Skill>()
    const second = deferred<Skill>()
    const call = vi.fn((_method: string, params?: Record<string, unknown>) => {
      return params?.name === 'first' ? first.promise : second.promise
    })
    const installDeps = vi.fn(async () => completeOutcome())
    const { controller, unmount } = mountController({ rpc: { call }, installDeps })

    const openFirst = controller.openSkill({ name: 'first' })
    const openSecond = controller.openSkill({ name: 'second' })
    second.resolve({ name: 'second', status: 'ready', content: '# second' })
    await openSecond
    first.resolve({ name: 'first', status: 'ready', content: '# first' })
    await openFirst

    expect(controller.selectedSkill.value?.name).toBe('second')
    expect(controller.selectedSkill.value?.content).toBe('# second')
    unmount()
  })

  it('revalidates the action with the latest skills.get before installing', async () => {
    let read = 0
    const call = vi.fn(async () => {
      read += 1
      return read === 1
        ? missingBinaryDetail('render')
        : { name: 'render', status: 'ready', install: [] }
    })
    const installDeps = vi.fn(async () => completeOutcome())
    const { controller, unmount } = mountController({ rpc: { call }, installDeps })

    await controller.openSkill(missingBinaryDetail('render'))
    const installed = await controller.installCurrentDependencies('render', 'ffmpeg')

    expect(installed).toBe(false)
    expect(installDeps).not.toHaveBeenCalled()
    expect(controller.selectedSkillError.value).not.toBe('')
    unmount()
  })

  it('keeps the dialog open when envAny remains after a successful installer run', async () => {
    const stillMissing: Skill = {
      ...missingBinaryDetail('render'),
      missing_bins: [],
      missing_env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
    }
    let read = 0
    const call = vi.fn(async () => {
      read += 1
      if (read < 3) return missingBinaryDetail('render')
      return stillMissing
    })
    const installDeps = vi.fn(async (): Promise<SkillDependencyInstallOutcome> => ({
      success: true,
      complete: false,
      message: 'binary installed',
      missingStill: {
        bins: [],
        env: [],
        env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
      },
    }))
    const { controller, unmount } = mountController({ rpc: { call }, installDeps })

    await controller.openSkill(missingBinaryDetail('render'))
    const installed = await controller.installCurrentDependencies('render', 'ffmpeg')

    expect(installed).toBe(false)
    expect(controller.selectedSkill.value?.name).toBe('render')
    expect(controller.selectedSkill.value?.dependency_summary?.missing.api_env.any)
      .toEqual([['OPENROUTER_API_KEY', 'ARK_API_KEY']])
    expect(controller.installFeedback.value).not.toBe('')
    unmount()
  })

  it('binds delayed close to the installed skill and cannot close a newer dialog', async () => {
    vi.useFakeTimers()
    const reads = new Map<string, number>()
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      const name = String(params?.name || '')
      const count = (reads.get(name) || 0) + 1
      reads.set(name, count)
      if (name === 'first' && count >= 3) return { name, status: 'ready', install: [] }
      return missingBinaryDetail(name)
    })
    const installDeps = vi.fn(async () => completeOutcome())
    const { controller, unmount } = mountController({
      rpc: { call },
      installDeps,
      closeDelayMs: 600,
    })

    await controller.openSkill(missingBinaryDetail('first'))
    expect(await controller.installCurrentDependencies('first', 'ffmpeg')).toBe(true)
    await controller.openSkill(missingBinaryDetail('second'))
    await vi.advanceTimersByTimeAsync(600)

    expect(controller.selectedSkill.value?.name).toBe('second')
    unmount()
  })
})
