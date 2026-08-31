<script setup lang="ts">
/**
 * Workspace file preview panel (workbench kind: 'file').
 *
 * Renders a bounded text read from GET /api/v1/files/content in a read-only
 * Monaco editor; binary and over-limit files degrade to a metadata notice.
 */
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type * as Monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'

import Icon from '@/components/Icon.vue'
import type { FileTreeWorkspace } from '@/stores/fileTree'
import { requestWorkspaceFileAttach } from '@/workbench/workspaceFileAttachEvent'
import { WORKSPACE_FILES_KEY, type WorkspaceFiles } from '@/modules/workspaceFiles'

interface MonacoEnvironmentShape {
  MonacoEnvironment?: {
    getWorker(moduleId: string, label: string): Worker
  }
}

const monacoGlobal = globalThis as unknown as MonacoEnvironmentShape
monacoGlobal.MonacoEnvironment ||= {
  getWorker(_moduleId: string, label: string) {
    return label === 'html' || label === 'handlebars' || label === 'razor'
      ? new EditorWorker()
      : new EditorWorker()
  },
}

const props = defineProps<{
  workspace: FileTreeWorkspace
  path: string
  /** Absolute root path, for the copy toolbar hint. */
  rootPath: string
  /** Per-open counter from the workbench item payload. Bumped every time
   *  the tree opens this file, so re-clicking the same file retriggers a
   *  reload instead of being a silent no-op. */
  openNonce?: number
}>()

const { t } = useI18n()

const injectedWorkspaceFiles = inject(WORKSPACE_FILES_KEY)
if (!injectedWorkspaceFiles) throw new Error('WorkspaceFiles was not provided')
const workspaceFiles: WorkspaceFiles = injectedWorkspaceFiles

const state = ref<'loading' | 'ready' | 'binary' | 'error'>('loading')
const errorText = ref('')
const truncated = ref(false)
const size = ref<number | null>(null)
const content = ref('')

const editorElement = ref<HTMLElement | null>(null)
const monacoFailed = ref(false)
let editor: Monaco.editor.IStandaloneCodeEditor | null = null
let monacoModule: typeof Monaco | null = null
let editorDisposables: Monaco.IDisposable[] = []
let unmounted = false

// Selection context menu (right-click with a non-empty selection).
const ctxMenu = ref<{ x: number; y: number; text: string } | null>(null)

/** `README.md` → `README.selection.txt`; extension-less stays clean. */
function snippetName(path: string): string {
  const base = path.split('/').pop() || path
  const dot = base.lastIndexOf('.')
  const stem = dot > 0 ? base.slice(0, dot) : base
  return `${stem}.selection.txt`
}

function closeCtxMenu() {
  ctxMenu.value = null
}

function onWindowMouseDown(event: MouseEvent) {
  if (!ctxMenu.value) return
  const target = event.target as Element | null
  if (target?.closest('[data-ws-file-ctx-menu]')) return
  closeCtxMenu()
}

function onWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeCtxMenu()
}

function attachSelectedText(text: string) {
  requestWorkspaceFileAttach({
    workspaceId: props.workspace.id,
    workspacePath: props.workspace.path,
    path: props.path,
    name: snippetName(props.path),
    content: text,
  })
}

function copySelectionText(text: string) {
  void navigator.clipboard?.writeText(text).catch(() => {
    /* clipboard unavailable (permissions) — no-op */
  })
}

function onCtxCopy() {
  if (ctxMenu.value) copySelectionText(ctxMenu.value.text)
  closeCtxMenu()
}

function onCtxAttach() {
  if (ctxMenu.value) attachSelectedText(ctxMenu.value.text)
  closeCtxMenu()
}

/**
 * Own the context menu at the DOM layer. Monaco's native menu is disabled
 * outright (`contextmenu: false` — its ContextMenuController registers
 * earlier and shows the menu before any editor.onContextMenu listener can
 * react, and the two stacked menus were the original bug), so this
 * container listener is the only menu source. With a non-empty selection we
 * show our 复制/添加到对话 menu; otherwise nothing pops up.
 */
function registerEditorContextMenu(created: Monaco.editor.IStandaloneCodeEditor, container: HTMLElement) {
  const onContextMenu = (event: MouseEvent) => {
    // Monaco's option already suppresses its own menu and the browser menu
    // inside the editor surface; preventDefault here covers the rest.
    event.preventDefault()
    const selection = created.getSelection()
    const model = created.getModel()
    if (!selection || selection.isEmpty() || !model) return
    const text = model.getValueInRange(selection)
    if (!text) return
    // Rough two-item menu size; clamp keeps the menu inside the viewport.
    const x = Math.min(event.clientX, window.innerWidth - 200)
    const y = Math.min(event.clientY, window.innerHeight - 96)
    ctxMenu.value = { x, y, text }
  }
  container.addEventListener('contextmenu', onContextMenu, true)
  editorDisposables.push({
    dispose: () => container.removeEventListener('contextmenu', onContextMenu, true),
  })
  editorDisposables.push(created.onDidScrollChange(closeCtxMenu))
}

function languageFor(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  switch (ext) {
    case 'ts':
    case 'tsx':
      return 'typescript'
    case 'js':
    case 'jsx':
    case 'mjs':
    case 'cjs':
      return 'javascript'
    case 'vue':
      return 'html'
    case 'json':
      return 'json'
    case 'py':
      return 'python'
    case 'rs':
      return 'rust'
    case 'go':
      return 'go'
    case 'java':
      return 'java'
    case 'c':
    case 'h':
      return 'c'
    case 'cpp':
    case 'cc':
    case 'cxx':
      return 'cpp'
    case 'cs':
      return 'csharp'
    case 'css':
    case 'scss':
    case 'less':
      return 'css'
    case 'html':
    case 'htm':
      return 'html'
    case 'md':
    case 'mdx':
      return 'markdown'
    case 'yml':
    case 'yaml':
      return 'yaml'
    case 'toml':
    case 'ini':
    case 'conf':
      return 'ini'
    case 'sh':
    case 'bash':
    case 'ps1':
      return 'shell'
    case 'sql':
      return 'sql'
    case 'xml':
    case 'svg':
      return 'xml'
    default:
      return 'plaintext'
  }
}

async function load() {
  state.value = 'loading'
  errorText.value = ''
  truncated.value = false
  size.value = null
  content.value = ''
  try {
    const body = await workspaceFiles.readFile(props.workspace.id, props.path)
    if (unmounted) return
    size.value = body.size
    truncated.value = body.truncated
    if (body.binary) {
      state.value = 'binary'
      return
    }
    content.value = body.content ?? ''
    state.value = 'ready'
    // The editor container only exists in the ready branch; after this tick
    // it is rendered, so create Monaco on first use and push the content in.
    await nextTick()
    await ensureEditor()
    syncEditor()
  } catch (caught) {
    if (unmounted) return
    state.value = 'error'
    errorText.value = caught instanceof Error ? caught.message : String(caught)
  }
}

/**
 * Create the Monaco editor once, when its container is mounted. Creation
 * cannot happen in onMounted: at that point state is 'loading' and the
 * ready-branch container is not rendered yet, so the ref is still null.
 */
async function ensureEditor() {
  if (editor || monacoFailed.value || unmounted) return
  if (!editorElement.value) return
  let monaco: typeof Monaco
  try {
    monaco = await import('monaco-editor')
  } catch {
    // Monaco failed to load: the plain-text fallback below stays available.
    monacoFailed.value = true
    return
  }
  monacoModule = monaco
  const container = editorElement.value
  if (unmounted || !container || editor) return
  editor = monaco.editor.create(container, {
    value: '',
    language: 'plaintext',
    theme: 'vs-dark',
    automaticLayout: true,
    minimap: { enabled: false },
    fontSize: 13,
    lineNumbersMinChars: 3,
    padding: { top: 10, bottom: 10 },
    scrollBeyondLastLine: false,
    tabSize: 2,
    readOnly: true,
    renderLineHighlight: 'none',
    // Kill Monaco's own context menu on every path (mouse and Shift+F10);
    // the container-level DOM listener below owns the menu instead.
    contextmenu: false,
  })
  registerEditorContextMenu(editor, container)
}

function formatBytes(value: number | null): string {
  if (value === null) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

const sizeLabel = computed(() => formatBytes(size.value))
const language = computed(() => languageFor(props.path))
const fileName = computed(() => props.path.split('/').pop() || props.path)

onMounted(() => {
  window.addEventListener('mousedown', onWindowMouseDown)
  window.addEventListener('keydown', onWindowKeydown)
  void load()
})

function syncEditor() {
  if (!editor || state.value !== 'ready') return
  const model = editor.getModel()
  if (!model) return
  const lang = language.value
  if (model.getLanguageId() !== lang && monacoModule) {
    // The panel is reused across files, so the language must follow the path.
    monacoModule.editor.setModelLanguage(model, lang)
  }
  if (model.getValue() !== content.value) {
    model.setValue(content.value)
  }
}

// Reload when the panel is reused for another file or workspace, or when
// the tree re-opens the same file (bumped openNonce). state must stay out
// of the dependency list: load() itself mutates state, so watching it
// would re-trigger this handler in an endless fetch loop.
// Reload when the panel is reused for another file or workspace, or when
// the tree re-opens the same file (bumped openNonce). The array-of-getters
// form is deliberate: a single getter returning an array is compared by
// reference (always fresh), so every parent re-render — which rebuilds the
// workspace prop object — would re-trigger load() and flicker the panel.
// state must stay out of the sources: load() itself mutates state, so
// watching it would re-trigger this handler in an endless fetch loop.
watch(
  [() => props.path, () => props.workspace.id, () => props.openNonce],
  () => {
    void load()
  },
)

function copyAbsolutePath() {
  const absolute = `${props.rootPath.replace(/[\\/]+$/, '')}/${props.path}`
  void navigator.clipboard?.writeText(absolute).catch(() => {
    /* clipboard unavailable — no-op */
  })
}

onBeforeUnmount(() => {
  unmounted = true
  window.removeEventListener('mousedown', onWindowMouseDown)
  window.removeEventListener('keydown', onWindowKeydown)
  for (const disposable of editorDisposables) disposable.dispose()
  editorDisposables = []
  editor?.dispose()
  editor = null
})

defineExpose({ reload: load })
</script>

<template>
  <div class="ws-file-preview" data-testid="workspace-file-preview">
    <div class="ws-file-preview__toolbar">
      <span class="ws-file-preview__file" :title="path">
        <bdi dir="auto">{{ fileName }}</bdi>
      </span>
      <span v-if="sizeLabel" class="ws-file-preview__size">{{ sizeLabel }}</span>
      <span v-if="truncated" class="ws-file-preview__notice">
        {{ t('fileTree.previewTruncated') }}
      </span>
      <button
        type="button"
        class="ws-file-preview__copy"
        :title="t('fileTree.copyPath')"
        :aria-label="t('fileTree.copyPath')"
        @click="copyAbsolutePath"
      >
        <Icon name="copy" :size="13" />
      </button>
    </div>

    <div v-if="state === 'loading'" class="ws-file-preview__state">
      {{ t('fileTree.loading') }}
    </div>
    <div v-else-if="state === 'error'" class="ws-file-preview__state" role="alert">
      <p>{{ errorText }}</p>
      <button type="button" class="ws-file-preview__retry" @click="load()">
        {{ t('fileTree.retry') }}
      </button>
    </div>
    <div v-else-if="state === 'binary'" class="ws-file-preview__state">
      <p>{{ t('fileTree.binaryNotPreviewable') }}</p>
      <span v-if="sizeLabel" class="ws-file-preview__size">{{ sizeLabel }}</span>
    </div>
    <!-- The editor container must survive reloads (v-show, not v-if): a
         state round-trip to 'loading' must not destroy the DOM node Monaco
         is mounted on, or the editor would blank out on every re-open. -->
    <div
      v-show="state === 'ready' && !monacoFailed"
      ref="editorElement"
      class="ws-file-preview__editor"
    />
    <!-- Plain-text fallback when the Monaco bundle is unavailable. -->
    <pre v-if="state === 'ready' && monacoFailed" class="ws-file-preview__fallback">{{ content }}</pre>

    <div
      v-if="ctxMenu"
      class="ws-file-preview__ctx"
      data-ws-file-ctx-menu
      :style="{ left: `${ctxMenu.x}px`, top: `${ctxMenu.y}px` }"
      role="menu"
      @contextmenu.prevent
    >
      <button type="button" class="ws-file-preview__ctx-item" role="menuitem" @click="onCtxCopy">
        {{ t('fileTree.ctxCopy') }}
      </button>
      <button type="button" class="ws-file-preview__ctx-item" role="menuitem" @click="onCtxAttach">
        {{ t('fileTree.ctxAttach') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.ws-file-preview {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.ws-file-preview__toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
}

.ws-file-preview__file {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.ws-file-preview__size {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.ws-file-preview__notice {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.ws-file-preview__copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  border-radius: var(--radius-sm);
  cursor: pointer;
  flex-shrink: 0;
}

.ws-file-preview__copy:hover {
  background: var(--bg-elevated);
  color: var(--text);
}

.ws-file-preview__state {
  padding: 20px 16px;
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-start;
}

.ws-file-preview__state p {
  margin: 0;
  word-break: break-word;
}

.ws-file-preview__retry {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.ws-file-preview__retry:hover {
  background: var(--bg-elevated);
}

.ws-file-preview__editor {
  flex: 1;
  min-height: 0;
}

.ws-file-preview__fallback {
  flex: 1;
  overflow: auto;
  margin: 0;
  padding: 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.ws-file-preview__ctx {
  position: fixed;
  z-index: 1100;
  min-width: 160px;
  display: flex;
  flex-direction: column;
  padding: 4px;
  background: var(--bg-elevated, var(--bg));
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-lg);
}

.ws-file-preview__ctx-item {
  display: block;
  width: 100%;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  text-align: start;
  padding: 6px 10px;
  border-radius: var(--radius-xs);
  cursor: pointer;
  white-space: nowrap;
}

.ws-file-preview__ctx-item:hover,
.ws-file-preview__ctx-item:focus-visible {
  background: var(--bg-hover, var(--bg-elevated));
}
</style>
