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

  it('keeps workspace validation and draft hydration cancellation request-local', () => {
    const validationStart = chatViewSource.indexOf('async function validateActiveProjectBeforeSend()')
    const validationEnd = chatViewSource.indexOf('\nfunction draftProjectHydrationIsCurrent(', validationStart)
    const validation = chatViewSource.slice(validationStart, validationEnd)
    const draftStart = chatViewSource.indexOf('async function syncDraftProjectFromRoute(')
    const draftEnd = chatViewSource.indexOf('\nfunction enterDraft()', draftStart)
    const draft = chatViewSource.slice(draftStart, draftEnd)

    expect(validationStart).toBeGreaterThan(-1)
    expect(validation).toContain("timeoutAction: 'reconnect'")
    expect(validation).toContain("abortAction: 'reject'")
    expect(validation).not.toContain("abortAction: 'reconnect'")
    expect(draftStart).toBeGreaterThan(-1)
    expect(draft).toContain('await sessionConversation.ready({')
    expect(draft).not.toContain('rpc.waitForConnection(')
    expect(draft).toContain("timeoutAction: 'reject'")
    expect(draft).toContain("timeoutAction: 'reconnect'")
    expect(draft).toContain("abortAction: 'reject'")
    expect(draft).not.toContain("abortAction: 'reconnect'")
  })

  it('queues Session optional reads only after bootstrap critical frames', () => {
    const schedulerStart = chatViewSource.indexOf('type SessionOptionalReadRequest = {')
    const schedulerEnd = chatViewSource.indexOf('\nwatch(shareableMessageCount', schedulerStart)
    const scheduler = chatViewSource.slice(schedulerStart, schedulerEnd)
    const sessionWatchStart = scheduler.indexOf('watch(sessionKey, () => {')
    const reconnectWatchStart = scheduler.indexOf("watch(() => rpc.state", sessionWatchStart)
    const sessionWatch = scheduler.slice(sessionWatchStart, reconnectWatchStart)

    expect(schedulerStart).toBeGreaterThan(-1)
    expect(scheduler).toContain('if (!optionalSessionRpcAllowed.value) return')
    expect(scheduler).toContain('watch(optionalSessionRpcAllowed, admitted => {')
    expect(scheduler).toContain('void artifactPromptAnnotationsStore.load(')
    expect(scheduler).toContain('? loadSessionArtifactsAfterReconnect()')
    expect(scheduler).toContain(': loadSessionArtifacts()')
    expect(sessionWatch).toContain('scheduleSessionOptionalReads({')
    expect(sessionWatch).not.toContain('artifactPromptAnnotationsStore.load(')
    expect(sessionWatch).not.toContain('void loadSessionArtifacts()')
  })
})
