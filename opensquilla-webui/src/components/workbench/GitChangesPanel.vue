<template>
  <section class="git-changes" data-testid="git-changes-panel">
    <header class="git-changes__bar">
      <span class="git-changes__branch" :title="branchLabel || t('workbench.gitChanges.title')">
        <Icon name="fork" :size="14" aria-hidden="true" />
        <span class="git-changes__branch-label">{{ branchLabel || t('workbench.gitChanges.title') }}</span>
      </span>
      <span v-if="ahead > 0" class="git-changes__tracking" :title="t('workbench.gitChanges.ahead', { count: ahead })">↑{{ ahead }}</span>
      <span v-if="behind > 0" class="git-changes__tracking" :title="t('workbench.gitChanges.behind', { count: behind })">↓{{ behind }}</span>
      <span class="git-changes__spacer" />
      <span v-if="status" class="git-changes__count">{{ t('workbench.gitChanges.fileCount', { count: dirtyCount }) }}</span>
      <button
        type="button"
        class="git-changes__icon-button"
        :disabled="loading"
        :aria-label="t('workbench.gitChanges.refresh')"
        :title="t('workbench.gitChanges.refresh')"
        data-testid="git-changes-refresh"
        @click="reload()"
      >
        <Icon name="refresh" :size="14" aria-hidden="true" />
      </button>
    </header>

    <p v-if="error" class="git-changes__notice is-error" role="alert" data-testid="git-changes-error">
      <span class="git-changes__notice-text">{{ error }}</span>
      <button type="button" class="git-changes__retry" @click="reload()">{{ t('workbench.gitChanges.retry') }}</button>
    </p>
    <p v-else-if="loading && !status" class="git-changes__notice" role="status" data-testid="git-changes-loading">
      {{ t('workbench.gitChanges.loading') }}
    </p>
    <p v-else-if="unavailableLabel" class="git-changes__notice" role="status" data-testid="git-changes-unavailable">
      {{ unavailableLabel }}
    </p>
    <p v-else-if="status && !sections.length" class="git-changes__notice" role="status" data-testid="git-changes-clean">
      {{ t('workbench.gitChanges.clean') }}
    </p>

    <div class="git-changes__body">
      <div v-if="sections.length" class="git-changes__files" data-testid="git-changes-files">
        <section
          v-for="section in sections"
          :key="section.key"
          class="git-changes__section"
          :data-testid="`git-changes-section-${section.key}`"
        >
          <h2 class="git-changes__section-title">
            <span>{{ section.label }}</span>
            <span class="git-changes__section-count">{{ section.files.length }}</span>
          </h2>
          <button
            v-for="file in section.files"
            :key="`${section.key}:${file.path}`"
            type="button"
            class="git-changes__file"
            :class="{ 'is-selected': isSelected(file, section.staged) }"
            :aria-pressed="isSelected(file, section.staged) ? 'true' : 'false'"
            :title="file.path"
            data-testid="git-changes-file"
            @click="selectFile(file, section.staged)"
          >
            <span
              class="git-changes__status"
              :class="`is-${file.status}`"
              :title="t(`workbench.gitChanges.status.${file.status}`)"
            >{{ statusLetter(file.status) }}</span>
            <span class="git-changes__name">{{ file.name }}</span>
            <span v-if="file.directory" class="git-changes__dir">{{ file.directory }}</span>
          </button>
        </section>
      </div>

      <section v-if="selected" class="git-changes__diff" data-testid="git-changes-diff">
        <header class="git-changes__diff-head">
          <span class="git-changes__diff-path" :title="selected.path">{{ selected.path }}</span>
          <span v-if="selected.staged" class="git-changes__badge">{{ t('workbench.gitChanges.staged') }}</span>
          <template v-if="diff">
            <span class="git-changes__additions" :title="t('workbench.gitChanges.additions')">+{{ diff.additions }}</span>
            <span class="git-changes__deletions" :title="t('workbench.gitChanges.deletions')">-{{ diff.deletions }}</span>
          </template>
          <button
            type="button"
            class="git-changes__icon-button"
            :aria-label="t('workbench.gitChanges.closeDiff')"
            :title="t('workbench.gitChanges.closeDiff')"
            data-testid="git-changes-diff-close"
            @click="clearSelection()"
          >
            <Icon name="x" :size="14" aria-hidden="true" />
          </button>
        </header>
        <p v-if="diffLoading" class="git-changes__notice" role="status" data-testid="git-changes-diff-loading">
          {{ t('workbench.gitChanges.loading') }}
        </p>
        <p v-else-if="diffError" class="git-changes__notice is-error" role="alert" data-testid="git-changes-diff-error">
          {{ diffError }}
        </p>
        <p v-else-if="diff && diff.binary" class="git-changes__notice" role="status" data-testid="git-changes-diff-binary">
          {{ t('workbench.gitChanges.binary') }}
        </p>
        <template v-else-if="diff">
          <p v-if="!diffLines.length" class="git-changes__notice" role="status">{{ t('workbench.gitChanges.emptyDiff') }}</p>
          <code v-else class="git-changes__diff-code" data-testid="git-changes-diff-code"><span
            v-for="(line, index) in diffLines"
            :key="index"
            class="git-changes__diff-line"
            :class="line.className"
          >{{ line.text }}</span></code>
          <p v-if="diff.truncated" class="git-changes__notice" role="status" data-testid="git-changes-diff-truncated">
            {{ t('workbench.gitChanges.truncated') }}
          </p>
        </template>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  groupChangedFiles,
  useGitAccess,
  type GitDiffPayload,
  type GitFileStatus,
} from '@/modules/git'
import { useGitStatus } from '@/composables/useGitStatus'

/**
 * Source-control panel: grouped working-tree changes plus the unified diff of
 * the selected file. Every read is delegated to the Git owner through the
 * injected `GitAccess`; the panel never shells out and never mutates.
 *
 * `workspaceId` arrives from the workbench item payload (App.vue writes it when
 * the branch chip opens the tab), because a workbench panel renders outside the
 * chat route's project context.
 */
const props = withDefaults(defineProps<{
  workspaceId?: string | null
  /** Render state from the host: a collapsed/background tab must not read Git. */
  visible?: boolean
}>(), { workspaceId: null, visible: true })

const { t } = useI18n()
const access = useGitAccess()
const workspaceId = computed(() => props.workspaceId ?? null)
const { status, loading, error, branchLabel, dirtyCount, refresh } = useGitStatus({
  access: computed(() => access),
  workspaceId,
  active: computed(() => props.visible),
})

const STATUS_LETTER: Record<GitFileStatus, string> = {
  modified: 'M',
  added: 'A',
  deleted: 'D',
  renamed: 'R',
  untracked: '?',
  conflicted: 'U',
}

interface FileRow {
  path: string
  /** Last path segment, so a deep path stays readable in a narrow panel. */
  name: string
  /** Everything before the last segment; rendered as muted secondary text. */
  directory: string
  status: GitFileStatus
}

interface FileSection {
  key: 'staged' | 'unstaged' | 'untracked'
  staged: boolean
  label: string
  files: FileRow[]
}

function statusLetter(value: GitFileStatus): string {
  return STATUS_LETTER[value] ?? '?'
}

function splitPath(path: string): { name: string; directory: string } {
  const index = path.lastIndexOf('/')
  return index < 0
    ? { name: path, directory: '' }
    : { name: path.slice(index + 1), directory: path.slice(0, index) }
}

function toRow(file: { path: string; status: GitFileStatus }): FileRow {
  const parts = splitPath(file.path)
  return { path: file.path, name: parts.name, directory: parts.directory, status: file.status }
}

/** Empty sections are dropped so a clean group never renders an empty heading. */
const sections = computed<FileSection[]>(() => {
  const grouped = groupChangedFiles(status.value?.files ?? [])
  return [
    { key: 'staged' as const, staged: true, label: t('workbench.gitChanges.staged'), files: grouped.staged.map(toRow) },
    { key: 'unstaged' as const, staged: false, label: t('workbench.gitChanges.unstaged'), files: grouped.unstaged.map(toRow) },
    { key: 'untracked' as const, staged: false, label: t('workbench.gitChanges.untracked'), files: grouped.untracked.map(toRow) },
  ].filter(section => section.files.length > 0)
})

const ahead = computed(() => status.value?.ahead ?? 0)
const behind = computed(() => status.value?.behind ?? 0)

/**
 * A repository that Git cannot read is a state, not an error: the reason drives
 * the copy so "no repository" and "no Git" never look like a load failure.
 */
const unavailableLabel = computed(() => {
  const current = status.value
  if (current && !current.available) {
    return current.reason === 'git_unavailable'
      ? t('workbench.gitChanges.gitUnavailable')
      : t('workbench.gitChanges.notARepository')
  }
  if (!access) return t('workbench.gitChanges.gitUnavailable')
  return ''
})

interface Selection {
  path: string
  staged: boolean
}

const selected = ref<Selection | null>(null)
const diff = ref<GitDiffPayload | null>(null)
const diffLoading = ref(false)
const diffError = ref<string | null>(null)
/** Fence: only the newest diff request may publish a payload. */
let diffController: AbortController | null = null

function isSelected(file: { path: string }, staged: boolean): boolean {
  const current = selected.value
  return current !== null && current.path === file.path && current.staged === staged
}

function clearSelection(): void {
  diffController?.abort()
  diffController = null
  selected.value = null
  diff.value = null
  diffError.value = null
  diffLoading.value = false
}

async function loadDiff(): Promise<void> {
  const target = selected.value
  diffController?.abort()
  diffController = null
  diff.value = null
  diffError.value = null
  if (!target || !access) {
    diffLoading.value = false
    return
  }
  const controller = new AbortController()
  diffController = controller
  diffLoading.value = true
  try {
    const payload = await access.diff({
      path: target.path,
      staged: target.staged,
      workspaceId: workspaceId.value,
      signal: controller.signal,
    })
    if (diffController !== controller) return
    diff.value = payload
  } catch (cause) {
    if (diffController !== controller) return
    diffError.value = cause instanceof Error && cause.message ? cause.message : String(cause)
  } finally {
    if (diffController === controller) {
      diffController = null
      diffLoading.value = false
    }
  }
}

async function selectFile(file: { path: string }, staged: boolean): Promise<void> {
  selected.value = { path: file.path, staged }
  await loadDiff()
}

/** Refresh re-reads the repository and the open diff; a stale diff would lie. */
async function reload(): Promise<void> {
  await refresh()
  if (selected.value) await loadDiff()
}

const diffLines = computed<Array<{ text: string; className: string }>>(() => {
  const text = diff.value?.diff
  if (!text) return []
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines.map(line => ({
    // `white-space: pre` keeps alignment; an empty line still needs a glyph box.
    text: line === '' ? ' ' : line,
    className: diffLineClass(line),
  }))
})

function diffLineClass(line: string): string {
  if (line.startsWith('@@')) return 'is-hunk'
  if (line.startsWith('+++') || line.startsWith('---')) return 'is-meta'
  if (/^(?:diff --git|index |new file mode|deleted file mode|similarity index|rename from|rename to|old mode|new mode|Binary files) /.test(line)) {
    return 'is-meta'
  }
  if (line.startsWith('+')) return 'is-add'
  if (line.startsWith('-')) return 'is-del'
  return 'is-context'
}

// Another repository must never show the previous repository's diff.
watch(workspaceId, () => { clearSelection() })
onScopeDispose(() => {
  diffController?.abort()
  diffController = null
})
</script>

<style scoped>
.git-changes { background: var(--bg); color: var(--text); display: flex; flex: 1; flex-direction: column; min-height: 0; }
.git-changes__bar {
  align-items: center;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  display: flex;
  flex-shrink: 0;
  font: var(--fs-xs) var(--font-sans);
  gap: var(--sp-2);
  min-height: 34px;
  padding: 0 var(--sp-3);
}
.git-changes__branch { align-items: center; color: var(--text); display: inline-flex; gap: var(--sp-1); min-width: 0; }
.git-changes__branch-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.git-changes__tracking { color: var(--text-dim); font-variant-numeric: tabular-nums; }
.git-changes__spacer { flex: 1; }
.git-changes__count { color: var(--text-dim); font-variant-numeric: tabular-nums; }
.git-changes__icon-button {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex-shrink: 0;
  height: 26px;
  justify-content: center;
  padding: 0;
  width: 26px;
}
.git-changes__icon-button:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.git-changes__icon-button:disabled { cursor: default; opacity: 0.5; }
.git-changes__icon-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.git-changes__body { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.git-changes__files {
  border-bottom: 1px solid var(--border);
  flex: 0 1 auto;
  max-height: 55%;
  min-height: 0;
  overflow: auto;
  padding: var(--sp-1) 0;
}
.git-changes__section + .git-changes__section { border-top: 1px solid var(--hairline); }
.git-changes__section-title {
  align-items: baseline;
  color: var(--text-dim);
  display: flex;
  font: 600 var(--fs-xs) var(--font-sans);
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-2) var(--sp-3) var(--sp-1);
}
.git-changes__section-count { color: var(--text-dim); font-weight: 400; }
.git-changes__file {
  align-items: baseline;
  background: transparent;
  border: 0;
  color: var(--text);
  cursor: pointer;
  display: flex;
  font: var(--fs-sm) var(--font-sans);
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  text-align: left;
  width: 100%;
}
.git-changes__file:hover { background: var(--bg-hover); }
.git-changes__file.is-selected { background: color-mix(in srgb, var(--accent) 14%, transparent); }
.git-changes__file:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.git-changes__status { color: var(--text-dim); flex-shrink: 0; font: 600 var(--fs-xs) var(--font-mono); min-width: 1.1em; text-align: center; }
.git-changes__status.is-modified { color: var(--warn); }
.git-changes__status.is-added { color: var(--ok); }
.git-changes__status.is-deleted { color: var(--danger); }
.git-changes__status.is-conflicted { color: var(--danger); }
.git-changes__status.is-renamed { color: var(--info); }
.git-changes__name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.git-changes__dir { color: var(--text-dim); flex: 1 1 auto; font: var(--fs-xs) var(--font-mono); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.git-changes__diff { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.git-changes__diff-head {
  align-items: center;
  border-bottom: 1px solid var(--hairline);
  color: var(--text-muted);
  display: flex;
  flex-shrink: 0;
  font: var(--fs-xs) var(--font-sans);
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
}
.git-changes__diff-path { color: var(--text); flex: 1 1 auto; font: var(--fs-xs) var(--font-mono); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.git-changes__badge { border: 1px solid var(--border); border-radius: var(--radius-pill); color: var(--text-dim); flex-shrink: 0; padding: 0 var(--sp-2); }
.git-changes__additions { color: var(--ok); flex-shrink: 0; font-variant-numeric: tabular-nums; }
.git-changes__deletions { color: var(--danger); flex-shrink: 0; font-variant-numeric: tabular-nums; }
.git-changes__diff-code { display: block; flex: 1; font: var(--fs-xs) var(--font-mono); min-height: 0; overflow: auto; padding: var(--sp-1) 0; white-space: pre; }
.git-changes__diff-line { display: block; padding: 0 var(--sp-3); white-space: pre; }
.git-changes__diff-line.is-add { background: color-mix(in srgb, var(--ok) 14%, transparent); }
.git-changes__diff-line.is-del { background: color-mix(in srgb, var(--danger) 14%, transparent); }
.git-changes__diff-line.is-hunk { background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent); }
.git-changes__diff-line.is-meta { color: var(--text-dim); }
.git-changes__notice {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  font: var(--fs-sm) var(--font-sans);
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
}
.git-changes__notice.is-error { color: var(--danger); }
.git-changes__notice-text { min-width: 0; overflow-wrap: anywhere; }
.git-changes__retry {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text);
  cursor: pointer;
  font: var(--fs-xs) var(--font-sans);
  padding: var(--sp-1) var(--sp-2);
}
.git-changes__retry:hover { background: var(--bg-hover); }
</style>
