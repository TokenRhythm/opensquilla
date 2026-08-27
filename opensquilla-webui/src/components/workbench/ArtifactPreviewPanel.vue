<template>
  <section
    class="artifact-preview"
    :data-preview-kind="preview.kind.value"
    :data-preview-state="preview.state.value"
  >
    <header v-if="showHeader" class="artifact-preview__toolbar">
      <span class="artifact-preview__file-icon" aria-hidden="true">
        <Icon :name="artifactIconName(artifact)" :size="17" />
      </span>
      <span class="artifact-preview__identity">
        <strong class="artifact-preview__title">{{ artifactFileTitle(artifact) }}</strong>
        <span class="artifact-preview__meta">{{ artifactFileSubtitle(artifact) }}</span>
      </span>
      <span class="artifact-preview__actions">
        <button
          v-if="preview.kind.value !== 'unsupported'"
          type="button"
          class="btn btn--icon btn--ghost artifact-preview__action"
          :aria-label="t('workbench.artifactPreview.refresh')"
          :title="t('workbench.artifactPreview.refresh')"
          :disabled="preview.state.value === 'loading'"
          @click="reloadPreview"
        >
          <Icon name="refresh" :size="15" />
        </button>
        <button
          type="button"
          class="btn btn--icon btn--ghost artifact-preview__action"
          :aria-label="t('workbench.artifactPreview.openExternal')"
          :title="t('workbench.artifactPreview.openExternal')"
          @click="emitArtifactEvent('artifact-external-open', 'external-open')"
        >
          <Icon name="externalLink" :size="15" />
        </button>
        <button
          type="button"
          class="btn btn--icon btn--ghost artifact-preview__action"
          :aria-label="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
          :title="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
          @click="emitArtifactEvent('artifact-download', 'download')"
        >
          <Icon name="download" :size="15" />
        </button>
      </span>
    </header>

    <p
      v-if="preview.state.value === 'missing-resource'
        || preview.state.value === 'ready-with-warnings'"
      class="artifact-preview__notice"
      role="status"
    >
      <Icon name="info" :size="14" />
      <span>{{ t('workbench.artifactPreview.missingResources') }}</span>
    </p>
    <p
      v-if="showOfflineWebLimits"
      class="artifact-preview__notice"
      role="status"
    >
      <Icon name="shield" :size="14" />
      <span>{{ t('workbench.artifactPreview.offlineWebLimits') }}</span>
    </p>

    <div class="artifact-preview__viewport">
      <div
        v-if="agentEditInProgress"
        class="artifact-preview__status"
        data-testid="artifact-preview-agent-edit-in-progress"
        role="status"
      >
        <Icon name="info" :size="18" />
        <strong>{{ t('workbench.artifactPreview.agentEditInProgress') }}</strong>
      </div>

      <div
        v-else-if="preview.state.value === 'loading'"
        class="artifact-preview__status"
        role="status"
        :aria-label="t('chat.loadingPreview')"
      >
        <span class="artifact-preview__loading-line" aria-hidden="true" />
        <span>{{ t('chat.loadingPreview') }}</span>
        <span v-if="preview.progress.value !== null">
          {{ preview.progress.value }}%
        </span>
      </div>

      <div
        v-else-if="preview.state.value === 'suspended'"
        class="artifact-preview__status"
        role="status"
      >
        <span>{{ t('workbench.artifactPreview.suspended') }}</span>
      </div>

      <div
        v-else-if="isFailureState"
        class="artifact-preview__status artifact-preview__status--error"
        role="alert"
      >
        <Icon name="info" :size="18" />
        <strong>{{ failureTitle }}</strong>
        <span class="artifact-preview__status-detail">{{ failureDetail }}</span>
        <span class="artifact-preview__status-actions">
          <button
            v-if="preview.state.value !== 'unsupported' || preview.errorCode.value !== 'unsupported'"
            type="button"
            class="btn btn--ghost"
            @click="reloadPreview"
          >
            <Icon name="refresh" :size="14" />
            <span>{{ t('chat.retry') }}</span>
          </button>
          <button
            type="button"
            class="btn btn--ghost"
            @click="emitArtifactEvent('artifact-download', 'download')"
          >
            <Icon name="download" :size="14" />
            <span>{{ t('chat.download') }}</span>
          </button>
        </span>
      </div>

      <template v-else-if="isRenderable">
        <img
          v-if="preview.kind.value === 'image'"
          class="artifact-preview__image"
          :src="preview.objectUrl.value"
          :alt="artifactFileTitle(artifact)"
          decoding="async"
        />
        <div
          v-else-if="preview.kind.value === 'pdf'"
          class="artifact-preview__pdf-stack"
        >
          <iframe
            ref="previewFrameRef"
            class="artifact-preview__frame artifact-preview__frame--pdf"
            :src="pdfFrameUrl"
            :title="t('chat.previewOf', { title: artifactFileTitle(artifact) })"
            tabindex="0"
            referrerpolicy="no-referrer"
          />
          <!-- The browser-owned PDF viewer cannot run our HTML Escape bridge.
               This focus-revealed exit follows the frame in DOM order so a
               keyboard user can leave the viewer and collapse the dialog. -->
          <button
            type="button"
            class="btn artifact-preview__frame-exit"
            @click="requestWorkbenchCollapse"
          >
            {{ t('workbench.collapse') }}
          </button>
        </div>
        <div
          v-else-if="preview.kind.value === 'markdown'"
          class="artifact-preview__markdown chat-markdown"
        >
          <!-- eslint-disable-next-line vue/no-v-html -- output is DOMPurify-sanitized in the resource controller -->
          <div v-html="preview.markdownHtml.value" />
        </div>
        <pre
          v-else-if="preview.kind.value === 'text'"
          class="artifact-preview__text"
        >{{ preview.text.value }}</pre>
        <iframe
          v-else-if="preview.kind.value === 'html' && !nativeHtml"
          :key="htmlFrameGeneration"
          ref="previewFrameRef"
          class="artifact-preview__frame artifact-preview__frame--html"
          :src="preview.objectUrl.value"
          :title="t('chat.previewOf', { title: artifactFileTitle(artifact) })"
          tabindex="0"
          :sandbox="htmlSandbox"
          :allow="htmlPermissions"
          referrerpolicy="no-referrer"
        />
        <div
          v-else-if="preview.kind.value === 'html'"
          class="artifact-preview__native-slot"
          data-workbench-native-surface-slot
          :aria-label="t('workbench.artifactPreview.nativePreview')"
        >
          <span class="artifact-preview__native-fallback">
            {{ t('workbench.artifactPreview.nativePreview') }}
          </span>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  useArtifactPreviewResource,
  type ArtifactPreviewResourceState,
  type NativeHtmlArtifactResource,
} from '@/composables/workbench/useArtifactPreviewResource'
import type { ArtifactPayload } from '@/types/rpc'
import type { WorkbenchComponentEvent } from '@/workbench/types'
import {
  artifactFileSubtitle,
  artifactFileTitle,
  artifactIconName,
} from '@/utils/chat/artifacts'
import { ARTIFACT_PREVIEW_ESCAPE_MESSAGE } from '@/utils/workbench/artifactPreview'

const props = withDefaults(defineProps<{
  agentEditInProgress?: boolean
  artifact: ArtifactPayload
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
  sessionKey?: string
  showHeader?: boolean
  suspended?: boolean
}>(), {
  agentEditInProgress: false,
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
  sessionKey: '',
  showHeader: true,
  suspended: false,
})

const emit = defineEmits<{
  download: [artifact: ArtifactPayload]
  'external-open': [artifact: ArtifactPayload]
  'native-html-ready': [resource: NativeHtmlArtifactResource]
  'state-change': [state: ArtifactPreviewResourceState]
  'workbench-event': [event: WorkbenchComponentEvent]
}>()

const { t } = useI18n()
const previewFrameRef = ref<HTMLIFrameElement | null>(null)
const htmlFrameGeneration = ref(0)

const preview = useArtifactPreviewResource({
  artifact: () => props.artifact,
  authToken: () => props.authToken,
  baseOrigin: () => props.baseOrigin,
  htmlCollectionStatus: () => props.previewCollectionStatus,
  htmlLaunchUrl: () => props.previewLaunchUrl,
  htmlLeaseState: () => props.previewBlocked
    ? props.previewErrorMessage ? 'blocked' : 'pending'
    : 'ready',
  nativeHtml: () => props.nativeHtml,
  onNativeHtmlReady: resource => {
    emit('native-html-ready', resource)
    emit('workbench-event', { type: 'native-html-ready', payload: resource })
  },
  sessionKey: () => props.sessionKey,
})

const resourceSignature = computed(() => [
  props.artifact.id || '',
  props.artifact.download_url || '',
  props.artifact.name || '',
  props.artifact.mime || '',
  props.artifact.size || '',
  props.sessionKey,
  props.authToken,
  props.baseOrigin,
  props.nativeHtml ? 'native' : 'web',
  props.previewBlocked ? 'blocked' : 'unblocked',
  props.previewCollectionStatus,
  props.previewErrorMessage,
  props.previewLaunchUrl,
  props.previewMode,
  props.previewNetworkAllowed ? 'network' : 'no-network',
  props.previewSandboxProfile,
].join('\u0000'))

let loadedResourceSignature: string | null = null
let resourceSyncGeneration = 0

async function syncPreviewResource(
  signature: string,
  suspended: boolean,
  returningFromSuspend: boolean,
) {
  const generation = ++resourceSyncGeneration
  if (suspended) {
    preview.suspend()
    return
  }

  if (returningFromSuspend) await preview.resume()
  if (
    generation !== resourceSyncGeneration
    || props.suspended
    || resourceSignature.value !== signature
  ) return

  // A panel opened directly on Source has never loaded preview bytes. Its
  // first resume performs that initial load, so recording the signature here
  // avoids fetching the same revision twice.
  if (returningFromSuspend && loadedResourceSignature === null) {
    loadedResourceSignature = signature
    return
  }

  // Vue's watch tuple remembers signatures even while the panel is hidden.
  // Keep a separate identity for the bytes actually loaded by the preview so
  // resuming after a Source save cannot revive an older native lease or Blob.
  if (loadedResourceSignature !== signature) await preview.reload()
  else if (!returningFromSuspend) await preview.resume()
  if (
    generation === resourceSyncGeneration
    && !props.suspended
    && resourceSignature.value === signature
  ) loadedResourceSignature = signature
}

watch(
  [resourceSignature, () => props.suspended],
  ([signature, suspended], previous) => {
    void syncPreviewResource(signature, suspended, previous?.[1] === true)
  },
  { immediate: true },
)

watch(
  () => props.nativeSurfaceState,
  state => {
    if (!props.nativeHtml) return
    if (state === 'crashed') preview.markNativeCrashed()
    else if (state === 'error') preview.markNativeError()
  },
  { immediate: true },
)

watch(
  preview.state,
  state => {
    emit('state-change', state)
    emit('workbench-event', { type: 'preview-state-change', payload: state })
  },
  { immediate: true },
)

function emitArtifactEvent(
  type: 'artifact-download' | 'artifact-external-open',
  legacyType: 'download' | 'external-open',
) {
  if (legacyType === 'download') emit('download', props.artifact)
  else emit('external-open', props.artifact)
  emit('workbench-event', { type, payload: props.artifact })
}

const isRenderable = computed(() =>
  preview.state.value === 'ready'
  || preview.state.value === 'ready-with-warnings'
  || preview.state.value === 'missing-resource')

const opaqueOfflinePreview = computed(() =>
  props.previewSandboxProfile === 'opaque-offline'
  || props.previewNetworkAllowed === false)

const fullHtmlPreview = computed(() =>
  props.previewMode === 'full'
  && !opaqueOfflinePreview.value)

const showOfflineWebLimits = computed(() =>
  !props.nativeHtml
  && !fullHtmlPreview.value
  && preview.kind.value === 'html')

const htmlSandbox = computed(() => fullHtmlPreview.value
  ? 'allow-scripts allow-same-origin allow-forms allow-modals allow-pointer-lock allow-presentation'
  : 'allow-scripts')

const htmlPermissions = computed(() => fullHtmlPreview.value
  ? 'camera; microphone; geolocation; clipboard-read; clipboard-write; fullscreen; display-capture'
  : '')

const pdfFrameUrl = computed(() => {
  const url = preview.objectUrl.value
  return url ? `${url}#zoom=page-width&view=FitH` : ''
})

function onPreviewFrameMessage(event: MessageEvent) {
  if (
    event.data !== ARTIFACT_PREVIEW_ESCAPE_MESSAGE
    || event.source !== previewFrameRef.value?.contentWindow
  ) return
  requestWorkbenchCollapse()
}

function requestWorkbenchCollapse() {
  emit('workbench-event', { type: 'request-collapse' })
}

async function reloadPreview() {
  if (props.previewBlocked) {
    emit('workbench-event', { type: 'preview-retry' })
    return
  }
  await preview.reload()
  if (preview.kind.value === 'html' && props.previewLaunchUrl) {
    htmlFrameGeneration.value += 1
  }
}

onMounted(() => window.addEventListener('message', onPreviewFrameMessage))
onBeforeUnmount(() => window.removeEventListener('message', onPreviewFrameMessage))

const isFailureState = computed(() =>
  preview.state.value === 'crashed'
  || preview.state.value === 'error'
  || preview.state.value === 'offline'
  || preview.state.value === 'unsupported')

const failureTitle = computed(() => {
  if (preview.state.value === 'offline') return t('workbench.artifactPreview.offline')
  if (preview.state.value === 'crashed') return t('workbench.artifactPreview.crashed')
  if (preview.errorCode.value === 'too-large') return t('workbench.artifactPreview.tooLarge')
  if (preview.errorCode.value === 'unsupported') return t('workbench.artifactPreview.unsupported')
  if (preview.errorCode.value === 'integrity-error') {
    return t('workbench.artifactPreview.integrityError')
  }
  return t('chat.previewFailed')
})

const failureDetail = computed(() => {
  if (preview.errorCode.value === 'preview-blocked' && props.previewErrorMessage) {
    return props.previewErrorMessage
  }
  if (preview.errorCode.value === 'integrity-error') {
    return t('workbench.artifactPreview.integrityErrorDetail')
  }
  if (preview.errorCode.value === 'invalid-content') {
    return t('workbench.artifactPreview.invalidContent')
  }
  if (preview.errorCode.value === 'too-large') {
    return t('workbench.artifactPreview.tooLargeDetail')
  }
  if (preview.errorCode.value === 'unsupported') {
    return t('workbench.artifactPreview.unsupportedDetail')
  }
  if (preview.state.value === 'offline') {
    return t('workbench.artifactPreview.offlineDetail')
  }
  if (preview.state.value === 'crashed') {
    return t('workbench.artifactPreview.crashedDetail')
  }
  return t('workbench.artifactPreview.failedDetail')
})

defineExpose({
  kind: preview.kind,
  reload: reloadPreview,
  state: preview.state,
})
</script>

<style scoped>
.artifact-preview {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  background: var(--bg-surface);
  color: var(--text);
}

.artifact-preview__toolbar {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: 10px;
  min-height: 54px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}

.artifact-preview__file-icon {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  justify-content: center;
}

.artifact-preview__identity {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.artifact-preview__title,
.artifact-preview__meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-preview__title {
  font-size: var(--fs-sm);
  font-weight: 650;
}

.artifact-preview__meta {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.artifact-preview__actions,
.artifact-preview__status-actions {
  align-items: center;
  display: inline-flex;
  gap: 4px;
}

.artifact-preview__action {
  min-height: 30px;
  min-width: 30px;
}

.artifact-preview__notice {
  align-items: flex-start;
  display: flex;
  flex: 0 0 auto;
  gap: 8px;
  margin: 0;
  padding: 9px 14px;
  background: color-mix(in srgb, var(--warn) 9%, var(--bg-surface));
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.artifact-preview__notice .icon {
  color: var(--warn);
  margin-top: 2px;
}

.artifact-preview__viewport {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow: auto;
  position: relative;
}

.artifact-preview__status {
  align-items: center;
  align-self: center;
  color: var(--text-dim);
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin: auto;
  max-width: 360px;
  padding: 24px;
  text-align: center;
}

.artifact-preview__status--error {
  flex-direction: column;
}

.artifact-preview__status-detail {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.artifact-preview__status-actions {
  justify-content: center;
  margin-top: 6px;
}

.artifact-preview__loading-line {
  height: 2px;
  width: 54px;
  overflow: hidden;
  position: relative;
  background: var(--bg-hover);
}

.artifact-preview__loading-line::after {
  animation: artifact-preview-loading 1.4s ease-in-out infinite;
  background: var(--accent);
  content: '';
  inset: 0;
  position: absolute;
  transform: translateX(-110%);
}

.artifact-preview__image {
  align-self: center;
  display: block;
  height: auto;
  margin: auto;
  max-height: 100%;
  max-width: 100%;
  object-fit: contain;
  padding: 16px;
}

.artifact-preview__frame,
.artifact-preview__native-slot {
  border: 0;
  flex: 1;
  height: 100%;
  min-height: 0;
  min-width: 0;
  width: 100%;
}

.artifact-preview__frame--pdf {
  background: var(--bg);
}

.artifact-preview__pdf-stack {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  width: 100%;
}

.artifact-preview__pdf-stack .artifact-preview__frame--pdf {
  height: auto;
}

.artifact-preview__frame-exit {
  align-self: flex-end;
  block-size: 1px;
  border: 0;
  clip-path: inset(50%);
  inline-size: 1px;
  margin: 0;
  opacity: 0;
  overflow: hidden;
  padding: 0;
  transition:
    opacity var(--dur-fast) var(--ease-standard);
}

.artifact-preview__frame-exit:focus,
.artifact-preview__frame-exit:focus-visible {
  block-size: auto;
  border: 1px solid transparent;
  clip-path: none;
  inline-size: auto;
  margin: 8px 12px;
  opacity: 1;
  overflow: visible;
  padding: 7px 14px;
}

.artifact-preview__frame--html,
.artifact-preview__native-slot {
  background: var(--bg-surface);
}

.artifact-preview__native-slot {
  align-items: center;
  display: flex;
  justify-content: center;
  position: relative;
}

.artifact-preview__native-fallback {
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

.artifact-preview__markdown {
  box-sizing: border-box;
  margin: 0 auto;
  max-width: 820px;
  padding: 24px 28px 64px;
  width: 100%;
}

.artifact-preview__text {
  box-sizing: border-box;
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.65;
  margin: 0;
  min-height: 100%;
  overflow-wrap: anywhere;
  padding: 20px 24px 64px;
  tab-size: 2;
  white-space: pre-wrap;
  width: 100%;
}

@keyframes artifact-preview-loading {
  0% { transform: translateX(-110%); }
  100% { transform: translateX(110%); }
}

@media (prefers-reduced-motion: reduce) {
  .artifact-preview__loading-line::after {
    animation: none;
    opacity: 0.55;
    transform: none;
  }

  .artifact-preview__frame-exit {
    transition: none;
  }
}

@media (max-width: 600px) {
  .artifact-preview__toolbar {
    min-height: 50px;
    padding-inline: 10px;
  }

  .artifact-preview__markdown,
  .artifact-preview__text {
    padding: 18px 16px 48px;
  }
}
</style>
