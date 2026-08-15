<template>
  <section
    class="artifact-document"
    :data-document-source="workspace?.source || 'loading'"
    :data-download-only="downloadOnly ? 'true' : 'false'"
  >
    <nav
      class="artifact-document__tabs"
      role="tablist"
      :aria-label="t('workbench.artifactDocument.sections')"
    >
      <button
        v-for="tab in tabs"
        :id="tabId(tab.id)"
        :key="tab.id"
        type="button"
        role="tab"
        class="artifact-document__tab"
        :class="{ 'is-active': activeTab === tab.id }"
        :aria-controls="panelId(tab.id)"
        :aria-selected="activeTab === tab.id"
        :tabindex="activeTab === tab.id ? 0 : -1"
        @click="activeTab = tab.id"
        @keydown="onTabKeydown($event, tab.id)"
      >
        <span>{{ tab.label }}</span>
        <span v-if="tab.count !== null" class="artifact-document__count">
          {{ tab.count }}
        </span>
      </button>
      <span v-if="documentSnapshot.loading" class="artifact-document__loading" role="status">
        {{ t('workbench.artifactDocument.syncing') }}
      </span>
      <button
        v-if="editableCopyAvailable"
        type="button"
        class="artifact-document__publish"
        data-artifact-action="create-editable-copy"
        :disabled="editableCopyBusy || mutationBusy"
        @click="createEditableCopy"
      >
        <Icon name="edit" :size="14" />
        <span>{{ t('workbench.resources.edit', { name: artifact.name || '' }) }}</span>
      </button>
    </nav>

    <p
      v-if="showDesktopElementEditingHint && activeTab === 'preview'"
      class="artifact-document__desktop-editing-hint"
      data-testid="artifact-document-desktop-editing-hint"
      role="note"
    >
      {{ t('workbench.artifactAnnotation.desktopEditingOnly') }}
    </p>

    <p v-if="mutationError" class="artifact-document__mutation-error" role="alert">
      {{ mutationError }}
    </p>

    <div
      v-if="annotationFallback"
      class="artifact-document__annotation-fallback"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="`${instanceId}-annotation-fallback-title`"
    >
      <form
        class="artifact-document__annotation-fallback-card"
        @submit.prevent="submitAnnotationFallback"
      >
        <strong :id="`${instanceId}-annotation-fallback-title`">
          {{ t('workbench.artifactAnnotation.fallbackTitle') }}
        </strong>
        <p>{{ t('workbench.artifactAnnotation.fallbackDetail') }}</p>
        <img
          v-if="annotationFallback.screenshotUrl"
          class="artifact-document__annotation-fallback-screenshot"
          :src="annotationFallback.screenshotUrl"
          :alt="t('workbench.artifactAnnotation.frozenPreview')"
        />
        <textarea
          ref="annotationFallbackInput"
          v-model="annotationFallbackBody"
          :maxlength="promptAnnotationMaxBodyLength"
          :placeholder="t('workbench.artifactAnnotation.placeholder')"
          @input="updateAnnotationFallback"
          @keydown.esc.prevent="cancelAnnotationFallback"
        />
        <span class="artifact-document__annotation-fallback-actions">
          <button type="button" class="btn" @click="cancelAnnotationFallback">
            {{ t('common.cancel') }}
          </button>
          <button
            type="submit"
            class="btn btn--primary"
            :disabled="!annotationFallbackBody.trim() || annotationFallbackTooLong"
          >
            {{ t('workbench.artifactAnnotation.keepDraft') }}
          </button>
        </span>
      </form>
    </div>

    <div
      v-show="activeTab === 'preview'"
      :id="panelId('preview')"
      class="artifact-document__panel artifact-document__panel--preview"
      data-document-section="preview"
      role="tabpanel"
      :aria-labelledby="tabId('preview')"
    >
      <div v-if="downloadOnly" class="artifact-document__download-only" role="status">
        <span class="artifact-document__download-icon" aria-hidden="true">
          <Icon name="fileText" :size="28" />
        </span>
        <strong>{{ downloadOnlyTitle }}</strong>
        <p>{{ downloadOnlyDetail }}</p>
        <button
          type="button"
          class="btn btn--primary"
          @click="downloadHead"
        >
          <Icon name="download" :size="15" />
          <span>{{ t('workbench.artifactDocument.downloadLatest') }}</span>
        </button>
        <small>{{ t('workbench.artifactDocument.noEditingClaim') }}</small>
      </div>
      <ArtifactPreviewPanel
        v-else
        ref="previewRef"
        :artifact="headArtifact"
        :auth-token="authToken"
        :base-origin="baseOrigin"
        :native-html="nativeHtml"
        :native-surface-state="nativeSurfaceState"
        :preview-blocked="previewBlocked"
        :preview-collection-status="previewCollectionStatus"
        :preview-error-message="previewErrorMessage"
        :preview-launch-url="previewLaunchUrl"
        :preview-mode="previewMode"
        :preview-network-allowed="previewNetworkAllowed"
        :preview-sandbox-profile="previewSandboxProfile"
        :session-key="sessionKey"
        :show-header="showHeader"
        :suspended="suspended || activeTab !== 'preview'"
        @workbench-event="forwardWorkbenchEvent"
      />
    </div>

    <div
      v-if="documentFeatures && documentModel?.capabilities.source && sourceActivated"
      v-show="activeTab === 'source'"
      :id="panelId('source')"
      class="artifact-document__panel artifact-document__panel--source"
      role="tabpanel"
      :aria-labelledby="tabId('source')"
    >
      <ArtifactHtmlStudio
        :key="documentModel.documentId"
        ref="sourceRef"
        :artifact="artifact"
        :document="documentModel"
        :session-key="sessionKey"
      />
    </div>

    <div
      v-if="documentFeatures"
      v-show="activeTab === 'versions'"
      :id="panelId('versions')"
      class="artifact-document__panel artifact-document__list-panel"
      data-document-section="versions"
      role="tabpanel"
      :aria-labelledby="tabId('versions')"
    >
      <p v-if="revisions.length === 0" class="artifact-document__empty">
        {{ t('workbench.artifactDocument.noVersions') }}
      </p>
      <ol v-else class="artifact-document__list artifact-document__versions">
        <li v-for="revision in revisions" :key="revision.revisionId">
          <span class="artifact-document__list-main">
            <strong>
              {{ t('workbench.artifactDocument.versionNumber', { generation: revision.generation }) }}
            </strong>
            <small>{{ revision.source }} · {{ actorLabel(revision.actorId, revision.actorKind) }}</small>
          </span>
          <span class="artifact-document__list-meta">
            <span
              v-if="revision.revisionId === documentModel?.headRevisionId"
              class="artifact-document__badge"
            >
              {{ t('workbench.artifactDocument.current') }}
            </span>
            <time v-if="revision.createdAt" :datetime="dateTime(revision.createdAt)">
              {{ formatDate(revision.createdAt) }}
            </time>
            <span class="artifact-document__actions">
              <button
                type="button"
                class="artifact-document__action"
                data-artifact-action="download-revision"
                :disabled="mutationBusy"
                @click="downloadRevision(revision.revisionId)"
              >
                {{ t('workbench.artifactDocument.downloadVersion') }}
              </button>
              <button
                v-if="canRestoreRevision(revision.revisionId)"
                type="button"
                class="artifact-document__action artifact-document__action--danger"
                data-artifact-action="restore-revision"
                :disabled="mutationBusy"
                @click="restoreRevision(revision.revisionId)"
              >
                {{ busyAction === `restore:${revision.revisionId}`
                  ? t('workbench.artifactDocument.restoring')
                  : t('workbench.artifactDocument.restoreVersion') }}
              </button>
            </span>
          </span>
        </li>
      </ol>
    </div>

    <div
      v-if="documentFeatures"
      v-show="activeTab === 'changes'"
      :id="panelId('changes')"
      class="artifact-document__panel artifact-document__list-panel"
      data-document-section="changes"
      role="tabpanel"
      :aria-labelledby="tabId('changes')"
    >
      <p v-if="changeSets.length === 0" class="artifact-document__empty">
        {{ t('workbench.artifactDocument.noChanges') }}
      </p>
      <ol v-else class="artifact-document__list">
        <li v-for="changeSet in changeSets" :key="changeSet.changeSetId">
          <span class="artifact-document__list-main">
            <strong>{{ changeSet.summary || changeSet.status }}</strong>
            <small>
              {{ changeSet.status }} · {{ t('workbench.artifactDocument.operationCount', {
                count: changeSet.operations.length,
              }) }}
            </small>
          </span>
          <span class="artifact-document__list-meta">
            <time v-if="changeSet.updatedAt" :datetime="dateTime(changeSet.updatedAt)">
              {{ formatDate(changeSet.updatedAt) }}
            </time>
            <button
              v-if="canRevertChangeSet(changeSet.changeSetId)"
              type="button"
              class="artifact-document__action artifact-document__action--danger"
              data-artifact-action="revert-change-set"
              :disabled="mutationBusy"
              @click="revertChangeSet(changeSet.changeSetId)"
            >
              {{ busyAction === `revert:${changeSet.changeSetId}`
                ? t('workbench.artifactDocument.reverting')
                : t('workbench.artifactDocument.revertChange') }}
            </button>
          </span>
        </li>
      </ol>
    </div>

  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import Icon from '@/components/Icon.vue'
import type {
  ArtifactDocumentActions,
  ArtifactDocumentWorkspaceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import {
  PROMPT_ANNOTATION_MAX_BODY_LENGTH,
  promptAnnotationBodyWithinLimit,
} from '@/types/promptAnnotations'
import { isOfficeArtifact } from '@/utils/chat/artifacts'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import type { WorkbenchComponentEvent } from '@/workbench/types'
import { artifactPayloadFromRevision } from '@/workbench/artifactDocumentProvider'
import ArtifactHtmlStudio from './ArtifactHtmlStudio.vue'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'

type DocumentTab = 'preview' | 'source' | 'versions' | 'changes'
type PreviewHandle = { reload: () => Promise<void> }
type SourceHandle = {
  beforeClose: () => Promise<boolean>
  reload: () => Promise<void>
}

const props = withDefaults(defineProps<{
  artifact: ArtifactPayload
  documentActions?: ArtifactDocumentActions | null
  documentFeatures?: boolean
  documentSnapshot?: ArtifactDocumentWorkspaceSnapshot
  editableCopyAvailable?: boolean
  editableCopyBusy?: boolean
  authToken?: string
  baseOrigin?: string
  nativeHtml?: boolean
  nativeSurfaceState?: 'crashed' | 'error' | 'loading' | 'ready'
  previewBlocked?: boolean
  previewCollectionStatus?: 'complete' | 'partial' | 'not_applicable'
  previewErrorMessage?: string
  previewLaunchUrl?: string
  previewMode?: 'full' | 'offline'
  previewNetworkAllowed?: boolean
  previewSandboxProfile?: 'default' | 'opaque-offline'
  publishing?: boolean
  sessionKey?: string
  showHeader?: boolean
  suspended?: boolean
  annotationFallback?: {
    annotationId: string
    body: string
    reason?: string
    screenshotUrl?: string
  } | null
}>(), {
  documentSnapshot: () => ({
    key: '',
    loading: false,
    loaded: false,
    stale: false,
    error: null,
    workspace: null,
  }),
  documentActions: null,
  documentFeatures: true,
  editableCopyAvailable: false,
  editableCopyBusy: false,
  authToken: '',
  baseOrigin: '',
  nativeHtml: false,
  nativeSurfaceState: 'loading',
  previewBlocked: false,
  previewCollectionStatus: 'not_applicable',
  previewErrorMessage: '',
  previewLaunchUrl: '',
  previewMode: 'offline',
  previewNetworkAllowed: true,
  previewSandboxProfile: 'default',
  publishing: false,
  sessionKey: '',
  showHeader: false,
  suspended: false,
  annotationFallback: null,
})

const emit = defineEmits<{
  'workbench-event': [event: WorkbenchComponentEvent]
}>()

const { t } = useI18n()
const instanceId = useId()
const promptAnnotationMaxBodyLength = PROMPT_ANNOTATION_MAX_BODY_LENGTH
const annotationFallbackBody = ref('')
const annotationFallbackInput = ref<HTMLTextAreaElement | null>(null)
const annotationFallbackTooLong = computed(() => (
  !promptAnnotationBodyWithinLimit(annotationFallbackBody.value)
))
const activeTab = ref<DocumentTab>('preview')
const sourceActivated = ref(false)
const previewRef = ref<PreviewHandle | null>(null)
const sourceRef = ref<SourceHandle | null>(null)
const busyAction = ref<string | null>(null)
const mutationError = ref('')

watch(activeTab, tab => {
  if (tab === 'source') sourceActivated.value = true
})

watch(() => props.annotationFallback, (fallback, previous) => {
  if (!fallback) {
    annotationFallbackBody.value = ''
    return
  }
  if (fallback.annotationId !== previous?.annotationId) {
    annotationFallbackBody.value = fallback.body
  }
  void nextTick(() => annotationFallbackInput.value?.focus())
}, { immediate: true })

function annotationFallbackPayload() {
  return {
    annotationId: props.annotationFallback?.annotationId || '',
    body: annotationFallbackBody.value,
  }
}

function updateAnnotationFallback() {
  emit('workbench-event', {
    type: 'artifact-annotation-fallback-update',
    payload: annotationFallbackPayload(),
  })
}

function submitAnnotationFallback() {
  if (
    !props.annotationFallback
    || !annotationFallbackBody.value.trim()
    || !promptAnnotationBodyWithinLimit(annotationFallbackBody.value)
  ) return
  emit('workbench-event', {
    type: 'artifact-annotation-fallback-submit',
    payload: annotationFallbackPayload(),
  })
}

function cancelAnnotationFallback() {
  if (!props.annotationFallback) return
  emit('workbench-event', {
    type: 'artifact-annotation-fallback-cancel',
    payload: { annotationId: props.annotationFallback.annotationId },
  })
}
const documentFeatures = computed(() => props.documentFeatures)
const workspace = computed(() => documentFeatures.value
  ? props.documentSnapshot.workspace
  : null)
const documentModel = computed(() => workspace.value?.document || null)
const revisions = computed(() => workspace.value?.revisions || [])
const changeSets = computed(() => workspace.value?.changeSets || [])
const headArtifact = computed(() => documentFeatures.value
  ? workspace.value?.headArtifact || props.artifact
  : props.artifact)
const downloadOnly = computed(() => {
  if (isOfficeArtifact(headArtifact.value)) {
    return documentModel.value?.capabilities.preview !== true
  }
  return artifactWorkbenchPreviewKind(headArtifact.value) === 'unsupported'
})
const mutationBusy = computed(() => busyAction.value !== null)
const actionsAvailable = computed(() => Boolean(
  props.documentActions
  && props.sessionKey.trim()
  && workspace.value?.source === 'document-api',
))
const showDesktopElementEditingHint = computed(() => Boolean(
  documentFeatures.value
  && workspace.value?.source === 'document-api'
  && documentModel.value?.kind === 'html'
  && documentModel.value.capabilities.agentEdit
  && artifactWorkbenchPreviewKind(headArtifact.value) === 'html'
  && !props.nativeHtml
  && !downloadOnly.value,
))
const downloadOnlyTitle = computed(() => isOfficeArtifact(headArtifact.value)
  ? t('workbench.artifactDocument.officeDownloadOnlyTitle')
  : t('workbench.artifactDocument.downloadOnlyTitle'))
const downloadOnlyDetail = computed(() => isOfficeArtifact(headArtifact.value)
  ? t('workbench.artifactDocument.officeDownloadOnlyDetail')
  : t('workbench.artifactDocument.downloadOnlyDetail'))

watch(
  () => documentModel.value?.documentId || '',
  (documentId, previousDocumentId) => {
    if (!previousDocumentId || documentId === previousDocumentId) return
    activeTab.value = 'preview'
    sourceActivated.value = false
  },
)

const tabs = computed<Array<{ id: DocumentTab; label: string; count: number | null }>>(() => {
  const result: Array<{ id: DocumentTab; label: string; count: number | null }> = [
    { id: 'preview', label: t('workbench.artifactDocument.preview'), count: null },
  ]
  if (documentModel.value?.capabilities.source) {
    result.push({ id: 'source', label: t('workbench.artifactDocument.source'), count: null })
  }
  if (documentFeatures.value) {
    result.push(
      { id: 'versions', label: t('workbench.artifactDocument.versions'), count: revisions.value.length },
      { id: 'changes', label: t('workbench.artifactDocument.changes'), count: changeSets.value.length },
    )
  }
  return result
})

function tabId(tab: DocumentTab): string {
  return `${instanceId}-artifact-document-tab-${tab}`
}

function panelId(tab: DocumentTab): string {
  return `${instanceId}-artifact-document-panel-${tab}`
}

function onTabKeydown(event: KeyboardEvent, current: DocumentTab) {
  const order = tabs.value.map(tab => tab.id)
  const index = order.indexOf(current)
  let next = index
  if (event.key === 'ArrowLeft') next = (index - 1 + order.length) % order.length
  else if (event.key === 'ArrowRight') next = (index + 1) % order.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = order.length - 1
  else return
  event.preventDefault()
  const nextTab = order[next]
  if (!nextTab) return
  activeTab.value = nextTab
  void nextTick(() => document.getElementById(tabId(nextTab))?.focus())
}

function forwardWorkbenchEvent(event: WorkbenchComponentEvent) {
  emit('workbench-event', event)
}

function downloadHead() {
  emit('workbench-event', {
    type: 'artifact-download',
    payload: headArtifact.value,
  })
}

function createEditableCopy() {
  if (!props.editableCopyAvailable || props.editableCopyBusy) return
  emit('workbench-event', { type: 'artifact-create-editable-copy' })
}

function downloadRevision(revisionId: string) {
  const revision = revisions.value.find(item => item.revisionId === revisionId)
  if (!revision || revision.documentId !== documentModel.value?.documentId) return
  emit('workbench-event', {
    type: 'artifact-download',
    payload: artifactPayloadFromRevision(revision),
  })
}

function canRestoreRevision(revisionId: string): boolean {
  return Boolean(
    actionsAvailable.value
    && documentModel.value?.capabilities.revisions
    && revisionId !== documentModel.value.headRevisionId
    && revisions.value.some(item => (
      item.revisionId === revisionId
      && item.documentId === documentModel.value?.documentId
    )),
  )
}

function canRevertChangeSet(changeSetId: string): boolean {
  const document = documentModel.value
  const changeSet = changeSets.value.find(item => item.changeSetId === changeSetId)
  return Boolean(
    actionsAvailable.value
    && document?.capabilities.changeSets
    && changeSet?.documentId === document.documentId
    && changeSet.status === 'applied'
    && changeSet.appliedRevisionId === document.headRevisionId,
  )
}

async function runMutation(
  actionId: string,
  failureMessage: string,
  operation: () => Promise<unknown>,
): Promise<boolean> {
  if (mutationBusy.value) return false
  mutationError.value = ''
  busyAction.value = actionId
  try {
    await operation()
    return true
  } catch {
    mutationError.value = failureMessage
    return false
  } finally {
    busyAction.value = null
  }
}

async function restoreRevision(revisionId: string) {
  if (!canRestoreRevision(revisionId) || !props.documentActions) return
  await runMutation(
    `restore:${revisionId}`,
    t('workbench.artifactDocument.restoreFailed'),
    () => props.documentActions!.restoreRevision(props.artifact, props.sessionKey, revisionId),
  )
}

async function revertChangeSet(changeSetId: string) {
  if (!canRevertChangeSet(changeSetId) || !props.documentActions) return
  await runMutation(
    `revert:${changeSetId}`,
    t('workbench.artifactDocument.revertFailed'),
    () => props.documentActions!.revertChangeSet(props.artifact, props.sessionKey, changeSetId),
  )
}

function dateTime(value: number | string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toISOString()
}

function formatDate(value: number | string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function actorLabel(actorId: string, kind: string): string {
  return actorId || t('workbench.artifactDocument.actor', { kind })
}

async function reload() {
  if (activeTab.value === 'source') await sourceRef.value?.reload()
  else await previewRef.value?.reload()
}

async function beforeClose(): Promise<boolean> {
  return await sourceRef.value?.beforeClose() ?? true
}

defineExpose({ beforeClose, reload })
</script>

<style scoped>
.artifact-document {
  position: relative;
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--bg-surface);
  color: var(--text);
}

.artifact-document__annotation-fallback {
  position: absolute;
  z-index: 20;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: color-mix(in srgb, var(--bg) 82%, transparent);
}

.artifact-document__annotation-fallback-card {
  display: grid;
  gap: 10px;
  width: min(420px, 100%);
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-lg);
}

.artifact-document__annotation-fallback-card p {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.artifact-document__annotation-fallback-screenshot {
  display: block;
  width: 100%;
  max-height: 180px;
  object-fit: contain;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}

.artifact-document__annotation-fallback-card textarea {
  min-height: 112px;
  resize: vertical;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  line-height: 1.5;
}

.artifact-document__annotation-fallback-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.artifact-document__tabs {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 2px;
  min-height: 42px;
  padding: 5px 10px 0;
  overflow-x: auto;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

.artifact-document__tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 34px;
  padding: 0 9px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}

.artifact-document__tab:hover,
.artifact-document__tab:focus-visible,
.artifact-document__tab.is-active {
  color: var(--text);
}

.artifact-document__tab.is-active {
  border-bottom-color: var(--accent);
}

.artifact-document__count,
.artifact-document__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  min-height: 18px;
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 10px;
  line-height: 1;
}

.artifact-document__loading {
  margin-inline-start: auto;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.artifact-document__publish {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  margin-inline-start: auto;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  white-space: nowrap;
}

.artifact-document__loading + .artifact-document__publish {
  margin-inline-start: 6px;
}

.artifact-document__publish:hover:not(:disabled),
.artifact-document__publish:focus-visible {
  border-color: var(--accent);
  color: var(--accent);
}

.artifact-document__publish:disabled {
  cursor: default;
  opacity: .6;
}

.artifact-document__mutation-error {
  flex: 0 0 auto;
  margin: 0;
  padding: 8px 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  background: color-mix(in srgb, var(--danger) 8%, var(--bg));
  color: var(--danger);
  font-size: 12px;
}

.artifact-document__desktop-editing-hint {
  flex: 0 0 auto;
  margin: 0;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--accent) 4%, var(--bg));
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}

.artifact-document__panel {
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.artifact-document__panel--preview {
  display: flex;
}

.artifact-document__panel--source {
  display: flex;
}

.artifact-document__download-only,
.artifact-document__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 10px;
  width: 100%;
  min-height: 100%;
  margin: 0;
  padding: 36px;
  color: var(--text-muted);
  text-align: center;
}

.artifact-document__download-only strong {
  color: var(--text);
  font-size: 15px;
}

.artifact-document__download-only p {
  max-width: 440px;
  margin: 0;
  line-height: 1.55;
}

.artifact-document__download-only small {
  max-width: 440px;
  line-height: 1.45;
}

.artifact-document__download-icon {
  display: grid;
  place-items: center;
  width: 54px;
  height: 54px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  color: var(--accent);
}

.artifact-document__list-panel {
  overflow: auto;
  padding: 14px;
}

.artifact-document__list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.artifact-document__list li {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}

.artifact-document__list-main,
.artifact-document__list-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.artifact-document__list-main small,
.artifact-document__list time,
.artifact-document__list-meta {
  color: var(--text-muted);
  font-size: 11px;
}

.artifact-document__list time {
  white-space: nowrap;
}

.artifact-document__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.artifact-document__action {
  min-height: 28px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.artifact-document__action:hover:not(:disabled),
.artifact-document__action:focus-visible {
  border-color: var(--accent);
}

.artifact-document__action--danger {
  color: var(--danger);
}

.artifact-document__action:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

@media (max-width: 520px) {
  .artifact-document__tabs {
    padding-inline: 6px;
  }

  .artifact-document__tab {
    padding-inline: 7px;
  }

  .artifact-document__list li {
    flex-direction: column;
  }
}
</style>
