// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import ProjectWorkspaceCreateDialog from './ProjectWorkspaceCreateDialog.vue'

const mountedApps: App<Element>[] = []

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        common: { close: 'Close', cancel: 'Cancel' },
        workspaces: {
          createProject: 'Create project',
          projectName: 'Project name',
          projectNamePlaceholder: 'Project name',
          sourceFolders: 'Source folders',
          addSourceFolder: 'Add a folder OpenSquilla can read and edit',
          creatingProject: 'Creating…',
        },
      },
    },
  })
}

async function mountDialog(options: {
  name?: string
  sourcePath?: string
  busy?: boolean
  sourcePicking?: boolean
} = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const name = ref(options.name || '')
  const chooseSource = vi.fn()
  const create = vi.fn()
  const close = vi.fn()
  const Root = defineComponent(() => () => h(ProjectWorkspaceCreateDialog, {
    open: true,
    name: name.value,
    sourcePath: options.sourcePath || '',
    busy: options.busy || false,
    sourcePicking: options.sourcePicking || false,
    'onUpdate:name': (value: string) => { name.value = value },
    onChooseSource: chooseSource,
    onCreate: create,
    onClose: close,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  return { name, chooseSource, create, close }
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ProjectWorkspaceCreateDialog', () => {
  it('collects a project name and source folder before creating', async () => {
    const events = await mountDialog({
      name: 'Demo',
      sourcePath: '/repos/demo',
    })
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const source = dialog?.querySelector<HTMLButtonElement>('.project-create__source-picker')
    const create = Array.from(dialog?.querySelectorAll<HTMLButtonElement>('button') || [])
      .find(button => button.textContent?.trim() === 'Create project')

    expect(dialog?.textContent).toContain('Source folders')
    expect(dialog?.textContent).toContain('/repos/demo')
    expect(create?.disabled).toBe(false)

    source?.click()
    create?.click()
    await nextTick()

    expect(events.chooseSource).toHaveBeenCalledOnce()
    expect(events.create).toHaveBeenCalledWith({ name: 'Demo', path: '/repos/demo' })
  })

  it('keeps creation disabled until both fields are present', async () => {
    await mountDialog()
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const create = Array.from(dialog?.querySelectorAll<HTMLButtonElement>('button') || [])
      .find(button => button.textContent?.trim() === 'Create project')

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
    const create = Array.from(dialog?.querySelectorAll<HTMLButtonElement>('button') || [])
      .find(button => button.textContent?.trim() === 'Create project')

    expect(source?.disabled).toBe(true)
    expect(create?.disabled).toBe(true)

    source?.click()
    create?.click()
    await nextTick()

    expect(events.chooseSource).not.toHaveBeenCalled()
    expect(events.create).not.toHaveBeenCalled()
  })
})
