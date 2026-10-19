// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import ProjectWorkspaceCreateDialog from './ProjectWorkspaceCreateDialog.vue'
import en from '@/locales/en.json'
import type { ProjectWorkspaceMode } from '@/utils/projectWorkspaceCreation'

const mountedApps: App<Element>[] = []

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: { en },
  })
}

async function mountDialog(options: {
  name?: string
  sourcePath?: string
  busy?: boolean
  sourcePicking?: boolean
  mode?: ProjectWorkspaceMode
  creationAvailable?: boolean
  hostPlatform?: 'windows' | 'posix'
  error?: string
} = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const name = ref(options.name || '')
  const mode = ref<ProjectWorkspaceMode>(options.mode || 'create')
  const chooseSource = vi.fn()
  const create = vi.fn()
  const close = vi.fn()
  const Root = defineComponent(() => () => h(ProjectWorkspaceCreateDialog, {
    open: true,
    mode: mode.value,
    name: name.value,
    sourcePath: options.sourcePath || '',
    busy: options.busy || false,
    sourcePicking: options.sourcePicking || false,
    creationAvailable: options.creationAvailable ?? true,
    hostName: 'project-host',
    hostPlatform: options.hostPlatform || 'posix',
    error: options.error,
    'onUpdate:name': (value: string) => { name.value = value },
    'onUpdate:mode': (value: ProjectWorkspaceMode) => { mode.value = value },
    onChooseSource: chooseSource,
    onCreate: create,
    onClose: close,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  return { name, mode, chooseSource, create, close }
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ProjectWorkspaceCreateDialog', () => {
  it('shows the final new directory and execution host before creating', async () => {
    const events = await mountDialog({
      name: 'Demo',
      sourcePath: '/repos',
    })
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const source = dialog?.querySelector<HTMLButtonElement>('.project-create__source-picker')
    const create = dialog?.querySelector<HTMLButtonElement>('button[type="submit"]')

    expect(dialog?.textContent).toContain('Save in')
    expect(dialog?.textContent).toContain('/repos/Demo')
    expect(dialog?.textContent).toContain('Computer: project-host')
    expect(create?.disabled).toBe(false)

    source?.click()
    create?.click()
    await nextTick()

    expect(events.chooseSource).toHaveBeenCalledOnce()
    expect(events.create).toHaveBeenCalledWith({ mode: 'create', name: 'Demo', path: '/repos' })
  })

  it('keeps creation disabled until both fields are present', async () => {
    await mountDialog()
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const create = dialog?.querySelector<HTMLButtonElement>('button[type="submit"]')

    expect(create?.disabled).toBe(true)
  })

  it('prevents duplicate folder picks and creation while the system picker is open', async () => {
    const events = await mountDialog({
      name: 'Demo',
      sourcePath: '/repos/demo',
      sourcePicking: true,
    })
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const source = dialog?.querySelector<HTMLButtonElement>('.project-create__source-picker')
    const create = dialog?.querySelector<HTMLButtonElement>('button[type="submit"]')

    expect(source?.disabled).toBe(true)
    expect(create?.disabled).toBe(true)

    source?.click()
    create?.click()
    await nextTick()

    expect(events.chooseSource).not.toHaveBeenCalled()
    expect(events.create).not.toHaveBeenCalled()
  })

  it('opens an existing directory without a project name on an older Gateway', async () => {
    const events = await mountDialog({
      mode: 'open', sourcePath: '/repos/existing', creationAvailable: false,
    })
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    const modes = dialog.querySelectorAll<HTMLButtonElement>('.project-create__modes button')
    const submit = dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!

    expect(modes[0].disabled).toBe(false)
    expect(modes[1].disabled).toBe(true)
    expect(dialog.querySelector('input')).toBeNull()
    expect(dialog.textContent).toContain(en.workspaces.createUnavailable)
    expect(submit.disabled).toBe(false)
    submit.click()
    await nextTick()
    expect(events.create).toHaveBeenCalledWith({ mode: 'open', name: '', path: '/repos/existing' })
  })

  it('uses the host platform for the final path and prevents multi-segment names', async () => {
    const events = await mountDialog({ name: 'Demo', sourcePath: 'C:\\Projects\\', hostPlatform: 'windows' })
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    expect(dialog.querySelector('.project-create__target')?.textContent).toBe('C:\\Projects\\Demo')
    events.name.value = '../other'
    await nextTick()
    expect(dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true)
  })

  it('keeps an unconfirmed result visible for review', async () => {
    await mountDialog({ name: 'Demo', sourcePath: '/repos', error: 'Check the project list before trying again.' })
    expect(document.body.querySelector('[role="alert"]')?.textContent).toContain('Check the project list')
  })
})
