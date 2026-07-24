// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import ProjectWorkspacePickerDialog from './ProjectWorkspacePickerDialog.vue'

const mocks = vi.hoisted(() => ({
  platform: { files: {} as { chooseProjectDirectory?: () => Promise<{ path: string } | null> } },
  rpcCall: vi.fn(),
}))

vi.mock('@/platform', () => ({
  getPlatform: () => mocks.platform,
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: mocks.rpcCall }),
}))

const mountedApps: App<Element>[] = []

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        common: { close: 'Close', cancel: 'Cancel' },
        workspaces: {
          chooseProject: 'Choose project',
          webPickerScope: 'Paths are on the gateway host.',
          pathPlaceholder: 'Project path',
          browse: 'Browse',
          choose: 'Choose',
        },
      },
    },
  })
}

async function mountPicker() {
  const events = { close: vi.fn(), choose: vi.fn() }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(ProjectWorkspacePickerDialog, {
    open: true,
    sessionKey: 'agent:main:webchat:picker',
    onClose: events.close,
    onChoose: events.choose,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  await nextTick()
  return { host, events }
}

beforeEach(() => {
  mocks.platform.files = {}
  mocks.rpcCall.mockReset()
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ProjectWorkspacePickerDialog', () => {
  it('uses the native desktop picker and treats cancellation as a no-op', async () => {
    mocks.platform.files.chooseProjectDirectory = vi.fn(async () => null)
    const { events } = await mountPicker()

    await new Promise(resolve => setTimeout(resolve, 0))

    expect(mocks.platform.files.chooseProjectDirectory).toHaveBeenCalledOnce()
    expect(events.choose).not.toHaveBeenCalled()
    expect(events.close).toHaveBeenCalledOnce()
    expect(mocks.rpcCall).not.toHaveBeenCalled()
  })

  it('browses gateway-host directories on web', async () => {
    mocks.rpcCall.mockResolvedValue({
      path: 'D:\\repos',
      entries: [
        { name: 'project-a', path: 'D:\\repos\\project-a', kind: 'directory', selectable: true },
      ],
    })
    await mountPicker()
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(mocks.rpcCall).toHaveBeenCalledWith('sandbox.path.list', {
      sessionKey: 'agent:main:webchat:picker',
      path: '.',
      kind: 'workspace',
      browseChildren: true,
    })
    expect(document.body.textContent).toContain('project-a')
  })
})
