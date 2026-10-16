import { describe, expect, it, vi } from 'vitest'
import { ScriptTarget, transpileModule } from '@typescript/typescript6'
import appWorkbenchSource from './AppWorkbench.vue?raw'

// Run the real explicit annotation activation handler with bounded runtime
// dependencies; the native annotation API is outside this presentation test.
const start = appWorkbenchSource.indexOf('async function activatePromptAnnotationItem(')
const end = appWorkbenchSource.indexOf('\nasync function onPromptAnnotationFocus(', start)
if (start < 0 || end < 0) throw new Error('Missing annotation activation handler')
const handler = transpileModule(appWorkbenchSource.slice(start, end), {
  compilerOptions: { target: ScriptTarget.ES2022 },
}).outputText

function setup(scope = 'parent', activationSucceeds = true) {
  const item = { id: 'preview', scope: { type: 'session', id: scope } }
  const store = {
    activeSessionId: 'parent', activeItemId: '',
    activateItem: vi.fn((id: string) => {
      if (activationSucceeds) store.activeItemId = id
      return activationSucceeds
    }),
    setExpanded: vi.fn(), requestReveal: vi.fn(),
  }
  const dependencies = {
    store,
    promptAnnotationItem: vi.fn(() => item),
    artifactPromptAnnotations: { setActiveDocument: vi.fn() },
    nextTick: async () => {},
    runtimeManager: {
      flush: vi.fn(async () => {}),
      getRenderState: vi.fn(() => ({ annotationAvailable: true, nativeSurfaceState: 'ready' })),
    },
  }
  const activate = new Function(...Object.keys(dependencies), `${handler}; return activatePromptAnnotationItem`)(...Object.values(dependencies))
  // A child-owned document may be shown under the parent's visible scope.
  const detail = { documentId: 'document', sessionKey: 'child', acknowledge: vi.fn() }
  return { ...dependencies, activate, detail, item }
}

describe('AppWorkbench explicit annotation reveal', () => {
  it('reveals a current-scope document before waiting for its native surface', async () => {
    const context = setup()
    context.runtimeManager.flush.mockImplementation(async () => {
      expect(context.store.requestReveal).toHaveBeenCalledOnce()
    })
    expect(await context.activate(context.detail)).toBe(context.item)
    expect(context.detail.acknowledge).toHaveBeenCalledOnce()
    expect(context.store.setExpanded).toHaveBeenCalledWith(true)
    expect(context.artifactPromptAnnotations.setActiveDocument).toHaveBeenCalledWith('child', 'document')
  })

  it('does not reveal a document whose visible session scope is no longer active', async () => {
    const context = setup('previous-parent')
    expect(await context.activate(context.detail)).toBeNull()
    expect(context.store.activateItem).not.toHaveBeenCalled()
    expect(context.store.requestReveal).not.toHaveBeenCalled()
    expect(context.runtimeManager.flush).not.toHaveBeenCalled()
  })

  it('does not reveal an unrelated panel if activation fails', async () => {
    const context = setup('parent', false)
    expect(await context.activate(context.detail)).toBeNull()
    expect(context.store.requestReveal).not.toHaveBeenCalled()
    expect(context.store.setExpanded).not.toHaveBeenCalled()
  })
})
