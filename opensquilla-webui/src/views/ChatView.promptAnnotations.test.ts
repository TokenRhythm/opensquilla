import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

describe('ChatView prompt annotation focus and reuse', () => {
  it('keeps Workbench resources independently gated and hides actions on old gateways', () => {
    const start = chatViewSource.indexOf('const workbenchResourcesEnabled = computed(')
    const end = chatViewSource.indexOf('\nconst activePromptAnnotations', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('appStore.features.documentWorkbenchResources === true')
    expect(source).toContain('|| promptAnnotationsEnabled.value')
    expect(source).toContain("rpc.supportsMethod('workbench.resources.list')")
    expect(source).toContain("rpc.supportsMethod('workbench.resources.get')")
    expect(source).toContain('const attachmentWorkbenchPreviewEnabled = computed(')
    expect(source).toContain("rpc.supportsMethod('workbench.resources.get')")
    expect(source).toContain('const attachmentWorkbenchEditEnabled = computed(')
    expect(source).toContain("rpc.supportsMethod('documents.import')")
    expect(chatViewSource).toContain(
      ':workbench-resource-preview-enabled="attachmentWorkbenchPreviewEnabled"',
    )
    expect(chatViewSource).toContain(
      ':workbench-resource-edit-enabled="attachmentWorkbenchEditEnabled"',
    )
    expect(chatViewSource).toContain(
      ':workbench-attachment-resources="attachmentWorkbenchResources"',
    )
    expect(chatViewSource).toContain("resource.resource.type === 'attachment'")
  })

  it('maps an open annotation editor to a global send-blocking message', () => {
    const start = chatViewSource.indexOf('function promptAnnotationBlockedMessage()')
    const end = chatViewSource.indexOf('\nasync function updatePromptAnnotation(', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain("if (reason === 'editing')")
    expect(source).toContain("t('chat.promptAnnotations.editingBlocked')")
  })

  it('activates the trusted preview before invoking the server focus RPC', () => {
    const start = chatViewSource.indexOf('async function jumpPromptAnnotation(')
    const end = chatViewSource.indexOf('\nasync function reusePromptAnnotation(', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('const activated = await focusArtifactPromptAnnotation({')
    expect(source).toContain('await artifactPromptAnnotationsStore.focus(annotationId)')
    expect(source.indexOf('await focusArtifactPromptAnnotation({'))
      .toBeLessThan(source.indexOf('artifactPromptAnnotationsStore.focus(annotationId)'))
    expect(source).toContain("promptAnnotationRpcErrorCode(error) === 'ARTIFACT_REVISION_CHANGED'")
    expect(source).not.toContain("if (annotation.freshness === 'stale') return")
    expect(source).not.toContain('artifactPromptAnnotationsStore.markAnnotationStale(')
  })

  it('flushes a matching open document before preparing annotation drafts', () => {
    const start = chatViewSource.indexOf('preparePromptAnnotationsForSend: async (ids')
    const end = chatViewSource.indexOf('\n  promptAnnotationSnapshots:', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('workbenchDocumentContextStore.prepareDocumentForSend(')
    expect(source).toContain('artifactPromptAnnotationsStore.prepareForSend(ids)')
    expect(source.indexOf('prepareDocumentForSend('))
      .toBeLessThan(source.indexOf('prepareForSend(ids)'))
  })

  it('reuses a history snapshot only through explicit current-DOM reselection', () => {
    const start = chatViewSource.indexOf('async function reusePromptAnnotation(')
    const end = chatViewSource.indexOf('\nconst promptCacheKeepaliveOpen', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('await reuseArtifactPromptAnnotation({')
    expect(source).toContain('body: annotation.body')
    expect(source).toContain('documentId: annotation.documentId')
    expect(source).not.toContain('annotation.locator')
    expect(source).not.toContain('annotation.anchorId')
  })
})
