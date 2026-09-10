// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import { createLegacyArtifactWorkspace, type ArtifactDocumentProvider } from '@/workbench/artifactDocumentProvider'
import ArtifactSourcePreview from './ArtifactSourcePreview.vue'

describe('read-only source preview', () => {
  it('reads source as text without opening an editing session or executing markup', async () => {
    const pinia = createPinia()
    const store = useArtifactDocumentsStore(pinia)
    const source = '<h1>Working page</h1><script>throw new Error("must not run")</script>'
    const readSource = vi.fn(async () => ({ content: source }))
    store.setProvider({ readSource } as unknown as ArtifactDocumentProvider)
    const documentModel = createLegacyArtifactWorkspace({
      id: 'page', name: 'page.html', mime: 'text/html',
    }, 'session-a').document
    const root = document.createElement('div')
    const app = createApp(ArtifactSourcePreview, { document: documentModel, sessionKey: 'session-a' })
    app.use(pinia).use(i18n).mount(root)
    await nextTick()
    await nextTick()
    expect(readSource).toHaveBeenCalledOnce()
    expect(root.querySelector('code')?.textContent).toBe(source)
    expect(root.querySelector('script')).toBeNull()
    expect(root.querySelector('textarea, [contenteditable]')).toBeNull()
    app.unmount()
  })
})
