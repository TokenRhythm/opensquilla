<template>
  <div>
    <WorkspaceFilePreview
      :file="selectedWorkspaceFile"
      :session-key="sessionKey || ''"
      :scope="workspaceFileScope"
      :workbench-available="workspaceWorkbenchAvailable"
      :native-actions-available="nativeWorkspaceActionsAvailable"
      :native-reveal-label="isMacPlatform() ? t('resourceActions.revealFinder') : t('resourceActions.reveal')"
      @close="selectedWorkspaceFile = null"
      @action="handleWorkspaceFileAction"
    />
    <WorkspaceFileActionsMenu
      ref="workspaceFileMenu"
      :session-key="sessionKey || ''"
      :workbench-available="workspaceWorkbenchAvailable"
      :native-open-available="nativeWorkspaceActionsAvailable"
      :native-reveal-available="nativeWorkspaceActionsAvailable"
      :native-reveal-label="isMacPlatform() ? t('resourceActions.revealFinder') : t('resourceActions.reveal')"
      :copy-contents-available="true"
      @action="handleWorkspaceFileAction"
    />
    <ResourceActionsMenu ref="fileMenu" :session-key="sessionKey" @open="emit('openResource', $event)" />
    <div v-if="part.html" ref="rootEl" class="msg-ai-text" v-html="part.html" />
    <p v-if="unmentionedPreviews.length" class="workspace-preview-fallback">
      <span>{{ t('workbench.artifactDocument.preview') }}: </span>
      <template v-for="(preview, index) in unmentionedPreviews" :key="workspacePreviewIdentity(preview)">
        <span v-if="index" aria-hidden="true"> · </span>
        <button
          type="button"
          role="link"
          class="workspace-file-link"
          :title="previewLabel(preview)"
          :aria-label="previewLabel(preview)"
          @click.stop="emit('workspacePreview', preview)"
          @contextmenu="showFileMenu($event, preview)"
          @keydown="showFileMenu($event, preview)"
        >{{ workspacePreviewLabel(preview, workspacePreviews) }}</button>
      </template>
    </p>
    <p v-if="missingCitationLabel" class="msg-ai-citation-warning">
      Some citations do not map to available sources: {{ missingCitationLabel }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { getActivePinia } from 'pinia'
import WorkspaceFilePreview from '@/components/chat/WorkspaceFilePreview.vue'
import WorkspaceFileActionsMenu from '@/components/chat/WorkspaceFileActionsMenu.vue'
import { WORKSPACE_FILES_KEY, type WorkspaceFile } from '@/modules/workspaceFiles'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { clearWorkspaceFileLinks, decorateWorkspaceFileLinks, workspaceFileCandidates } from '@/utils/chat/workspaceFiles'
import ResourceActionsMenu from '@/components/ResourceActionsMenu.vue'
import type { ArtifactPayload } from '@/types/artifacts'
import { useI18n } from 'vue-i18n'
import type { ChatPart, SourcePart } from '@/types/parts'
import { decorateCitations } from '@/utils/chat/citations'
import {
  decorateWorkspacePreviewLinks,
  workspacePreviewLabel,
  workspacePreviewIdentity,
  workspacePreviewOpenAction,
  type WorkspacePreviewLink,
} from '@/utils/chat/workspacePreviews'
import { copyTextWithFallback, downloadBlob, isMacPlatform } from '@/utils/browser'
import { decorateCodeBlocks } from '@/utils/chat/codeBlockChrome'
import { decorateMarkdownImages } from '@/utils/markdown/imageLightbox'
import { renderMermaidBlocks } from '@/utils/markdown/mermaidRenderer'
import { usePlatform } from '@/platform'
import { requestBrowserWorkbenchOpen } from '@/workbench/browserItems'
import { useWorkbenchStore } from '@/workbench/store'
import { createResolvedWorkspaceFileItem } from '@/workbench/workspaceFileItems'
import { useToasts } from '@/composables/useToasts'

const props = withDefaults(
  defineProps<{
    part: Extract<ChatPart, { type: 'text' }>
    sources?: SourcePart[]
    workspacePreviews?: WorkspacePreviewLink[]
    sessionKey?: string
    preferWorkspaceWorkbench?: boolean
  }>(),
  { sources: () => [], workspacePreviews: () => [], preferWorkspaceWorkbench: false },
)

const emit = defineEmits<{
  citation: [sourceId: number]
  workspacePreview: [preview: WorkspacePreviewLink]
  openResource: [artifact: ArtifactPayload]
}>()

const { t } = useI18n()
const { pushToast } = useToasts()
const platform = usePlatform()
// TextPart is also rendered in lightweight chat tests and in older clients
// without a Workbench provider. Resolve the store only for the Workbench path
// so the fallback preview remains usable in those hosts.
const pinia = props.preferWorkspaceWorkbench ? getActivePinia() : undefined
const workbench = pinia ? useWorkbenchStore(pinia) : null
const workspaceFiles = inject(WORKSPACE_FILES_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const selectedWorkspaceFile = shallowRef<WorkspaceFile | null>(null)
const workspaceWorkbenchAvailable = computed(() => Boolean(
  props.preferWorkspaceWorkbench && workbench,
))
const workspaceFileMenu = ref<InstanceType<typeof WorkspaceFileActionsMenu> | null>(null)
const nativeWorkspaceActionsAvailable = computed(() => platform.id === 'desktop'
  && Boolean(platform.files.workspaceFileAction)
  && Boolean(platform.gateway.getConnection)
  && Boolean(gateway?.isLocalOwner && gateway.isAvailable))
const workspaceFileScope = computed(() => JSON.stringify([
  props.sessionKey, gateway?.deliveryIdentity, gateway?.subscriptionEpoch,
  gateway?.isLocalOwner, gateway?.isAvailable,
]))
let fileRequest: AbortController | null = null
let fileSignature = ''
let fileResolutionHtml = ''
let resolvedFiles: WorkspaceFile[] = []

function applyWorkspaceFiles(root: HTMLElement, scope: string) {
  decorateWorkspaceFileLinks(root, resolvedFiles, file => {
    if (scope === workspaceFileScope.value) openWorkspaceFile(file)
  }, file => t('chat.openTitle', { title: file.path }), (event, file) => {
    void workspaceFileMenu.value?.show(event, file)
  }, file => `${t('resourceActions.more')} · ${file.path}`)
}

function openWorkspaceFile(file: WorkspaceFile) {
  if (file.kind === 'text' && workspaceWorkbenchAvailable.value && props.sessionKey && workbench?.openItem(
    createResolvedWorkspaceFileItem(props.sessionKey, file),
  )) { selectedWorkspaceFile.value = null; return }
  selectedWorkspaceFile.value = file
}

let fileActionRequest: AbortController | null = null
async function handleWorkspaceFileAction(action: string, file: WorkspaceFile) {
  const sessionKey = props.sessionKey
  const scope = workspaceFileScope.value
  if (!sessionKey || !gateway?.isLocalOwner || !gateway.isAvailable) return
  fileActionRequest?.abort()
  const request = new AbortController()
  fileActionRequest = request
  const current = () => !request.signal.aborted && scope === workspaceFileScope.value
  try {
    if (action === 'open') { openWorkspaceFile(file); return }
    if (action === 'copy-path') {
      await copyTextWithFallback(file.path)
      if (current()) pushToast(t('workspaceReference.copied'), { tone: 'ok' })
      return
    }
    if (action === 'native-open' || action === 'reveal') {
      const nativeAction = platform.files.workspaceFileAction
      if (!file.nativeActions || !nativeWorkspaceActionsAvailable.value || !nativeAction) throw new Error('unavailable')
      const connection = await platform.gateway.getConnection?.()
      if (!current()) return
      if (!connection || connection.status !== 'ready' || !connection.instanceId) throw new Error('unavailable')
      const result = await nativeAction({
        gatewayInstanceId: connection.instanceId,
        sessionKey,
        path: file.path,
        workspaceBinding: file.workspaceBinding,
        action: action === 'native-open' ? 'open' : 'reveal',
      })
      if (!result?.ok) throw new Error('failed')
      return
    }
    if (!workspaceFiles) throw new Error('unavailable')
    const blob = await workspaceFiles.read(sessionKey, file, request.signal)
    if (!current()) return
    if (action === 'copy-contents') {
      if (file.kind !== 'text') throw new Error('unsupported')
      const content = new TextDecoder('utf-8', { fatal: true }).decode(await blob.arrayBuffer())
      if (!current()) return
      if (content.includes('\0')) throw new Error('unsupported')
      await copyTextWithFallback(content)
      if (current()) pushToast(t('workspaceReference.copied'), { tone: 'ok' })
    } else if (action === 'download') {
      if (platform.files.saveArtifact) {
        const data = await blob.arrayBuffer()
        if (!current()) return
        await platform.files.saveArtifact({ data, name: file.name, mime: blob.type })
      } else { downloadBlob(blob, file.name) }
    }
  } catch {
    if (current()) pushToast(t('resourceActions.failed'), { tone: 'danger' })
  } finally {
    if (fileActionRequest === request) fileActionRequest = null
  }
}

function decorateWorkspaceFiles(root: HTMLElement) {
  const paths = workspaceFileCandidates(root)
  const scope = workspaceFileScope.value
  const signature = JSON.stringify([scope, paths])
  if (signature === fileSignature && (fileRequest || fileResolutionHtml === props.part.html
    || paths.every(path => resolvedFiles.some(file => file.requestedPath === path)))) {
    applyWorkspaceFiles(root, scope)
    return
  }
  fileSignature = signature
  fileResolutionHtml = props.part.html
  fileRequest?.abort()
  fileRequest = null
  resolvedFiles = []
  if (!workspaceFiles || !props.sessionKey || !gateway?.isLocalOwner || !gateway.isAvailable || !paths.length) return
  const request = new AbortController()
  fileRequest = request
  const sessionKey = props.sessionKey
  // A single response must not make claims about arbitrary unrequested paths.
  void (async () => {
    try {
      const batches: WorkspaceFile[] = []
      for (let offset = 0; offset < paths.length; offset += 32) {
        const files = await workspaceFiles.resolve(sessionKey, paths.slice(offset, offset + 32), request.signal)
        if (request.signal.aborted || scope !== workspaceFileScope.value) return
        batches.push(...files)
      }
      if (request.signal.aborted || root !== rootEl.value) return
      resolvedFiles = batches
      applyWorkspaceFiles(root, scope)
    } catch { /* Missing, inaccessible, or unsupported files stay plain text. */ }
    finally {
      if (fileRequest === request) {
        fileRequest = null
        // A later body may announce a file created after the first mention.
        if (!request.signal.aborted && fileResolutionHtml !== props.part.html) decorate()
      }
    }
  })()
}
const fileMenu = ref<InstanceType<typeof ResourceActionsMenu> | null>(null)
function showFileMenu(event: MouseEvent | KeyboardEvent, preview: WorkspacePreviewLink) {
  void fileMenu.value?.show(event, workspacePreviewOpenAction(preview, props.sessionKey))
}
const rootEl = ref<HTMLDivElement | null>(null)
const missingCitationIds = ref<number[]>([])
const mentionedPreviewIds = ref<string[]>([])
const unmentionedPreviews = computed(() => props.workspacePreviews.filter(
  preview => !preview.previewPagePath
    && !mentionedPreviewIds.value.includes(workspacePreviewIdentity(preview)),
))

function previewLabel(preview: WorkspacePreviewLink): string {
  return t('chat.openTitle', { title: workspacePreviewLabel(preview, props.workspacePreviews) })
}

const missingCitationLabel = computed(() =>
  missingCitationIds.value.map(id => `[${id}]`).join(', '),
)

function labelFor(sourceId: number): string {
  const source = props.sources[sourceId - 1]
  return source ? source.title || source.domain : ''
}

function mermaidLabels() {
  return {
    zoomIn: t('chat.mermaid.zoomIn'),
    zoomOut: t('chat.mermaid.zoomOut'),
    reset: t('chat.mermaid.reset'),
    copyCode: t('chat.mermaid.copyCode'),
    copied: t('chat.copied'),
    copyFailed: t('chat.toast.copyFailed'),
    exportPng: t('chat.mermaid.exportPng'),
    loadFailed: t('chat.mermaid.loadFailed'),
    viewSource: t('chat.mermaid.viewSource'),
    syntaxErrorAt: (line: number, excerpt: string) => t('chat.mermaid.syntaxErrorAt', { line, excerpt }),
  }
}

// After `v-html` has applied the sanitized body, upgrade any `[n]` that maps to
// a real source into a focusable citation pill. The pass works on already-clean
// text nodes only (createElement/textContent — never innerHTML), so it adds no
// HTML sink and re-runs idempotently when the body re-renders during streaming.
function decorate() {
  const root = rootEl.value
  if (!root) {
    mentionedPreviewIds.value = []
    return
  }
  clearWorkspaceFileLinks(root)
  mentionedPreviewIds.value = decorateWorkspacePreviewLinks(
    root, props.workspacePreviews, preview => emit('workspacePreview', preview), previewLabel, showFileMenu,
  )
  missingCitationIds.value = []
  decorateCitations(root, props.sources, {
    onActivate: n => emit('citation', n),
    labelFor,
    onMissingCitations: ids => {
      missingCitationIds.value = props.sources.length > 0 ? ids : []
    },
  })
  decorateCodeBlocks(root, {
    copy: t('chat.copy'),
    copied: t('chat.copied'),
    copyFailed: t('chat.toast.copyFailed'),
  })
  decorateMarkdownImages(root, t('chat.closePreview'))
  if (root.querySelector('code.language-mermaid')) {
    void renderMermaidBlocks(root, mermaidLabels()).catch(() => { /* failed fences keep their source */ })
  }
  decorateBrowserLinks()
  decorateWorkspaceFiles(root)
}

function decorateBrowserLinks() {
  const root = rootEl.value
  if (
    !root
    || platform.id !== 'desktop'
    || !platform.capabilities.hasNativeWorkbenchSurfaces
  ) return
  for (const anchor of root.querySelectorAll<HTMLAnchorElement>('a[href]')) {
    if (!/^https?:/i.test(anchor.href)) continue
    const next = anchor.nextElementSibling
    if (next?.classList.contains('link-side-browser-btn')) continue
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'link-side-browser-btn'
    button.textContent = '↗'
    const label = t('workbench.browser.openSide')
    button.title = label
    button.setAttribute('aria-label', label)
    button.addEventListener('click', event => {
      event.preventDefault()
      event.stopPropagation()
      requestBrowserWorkbenchOpen(anchor.href)
    })
    anchor.insertAdjacentElement('afterend', button)
  }
}

onMounted(decorate)
watch(() => props.part.html, decorate, { flush: 'post' })
watch(() => props.sources, decorate, { flush: 'post' })
watch(() => props.workspacePreviews, decorate, { flush: 'post' })
watch(workspaceFileScope, () => {
  fileActionRequest?.abort()
  fileActionRequest = null
  fileRequest?.abort()
  fileRequest = null
  fileSignature = ''
  resolvedFiles = []
  workspaceFileMenu.value?.close()
  selectedWorkspaceFile.value = null
  if (rootEl.value) clearWorkspaceFileLinks(rootEl.value)
  decorate()
}, { flush: 'sync' })
onBeforeUnmount(() => { fileRequest?.abort(); fileActionRequest?.abort() })
</script>

<style scoped>
.msg-ai-text {
  font-size: var(--fs-md);
  line-height: 1.7;
  color: var(--text);
  word-break: break-word;
  margin-bottom: var(--sp-3);
}

.workspace-preview-fallback {
  margin: 0.375rem 0 0.5rem;
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.workspace-file-link,
.msg-ai-text :deep(.workspace-file-link) {
  display: inline;
  padding: 0;
  border: 0;
  background: transparent;
  font: inherit;
  color: var(--accent);
  text-align: left;
  text-decoration: underline;
  text-underline-offset: 0.18em;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.workspace-file-entry,
.msg-ai-text :deep(.workspace-file-entry) {
  display: inline;
}

.workspace-file-action-trigger,
.msg-ai-text :deep(.workspace-file-action-trigger) {
  padding: 0 0.2rem;
  background: var(--bg-hover);
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--accent);
  font: inherit;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.35rem;
  height: 1.35rem;
  margin-inline-start: 0.45rem;
  line-height: 1;
  vertical-align: middle;
  border-radius: 50%;
  box-shadow: 0 0 0 2px var(--bg-surface);
}
.workspace-file-action-trigger:focus-visible,
.msg-ai-text :deep(.workspace-file-action-trigger:focus-visible) {
  color: var(--accent);
  background: var(--bg-surface);
  border-color: var(--border-focus);
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}
.workspace-file-action-trigger:hover,
.msg-ai-text :deep(.workspace-file-action-trigger:hover) {
  color: var(--accent);
  background: var(--bg-surface);
  border-color: var(--accent);
}
.workspace-file-action-trigger :deep(svg),
.msg-ai-text :deep(.workspace-file-action-trigger svg) { width: 15px; height: 15px; }

.workspace-file-link:focus-visible,
.msg-ai-text :deep(.workspace-file-link:focus-visible) {
  outline: 2px solid var(--border-focus);
  outline-offset: 3px;
  border-radius: var(--radius-sm);
}

.msg-ai-text :deep(.workspace-file-link code) {
  padding: 0;
  background: transparent;
  color: inherit;
}

.msg-ai-text :deep(p) { margin: 0.375rem 0; }
.msg-ai-text :deep(p:first-child) { margin-top: 0; }
.msg-ai-text :deep(ul), .msg-ai-text :deep(ol) { margin: 0.375rem 0; padding-left: 1.25rem; }
.msg-ai-text :deep(li) { margin: 0.125rem 0; }
.msg-ai-text :deep(code) {
  background: var(--bg-hover);
  padding: 0.0625rem 0.25rem;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  color: var(--text-muted);
}
.msg-ai-text :deep(pre) {
  background: var(--code-block-bg);
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-md);
  padding: 0.625rem;
  overflow-x: auto;
  margin: 0.375rem 0;
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 4%, transparent);
}
.msg-ai-text :deep(pre.code-block) {
  /* Chrome for fenced code (header gradient, language label, copy button)
     lives in styles/chat-markdown.css so streaming blocks and artifact
     previews render the identical surface. */
  position: relative;
}

.msg-ai-text :deep(.link-side-browser-btn) {
  display: inline-flex;
  width: 1.35rem;
  height: 1.35rem;
  align-items: center;
  justify-content: center;
  margin-left: 0.2rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  line-height: 1;
  vertical-align: text-bottom;
}

.msg-ai-text :deep(.link-side-browser-btn:hover),
.msg-ai-text :deep(.link-side-browser-btn:focus-visible) {
  background: var(--bg-hover);
  color: var(--accent);
}

.msg-ai-text :deep(pre code) {
  background: transparent;
  padding: 0;
}

.msg-ai-citation-warning {
  margin: 0.25rem 0 0.5rem;
  font-size: 0.75rem;
  line-height: 1.4;
  color: var(--text-muted);
}

/* Citation pills are injected outside Vue's template (built by decorateCitations
   with createElement), so the scoped data-v hash never lands on them — target
   them through :deep, the same mechanism the markdown elements above use. */
.msg-ai-text :deep(.citation-pill) {
  display: inline-flex;
  align-items: center;
  padding: 0 0.25rem;
  margin: 0 0.0625rem;
  font: inherit;
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  line-height: 1.2;
  vertical-align: baseline;
  color: var(--text-muted);
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition), background var(--transition), border-color var(--transition);
}

.msg-ai-text :deep(.citation-pill:hover) {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-hover));
}

.msg-ai-text :deep(.citation-pill:focus-visible) {
  outline: none;
  box-shadow: var(--focus-ring);
}

@media (prefers-reduced-motion: reduce) {
  .msg-ai-text :deep(.citation-pill) {
    transition: none;
  }
}
</style>
