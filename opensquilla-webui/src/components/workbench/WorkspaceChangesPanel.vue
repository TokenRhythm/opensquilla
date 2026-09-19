<template>
  <div class="wb-changes" :class="{ 'is-wrapped': wrapLines }">
    <header class="wb-changes__bar">
      <div class="wb-changes__meta">
        <span v-if="branchLabel" class="wb-changes__branch">{{ branchLabel }}</span>
        <span v-if="divergenceLabel" class="wb-changes__divergence">{{ divergenceLabel }}</span>
        <span v-if="changes?.available" class="wb-changes__count">
          {{ t('workbench.changes.fileCount', { count: changes.totalCount }) }}
        </span>
        <span
          v-if="changes?.available"
          class="wb-changes__count"
          :title="t('workbench.changes.diffStats', { added: changes.addedLines, removed: changes.removedLines })"
          :aria-label="t('workbench.changes.diffStats', { added: changes.addedLines, removed: changes.removedLines })"
        >
          <span class="wb-changes__added">+{{ changes.addedLines }}</span>
          <span class="wb-changes__removed">-{{ changes.removedLines }}</span>
        </span>
      </div>
      <div class="wb-changes__bar-actions">
        <button
          type="button"
          class="btn btn--icon"
          :aria-pressed="wrapLines"
          :aria-label="t('workbench.changes.wrapLines')"
          :title="t('workbench.changes.wrapLines')"
          data-testid="changes-wrap-lines"
          @click="wrapLines = !wrapLines"
        >
          <Icon name="wrapText" :size="12" />
        </button>
        <!-- History actions belong to the branch, not to one section, so they sit
             in this row with the branch name and its divergence. -->
        <button
          type="button"
          class="btn btn--icon"
          :disabled="indexBusy || !canUndoCommit"
          :aria-busy="indexBusy"
          :aria-label="t('workbench.changes.undoCommit')"
          :title="canUndoCommit
            ? t('workbench.changes.undoCommit')
            : t('workbench.changes.undoCommitUnavailable', { upstream: changes?.upstream || '' })"
          data-testid="changes-undo-commit"
          @click="undoCommit()"
        >
          <Icon name="undo" :size="12" />
        </button>
        <button
          type="button"
          class="btn btn--icon"
          :disabled="indexBusy || !changes?.upstream"
          :aria-busy="indexBusy"
          :aria-label="t('workbench.changes.push')"
          :title="changes?.upstream
            ? t('workbench.changes.push')
            : t('workbench.changes.pushUnavailable')"
          data-testid="changes-push"
          @click="pushBranch()"
        >
          <Icon name="arrowUp" :size="12" />
        </button>
        <button
          type="button"
          class="btn btn--icon"
          :disabled="loading"
          :aria-label="t('workbench.changes.refresh')"
          :title="t('workbench.changes.refresh')"
          data-testid="changes-refresh"
          @click="reload()"
        >
          <Icon name="refresh" :size="12" />
        </button>
      </div>
    </header>

    <!-- The commit message sits above the sections, the way every
         source-control surface arranges it: state first, then what you say
         about it. -->
    <div v-if="changes?.available" class="wb-changes__commit">
      <!-- A commit message is a subject and an optional body, so the field is
           a textarea the way every source-control input is; Enter still
           commits and Shift+Enter starts the body. -->
      <textarea
        ref="commitInputRef"
        v-model="commitMessage"
        rows="1"
        class="wb-changes__commit-input"
        :placeholder="t('workbench.changes.commitPlaceholder')"
        :aria-label="t('workbench.changes.commitPlaceholder')"
        :disabled="indexBusy || !hasStaged"
        data-testid="changes-commit-message"
        @keydown.enter.exact.prevent="commitIndex()"
      ></textarea>
      <!-- Where a source-control surface puts "write this for me": in the
           message row, next to the commit action. -->
      <button
        type="button"
        class="btn btn--icon"
        :disabled="indexBusy || !hasStaged || draftingMessage"
        :aria-busy="draftingMessage"
        :aria-label="t('workbench.changes.draftCommitMessage')"
        :title="draftingMessage
          ? t('workbench.changes.draftingCommitMessage')
          : t('workbench.changes.draftCommitMessage')"
        data-testid="changes-draft-message"
        @click="draftCommitMessage()"
      >
        <Icon name="sparkle" :size="12" />
      </button>
      <button
        type="button"
        class="btn btn--icon"
        :disabled="indexBusy || !canCommit"
        :aria-busy="indexBusy"
        :aria-label="t('workbench.changes.commit')"
        :title="t('workbench.changes.commit')"
        data-testid="changes-commit"
        @click="commitIndex()"
      >
        <Icon name="check" :size="12" />
      </button>
    </div>

    <p v-if="notice" class="wb-changes__note" role="status" data-testid="changes-notice">
      {{ notice }}
    </p>

    <p v-if="loading && !changes" class="wb-changes__note" role="status">
      {{ t('workbench.changes.loading') }}
    </p>

    <div v-else-if="errorMessage" class="wb-changes__note wb-changes__note--error" role="alert">
      <span>{{ errorMessage }}</span>
      <button type="button" class="wb-changes__action" @click="reload()">
        {{ t('workbench.changes.retry') }}
      </button>
    </div>

    <div v-else-if="changes && !changes.available" class="wb-changes__note" role="status">
      <strong>{{ t('workbench.changes.unavailableTitle') }}</strong>
      <span>{{ unavailableDetail }}</span>
    </div>

    <p v-else-if="changes && changes.entries.length === 0" class="wb-changes__note" role="status">
      {{ t('workbench.changes.empty') }}
    </p>

    <p
      v-else-if="changes && changes.truncated"
      class="wb-changes__note wb-changes__note--truncated"
      role="status"
    >
      {{ t('workbench.changes.truncated', {
        count: changes.entries.length,
        total: changes.totalCount,
      }) }}
    </p>

    <div
      v-if="changes && changes.entries.length > 0"
      class="wb-changes__body"
      :style="{ '--wb-changes-list-height': listHeight === null ? 'auto' : `${listHeight}px` }"
    >
      <div
        class="wb-changes__list"
        :class="{ 'is-sized': listHeight !== null }"
        role="list"
        :aria-label="t('workbench.changes.listLabel')"
      >
        <section v-for="group in groups" :key="group.key" class="wb-changes__group">
          <h4 class="wb-changes__group-head">
            <button
              type="button"
              class="wb-changes__group-toggle"
              :aria-expanded="!collapsed.has(group.key)"
              :aria-controls="`wb-changes-group-${group.key}`"
              data-testid="changes-group-toggle"
              :data-group="group.key"
              @click="toggleGroup(group.key)"
            >
              <!-- One chevron that rotates, the way the app's own collapsible
                   rows do it, instead of two different glyphs for two states. -->
              <Icon name="chevronRight" :size="12" class="wb-changes__group-chevron" />
              <span class="wb-changes__group-label">{{ group.label }}</span>
              <span class="wb-changes__group-count">{{ group.entries.length }}</span>
            </button>
            <!-- The whole-set action belongs to the group it applies to, so there
                 is no separate "stage everything" concept to explain. -->
            <!-- Section actions reveal on hover the way a source-control list does
                 it, so a header is quiet until you point at it. -->
            <span class="wb-changes__group-actions">
              <button
                v-if="canDiscardGroup(group)"
                type="button"
                class="wb-changes__list-action"
                :disabled="indexBusy"
                :aria-busy="indexBusy"
                :aria-label="t('workbench.changes.discardAll')"
                :title="t('workbench.changes.discardAll')"
                data-testid="changes-group-discard-action"
                :data-group="group.key"
                @click="discardGroup(group)"
              >
                <Icon name="undo" :size="12" />
              </button>
              <button
                type="button"
                class="wb-changes__list-action"
                :disabled="indexBusy"
                :aria-busy="indexBusy"
                :aria-label="t(`workbench.changes.${group.indexAction}All`)"
                :title="t(`workbench.changes.${group.indexAction}All`)"
                data-testid="changes-group-index-action"
                :data-index-action="group.indexAction"
                @click="applyGroupIndexChange(group)"
              >
                <Icon :name="group.indexAction === 'stage' ? 'plus' : 'minus'" :size="12" />
              </button>
            </span>
          </h4>
          <div v-show="!collapsed.has(group.key)" :id="`wb-changes-group-${group.key}`">
          <!-- The row is a container so the selectable area and the index action
               can be siblings: a button cannot contain another button. -->
          <div
            v-for="entry in group.entries"
            :key="entryKey(entry)"
            class="wb-changes__row"
            :class="{ 'is-selected': entryKey(entry) === selectedKey }"
          >
          <button
            type="button"
            class="wb-changes__entry"
            :aria-pressed="entryKey(entry) === selectedKey"
            :data-entry-key="entryKey(entry)"
            @click="select(entry)"
            @keydown.down.prevent="focusSibling(entry, 1)"
            @keydown.up.prevent="focusSibling(entry, -1)"
            @keydown.home.prevent="focusEdge('first')"
            @keydown.end.prevent="focusEdge('last')"
          >
            <span
              class="wb-changes__type"
              :data-type="entry.changeType"
              :aria-label="t(`workbench.changes.types.${entry.changeType}`)"
              :title="t(`workbench.changes.types.${entry.changeType}`)"
            >{{ typeLetter(entry.changeType) }}</span>
            <span class="wb-changes__path">
              <span v-if="pathParts(entry.path).dir" class="wb-changes__path-dir">{{ pathParts(entry.path).dir }}</span>
              <span class="wb-changes__path-base">{{ pathParts(entry.path).base }}</span>
            </span>
            <!-- One cluster for everything the row says about its state, so the
                 actions can replace it instead of landing on top of it. -->
            <span class="wb-changes__row-status">
              <span v-if="entryStats(entry)" class="wb-changes__stats">
                <span class="wb-changes__added">+{{ entry.addedLines }}</span>
                <span class="wb-changes__removed">-{{ entry.removedLines }}</span>
              </span>
              <span v-if="entry.staged && entry.unstaged" class="wb-changes__both">
                {{ t('workbench.changes.bothHalves') }}
              </span>
              <Icon v-else-if="entry.staged" name="check" :size="12" class="wb-changes__check" />
            </span>
          </button>
          <!-- One icon per action, the way a source-control list does it, so a
               row stays a single line however many actions it grows. -->
          <div class="wb-changes__row-actions">
            <button
              v-if="entry.changeType !== 'untracked'"
              type="button"
              class="wb-changes__list-action"
              :disabled="indexBusy"
              :aria-busy="indexBusy"
              :aria-label="t('workbench.changes.discard')"
              :title="t('workbench.changes.discard')"
              data-testid="changes-discard-action"
              :data-entry-key="entryKey(entry)"
              @click="discardEntry(entry)"
            >
              <Icon name="undo" :size="12" />
            </button>
            <button
              type="button"
              class="wb-changes__list-action"
              :disabled="indexBusy"
              :aria-busy="indexBusy"
              :aria-label="t(`workbench.changes.${indexAction(entry)}`)"
              :title="t(`workbench.changes.${indexAction(entry)}`)"
              data-testid="changes-index-action"
              :data-index-action="indexAction(entry)"
              :data-entry-key="entryKey(entry)"
              @click="applyIndexChange(entry)"
            >
              <Icon :name="indexAction(entry) === 'stage' ? 'plus' : 'minus'" :size="12" />
            </button>
          </div>
          </div>
          </div>
        </section>
      </div>

      <!-- The two panes are adjustable, like every other list/detail split. -->
      <div
        ref="splitterRef"
        class="wb-changes__splitter"
        :class="{ 'is-dragging': splitterDragging }"
        role="separator"
        tabindex="0"
        aria-orientation="horizontal"
        :aria-label="t('workbench.changes.resizeSplitter')"
        :aria-valuemin="SPLITTER_MIN_HEIGHT"
        :aria-valuemax="splitterMaxHeight"
        :aria-valuenow="Math.round(listHeight ?? measuredListHeight)"
        data-testid="changes-splitter"
        @pointerdown="onSplitterPointerDown"
        @pointermove="onSplitterPointerMove"
        @pointerup="onSplitterPointerUp"
        @pointercancel="onSplitterPointerUp"
        @lostpointercapture="onSplitterPointerUp"
        @dblclick="resetSplitter"
        @keydown="onSplitterKeydown"
      >
        <span class="wb-changes__splitter-grip" aria-hidden="true" />
      </div>

      <section
        class="wb-changes__diff"
        :aria-busy="diffLoading"
        :aria-label="t('workbench.changes.diffLabel')"
      >
        <!-- One row for "which file am I looking at". The list above is the
             navigation: it shows every changed file, and the arrow keys walk it.
             A step-by-one pager next to the name is a control no review surface
             has, and it earned nothing the list does not already do. -->
        <div v-if="changes && changes.entries.length > 0" class="wb-changes__diff-head">
          <template v-if="diff">
            <span class="wb-changes__diff-path">{{ diff.path }}</span>
            <span class="wb-changes__diff-side">
              {{ diff.staged ? t('workbench.changes.staged') : t('workbench.changes.unstaged') }}
            </span>
          </template>
          <!-- While the patch is in flight the file is already known, so the row
               keeps naming it instead of falling back to the prompt. -->
          <span v-else-if="selectedEntry" class="wb-changes__diff-path">
            {{ selectedEntry.path }}
          </span>
          <span v-else class="wb-changes__diff-prompt">
            {{ t('workbench.changes.selectPrompt') }}
          </span>
        </div>
        <div
          v-if="indexError"
          class="wb-changes__note wb-changes__note--error"
          role="alert"
          data-testid="changes-index-error"
        >
          <span>{{ indexError }}</span>
        </div>

        <p v-if="diffLoading" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffLoading') }}
        </p>
        <div v-else-if="diffError" class="wb-changes__note wb-changes__note--error" role="alert">
          {{ diffError }}
        </div>
        <p v-else-if="diff && diff.binary" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffBinary') }}
        </p>
        <p v-else-if="diff && !diff.text.trim()" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffEmpty') }}
        </p>
        <template v-else-if="diff">
          <p v-if="diff.truncated" class="wb-changes__note" role="status">
            {{ t('workbench.changes.diffTruncated') }}
          </p>
          <div class="wb-changes__code">
            <div
              v-for="(line, index) in diffLines"
              :key="index"
              class="wb-changes__line"
              :data-kind="line.kind"
              :title="line.kind === 'hunk' ? t('workbench.changes.hunkHeaderHint') : undefined"
            >
              <template v-if="hasGutters(line)">
                <span class="wb-changes__gutter" aria-hidden="true">{{ line.oldNumber ?? '' }}</span>
                <span class="wb-changes__gutter" aria-hidden="true">{{ line.newNumber ?? '' }}</span>
              </template>
              <code class="wb-changes__line-code">{{ line.text }}</code>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useConfirm } from '@/composables/useConfirm'
import {
  WORKSPACE_CHANGES_KEY,
  type WorkspaceChangeEntry,
  type WorkspaceChangeType,
  type WorkspaceChanges,
  type WorkspaceChangesReader,
  type WorkspaceFileDiff,
} from '@/modules/workspaceChanges'

const props = defineProps<{
  workspaceId: string
  workspaceName?: string
}>()

const { t } = useI18n()
const reader = inject<WorkspaceChangesReader | null>(WORKSPACE_CHANGES_KEY, null)

const changes = ref<WorkspaceChanges | null>(null)
const wrapLines = ref(true)
const loading = ref(false)

// Pane split. `null` keeps the automatic split; a number is a dragged height.
const SPLITTER_MIN_HEIGHT = 72
const SPLITTER_KEYBOARD_STEP = 24
const listHeight = ref<number | null>(null)
const measuredListHeight = ref(0)
const splitterDragging = ref(false)
const splitterRef = ref<HTMLElement | null>(null)
let splitterStartY = 0
let splitterStartHeight = 0

const splitterMaxHeight = computed(() => {
  const pane = splitterRef.value?.parentElement
  return pane ? Math.max(SPLITTER_MIN_HEIGHT, Math.round(pane.clientHeight * 0.8)) : 400
})

function currentListHeight(): number {
  const list = splitterRef.value?.previousElementSibling
  return list instanceof HTMLElement ? list.getBoundingClientRect().height : 0
}

function onSplitterPointerDown(event: PointerEvent) {
  const handle = splitterRef.value
  if (!handle) return
  splitterDragging.value = true
  splitterStartY = event.clientY
  splitterStartHeight = currentListHeight()
  measuredListHeight.value = Math.round(splitterStartHeight)
  handle.setPointerCapture(event.pointerId)
  event.preventDefault()
}

function onSplitterPointerMove(event: PointerEvent) {
  if (!splitterDragging.value) return
  const next = splitterStartHeight + (event.clientY - splitterStartY)
  listHeight.value = Math.min(
    splitterMaxHeight.value,
    Math.max(SPLITTER_MIN_HEIGHT, Math.round(next)),
  )
}

function onSplitterPointerUp(event: PointerEvent) {
  if (!splitterDragging.value) return
  splitterDragging.value = false
  const handle = splitterRef.value
  if (handle?.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId)
}

function onSplitterKeydown(event: KeyboardEvent) {
  const step = event.key === 'ArrowUp' ? -SPLITTER_KEYBOARD_STEP
    : event.key === 'ArrowDown' ? SPLITTER_KEYBOARD_STEP
      : 0
  if (step === 0) return
  event.preventDefault()
  const base = listHeight.value ?? Math.round(currentListHeight())
  listHeight.value = Math.min(
    splitterMaxHeight.value,
    Math.max(SPLITTER_MIN_HEIGHT, base + step),
  )
}

function resetSplitter() {
  listHeight.value = null
}
const errorMessage = ref('')
const indexBusy = ref(false)
const indexError = ref('')
const commitMessage = ref('')
/** Kept apart from `indexBusy`: drafting touches nothing, so it must not
 * disable the index actions the operator may already be mid-way through. */
const draftingMessage = ref(false)
/** Bumped whenever the index a draft describes may have moved, so an answer for
 * the previous index is dropped instead of filling the field with it. */
let draftRequestId = 0
const commitInputRef = ref<HTMLTextAreaElement | null>(null)
/** One slot for the last write's outcome, so a success is as visible as a
 * failure instead of leaving the list as the only evidence. */
const notice = ref('')
const { confirm } = useConfirm()

/** Groups the operator folded away, so a long list cannot hide the others. */
const collapsed = ref(new Set<string>())

function toggleGroup(key: string) {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}
const selectedKey = ref('')
const diff = ref<WorkspaceFileDiff | null>(null)
const diffLoading = ref(false)
const diffError = ref('')

// One glyph per change type keeps the row scannable; the localized name stays
// available through the chip's accessible label.
const TYPE_LETTER: Record<WorkspaceChangeType, string> = {
  added: 'A',
  modified: 'M',
  deleted: 'D',
  renamed: 'R',
  copied: 'C',
  typeChanged: 'T',
  unmerged: 'U',
  untracked: '?',
  unknown: '·',
}

type DiffLineKind = 'context' | 'added' | 'removed' | 'hunk' | 'notice'

interface DiffLine {
  kind: DiffLineKind
  oldNumber: number | null
  newNumber: number | null
  text: string
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/
// Patch plumbing the panel already states elsewhere (the file path and status
// are in its own header). Rendering it as code lines is what made the left edge
// of the patch jump between three different offsets.
const SKIPPED_PATCH_PREFIXES = [
  'diff --git ',
  'index ',
  '--- ',
  '+++ ',
  'old mode ',
  'new mode ',
  'similarity index ',
]

// Lines that carry information the panel header does not, kept as one
// full-width notice rather than a numbered code line.
const NOTICE_PATCH_PREFIXES = [
  'new file mode ',
  'deleted file mode ',
  'rename from ',
  'rename to ',
  'copy from ',
  'copy to ',
  'Binary files ',
  'GIT binary patch',
]

let requestToken = 0

const selectedEntry = computed<WorkspaceChangeEntry | null>(() => {
  if (!changes.value || !selectedKey.value) return null
  return changes.value.entries.find(entry => entryKey(entry) === selectedKey.value) || null
})

const branchLabel = computed(() => {
  const value = changes.value
  if (!value || !value.available) return ''
  if (value.detached) return t('workbench.changes.detached')
  return value.branch || ''
})

const divergenceLabel = computed(() => {
  const value = changes.value
  if (!value || !value.available) return ''
  if (!value.ahead && !value.behind) return ''
  return t('workbench.changes.divergence', {
    ahead: value.ahead,
    behind: value.behind,
  })
})

const groups = computed(() => {
  const value = changes.value
  if (!value) return []
  const untracked: WorkspaceChangeEntry[] = []
  const staged: WorkspaceChangeEntry[] = []
  const unstaged: WorkspaceChangeEntry[] = []
  for (const entry of value.entries) {
    if (entry.changeType === 'untracked') untracked.push(entry)
    else if (entry.staged) staged.push(entry)
    else unstaged.push(entry)
  }
  return [
    {
      key: 'staged',
      icon: 'check' as const,
      label: t('workbench.changes.groupStaged'),
      indexAction: 'unstage' as const,
      entries: staged,
    },
    {
      key: 'unstaged',
      icon: 'pencil' as const,
      label: t('workbench.changes.groupUnstaged'),
      indexAction: 'stage' as const,
      entries: unstaged,
    },
    {
      key: 'untracked',
      icon: 'plus' as const,
      label: t('workbench.changes.groupUntracked'),
      indexAction: 'stage' as const,
      entries: untracked,
    },
  ].filter(group => group.entries.length > 0)
})

/**
 * Split a unified diff into renderable rows.
 *
 * Line numbers come from the hunk headers rather than being counted from the
 * top, because a hunk never starts at line 1. Each row carries its kind so the
 * stylesheet can tint the whole row instead of only its text — a tinted row is
 * what makes a patch readable at a glance, and it keeps the code itself in the
 * normal foreground colour.
 */
const diffLines = computed<DiffLine[]>(() => {
  const value = diff.value
  if (!value || value.binary || !value.text) return []
  const rows: DiffLine[] = []
  let oldNumber = 0
  let newNumber = 0
  const push = (
    kind: DiffLineKind,
    text: string,
    oldLine: number | null,
    newLine: number | null,
  ) => {
    rows.push({ kind, oldNumber: oldLine, newNumber: newLine, text })
  }
  for (const raw of value.text.split('\n')) {
    // A trailing newline leaves one empty tail entry; an empty *context* line in
    // a patch always carries a leading space, so this cannot drop real content.
    if (raw === '') continue
    const hunk = HUNK_HEADER_RE.exec(raw)
    if (hunk) {
      oldNumber = Number(hunk[1])
      newNumber = Number(hunk[2])
      push('hunk', raw, null, null)
      continue
    }
    if (SKIPPED_PATCH_PREFIXES.some(prefix => raw.startsWith(prefix))) continue
    if (NOTICE_PATCH_PREFIXES.some(prefix => raw.startsWith(prefix))) {
      push('notice', raw, null, null)
      continue
    }
    if (raw.startsWith('+')) {
      push('added', raw, null, newNumber++)
      continue
    }
    if (raw.startsWith('-')) {
      push('removed', raw, oldNumber++, null)
      continue
    }
    if (raw.startsWith(' ')) {
      push('context', raw, oldNumber++, newNumber++)
      continue
    }
    // Trailing "\ No newline at end of file" and any other stray line.
    push('notice', raw, null, null)
  }
  return rows
})

const unavailableDetail = computed(() => {
  const reason = changes.value?.availabilityReason
  switch (reason) {
    case 'git_unavailable':
      return t('workbench.changes.reasonGitUnavailable')
    case 'not_repository':
      return t('workbench.changes.reasonNotRepository')
    case 'timed_out':
      return t('workbench.changes.reasonTimedOut')
    default:
      return t('workbench.changes.reasonFailed')
  }
})

function typeLetter(type: WorkspaceChangeType): string {
  return TYPE_LETTER[type] ?? '·'
}

/** Counts are shown only when both halves are known; `0/0` for a binary file
 * would claim a measurement Git did not make. */
function entryStats(entry: WorkspaceChangeEntry): boolean {
  return Number.isFinite(entry.addedLines) && Number.isFinite(entry.removedLines)
}

/** File headers and hunk bands span the row: an empty gutter reads as a
 * misaligned column. */
function hasGutters(line: DiffLine): boolean {
  return line.kind === 'context' || line.kind === 'added' || line.kind === 'removed'
}

function entryButtons(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
}

function focusSibling(entry: WorkspaceChangeEntry, offset: number) {
  const buttons = entryButtons()
  const index = buttons.findIndex(button => button.dataset.entryKey === entryKey(entry))
  const next = buttons[index + offset]
  next?.focus()
}

function focusEdge(edge: 'first' | 'last') {
  const buttons = entryButtons()
  const target = edge === 'first' ? buttons[0] : buttons[buttons.length - 1]
  target?.focus()
}

function pathParts(path: string): { dir: string; base: string } {
  const index = path.lastIndexOf('/')
  if (index === -1) return { dir: '', base: path }
  return { dir: path.slice(0, index + 1), base: path.slice(index + 1) }
}

function entryKey(entry: WorkspaceChangeEntry): string {
  return `${entry.path}::${entry.staged ? 'staged' : 'unstaged'}`
}

async function reload(preserveSelection = false) {
  const activeReader = reader
  if (!activeReader) {
    errorMessage.value = t('workbench.changes.readerUnavailable')
    return
  }
  const token = ++requestToken
  // The index may be about to change (a commit, a stage, another workspace), so
  // a draft still in flight describes a state that is going away.
  draftRequestId += 1
  draftingMessage.value = false
  loading.value = true
  if (!preserveSelection) {
    selectedKey.value = ''
    diff.value = null
    diffError.value = ''
  }
  try {
    const result = await activeReader.readChanges(props.workspaceId)
    if (token !== requestToken) return
    changes.value = result
    errorMessage.value = ''
    if (
      selectedKey.value
      && !result.entries.some(entry => entryKey(entry) === selectedKey.value)
    ) {
      selectedKey.value = ''
      diff.value = null
    }
  } catch (error) {
    if (token !== requestToken) return
    changes.value = null
    errorMessage.value = error instanceof Error
      ? error.message
      : t('workbench.changes.loadFailed')
  } finally {
    if (token === requestToken) loading.value = false
  }
}

/**
 * Which index action a row offers.
 *
 * The group a row sits in is the state it is showing, so the action is the
 * inverse of that group: a worktree change is offered "stage", a staged entry
 * is offered "unstage".
 */
function indexAction(entry: WorkspaceChangeEntry): 'stage' | 'unstage' {
  return entry.staged ? 'unstage' : 'stage'
}

interface WorkspaceChangeGroup {
  key: string
  icon: 'check' | 'pencil' | 'plus'
  label: string
  indexAction: 'stage' | 'unstage'
  entries: WorkspaceChangeEntry[]
}

/**
 * Move a set of paths between the worktree and the index.
 *
 * One row and a whole group are the same operation with a different path list,
 * so they share this. The write is acknowledged with an index-only result, so
 * the list is re-read rather than patched from the response. Rows move between
 * groups, and the open file is re-selected by *path* because its key (path plus
 * half) is expected to move with it.
 */
async function runIndexChange(paths: readonly string[], staged: boolean) {
  const activeReader = reader
  if (!activeReader || indexBusy.value || paths.length === 0) return
  const selectedPath = selectedEntry.value?.path
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    await activeReader.stagePaths({
      workspaceId: props.workspaceId,
      paths: [...paths],
      staged,
    })
    await reload(true)
    const refreshed = selectedPath
      ? changes.value?.entries.find(candidate => candidate.path === selectedPath)
      : undefined
    if (refreshed) {
      await select(refreshed)
    } else if (selectedPath) {
      selectedKey.value = ''
      diff.value = null
    }
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

function applyIndexChange(entry: WorkspaceChangeEntry) {
  return runIndexChange([entry.path], indexAction(entry) === 'stage')
}

/**
 * Discard one file's uncommitted worktree edits.
 *
 * The only operation here that can lose work, so it asks first and names the
 * file in the question. Untracked rows never offer it: restoring one would have
 * to delete the file, and that is not a decision to make from a list icon.
 */
async function discardEntry(entry: WorkspaceChangeEntry) {
  const activeReader = reader
  if (!activeReader || indexBusy.value) return
  const confirmed = await confirm({
    title: t('workbench.changes.discardTitle'),
    body: t('workbench.changes.discardBody', { path: entry.path }),
    primaryLabel: t('workbench.changes.discard'),
  })
  if (!confirmed) return
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const discarded = await activeReader.discardPaths({
      workspaceId: props.workspaceId,
      paths: [entry.path],
    })
    await reload(true)
    const refreshed = changes.value?.entries.find(
      candidate => candidate.path === entry.path,
    )
    if (refreshed) {
      await select(refreshed)
    } else {
      selectedKey.value = ''
      diff.value = null
    }
    notice.value = t('workbench.changes.discarded', { count: discarded.length })
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

const hasStaged = computed(() => Boolean(
  changes.value?.entries.some(entry => entry.staged),
))

const canCommit = computed(() => (
  hasStaged.value && commitMessage.value.trim().length > 0
))

/**
 * Grow the commit field to the message it holds.
 *
 * A commit message is a subject plus an optional body, and a field that shows
 * one line of it with a scrollbar stub on the rounded edge is not readable.
 * The height follows the content up to the stylesheet's cap, past which the
 * field scrolls with the bar hidden.
 */
function fitCommitInput() {
  const input = commitInputRef.value
  // Zero means the field is not laid out yet (or not measurable), and writing
  // that back would collapse it.
  if (!input || input.scrollHeight === 0) return
  // `scrollHeight` measures the content box while the field is border-box, so
  // the border has to be added back or the last line is clipped by its width.
  const style = getComputedStyle(input)
  const border = (parseFloat(style.borderTopWidth) || 0)
    + (parseFloat(style.borderBottomWidth) || 0)
  input.style.height = 'auto'
  input.style.height = `${input.scrollHeight + border}px`
}

// Typing, a drafted message, and the clear after a commit all change the
// content, and the ref watcher covers the field appearing or being replaced.
watch(commitMessage, () => { void nextTick(fitCommitInput) })
watch(commitInputRef, () => { void nextTick(fitCommitInput) })

/**
 * Fill the commit field with a drafted message.
 *
 * The draft replaces what is in the field: it is a proposal, and the
 * operator's next keystroke is the edit. Whether it says anything beyond the
 * diff comes from the application setting, never from a control here. A
 * failure goes to the panel's existing error slot rather than a new one.
 */
async function draftCommitMessage() {
  const activeReader = reader
  if (!activeReader || indexBusy.value || draftingMessage.value || !hasStaged.value) return
  const requestId = ++draftRequestId
  draftingMessage.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const draft = await activeReader.draftCommitMessage({
      workspaceId: props.workspaceId,
    })
    // The model takes as long as it takes, and the index can move while it
    // writes: a message describing the previous staged patch must not land in
    // a field whose commit would now mean something else.
    if (requestId !== draftRequestId) return
    commitMessage.value = draft.body ? `${draft.subject}\n\n${draft.body}` : draft.subject
  } catch (error) {
    if (requestId !== draftRequestId) return
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.draftCommitMessageFailed')
  } finally {
    if (requestId === draftRequestId) draftingMessage.value = false
  }
}

async function commitIndex() {
  const activeReader = reader
  if (!activeReader || indexBusy.value || !canCommit.value) return
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const committed = await activeReader.commitIndex({
      workspaceId: props.workspaceId,
      message: commitMessage.value.trim(),
    })
    commitMessage.value = ''
    selectedKey.value = ''
    diff.value = null
    await reload()
    notice.value = t('workbench.changes.committed', {
      sha: committed.sha.slice(0, 7),
      subject: committed.subject,
    })
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

/**
 * Whether undoing the tip is worth offering.
 *
 * A branch with no upstream has never been pushed, so its tip is local; a
 * branch that is ahead has commits the upstream does not. Only a tip the
 * upstream already has is out of bounds, because undoing it would need a force
 * push, and the Gateway refuses that case regardless of what this says.
 */
const canUndoCommit = computed(() => {
  const value = changes.value
  if (!value?.available) return false
  if (!value.upstream) return true
  return value.ahead > 0
})

async function undoCommit() {
  const activeReader = reader
  if (!activeReader || indexBusy.value || !canUndoCommit.value) return
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const undone = await activeReader.undoCommit({ workspaceId: props.workspaceId })
    selectedKey.value = ''
    diff.value = null
    await reload()
    notice.value = t('workbench.changes.undone', {
      sha: undone.sha.slice(0, 7),
      subject: undone.subject,
    })
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

/** A section offers discard only when it holds something discardable: every
 * untracked row would have to be deleted instead, so a section of them gets no
 * button at all rather than one that does nothing. */
function canDiscardGroup(group: WorkspaceChangeGroup): boolean {
  return group.entries.some(entry => entry.changeType !== 'untracked')
}

/** Discard every worktree edit in one section, after asking. */
async function discardGroup(group: WorkspaceChangeGroup) {
  const activeReader = reader
  const paths = group.entries
    .filter(entry => entry.changeType !== 'untracked')
    .map(entry => entry.path)
  if (!activeReader || indexBusy.value || paths.length === 0) return
  const confirmed = await confirm({
    title: t('workbench.changes.discardAllTitle'),
    body: t('workbench.changes.discardAllBody', { count: paths.length }),
    primaryLabel: t('workbench.changes.discardAll'),
  })
  if (!confirmed) return
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const discarded = await activeReader.discardPaths({
      workspaceId: props.workspaceId,
      paths,
    })
    selectedKey.value = ''
    diff.value = null
    await reload()
    notice.value = t('workbench.changes.discarded', { count: discarded.length })
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

async function pushBranch() {
  const activeReader = reader
  if (!activeReader || indexBusy.value) return
  indexBusy.value = true
  indexError.value = ''
  notice.value = ''
  try {
    const pushed = await activeReader.pushBranch({ workspaceId: props.workspaceId })
    await reload(true)
    notice.value = t('workbench.changes.pushed', { upstream: pushed.upstream })
  } catch (error) {
    indexError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.indexFailed')
  } finally {
    indexBusy.value = false
  }
}

function applyGroupIndexChange(group: WorkspaceChangeGroup) {
  return runIndexChange(
    group.entries.map(entry => entry.path),
    group.indexAction === 'stage',
  )
}

async function select(entry: WorkspaceChangeEntry) {
  const activeReader = reader
  const key = entryKey(entry)
  selectedKey.value = key
  diff.value = null
  diffError.value = ''
  if (!activeReader) {
    diffError.value = t('workbench.changes.readerUnavailable')
    return
  }
  const token = ++requestToken
  diffLoading.value = true
  try {
    // The staged flag decides which half of the change is shown; an untracked
    // file has only worktree content, so it is always read unstaged.
    const result = await activeReader.readDiff({
      workspaceId: props.workspaceId,
      path: entry.path,
      staged: entry.staged && !entry.unstaged,
    })
    if (token !== requestToken) return
    diff.value = result
  } catch (error) {
    if (token !== requestToken) return
    diffError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.diffFailed')
  } finally {
    if (token === requestToken) diffLoading.value = false
  }
}

watch(() => props.workspaceId, () => { void reload() }, { immediate: true })
</script>

<style scoped>
.wb-changes {
  /* One reserved column for the row actions, two icons wide, so revealing them
     moves neither the counts beside them nor the paths before them. Rows only:
     a section header keeps its count next to its own title, so its actions can
     appear without anything else moving. */
  --wb-changes-action-slot: calc(2 * 1.375rem + var(--sp-1));
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  height: 100%;
  min-height: 0;
  padding: 0.5rem 0.75rem 0.75rem;
  color: var(--text);
  font-size: 0.8125rem;
}

.wb-changes__bar {
  display: flex;
  flex: none;
  /* The list below scrolls, so its content stops one scrollbar, one border and
     one row inset short of this row. Compensating here is what puts the icon
     controls of both rows on one right edge instead of two nearby ones. */
  padding-inline-end: calc(var(--scrollbar-width) + 1px + 0.5rem);
  /* The dock is narrow and the bar carries the whole panel's controls. When
     they no longer fit beside the branch and the totals, the controls move to
     their own row instead of squeezing the text into a clipped box. */
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  min-height: 1.75rem;
}

.wb-changes__meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}

.wb-changes__branch {
  overflow: hidden;
  color: var(--text-muted);
  font-family: var(--font-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Small labels on a surface need the secondary text token: --text-dim is a
   low-emphasis tier that drops below 4.5:1 on several themes. */
.wb-changes__divergence,
.wb-changes__count {
  flex: none;
  color: var(--text-muted);
}

.wb-changes__bar-actions {
  display: flex;
  flex: none;
  gap: 0.375rem;
  align-items: center;
  /* Keeps the controls at the trailing edge both on their own row and beside
     the branch text. */
  margin-inline-start: auto;
}

.wb-changes__action {
  display: inline-flex;
  flex: none;
  gap: 0.25rem;
  align-items: center;
  justify-content: center;
  padding: 0.125rem 0.5rem;
  color: var(--text-muted);
  font: inherit;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.wb-changes__action:disabled {
  cursor: default;
  opacity: 0.6;
}

/* An action inside a list row or a section header, at the size and treatment
   base.css already gives one (.sidebar-project-action): a transparent 20px
   square that the row reveals. */
.wb-changes__list-action {
  display: inline-flex;
  width: 20px;
  height: 20px;
  min-width: 0;
  flex: none;
  /* The row is a flex line, so without this the square stretches to its
     height; a 20x36 box is what made the hover actions look thin. */
  align-self: center;
  align-items: center;
  justify-content: center;
  padding: 0;
  color: var(--text-muted);
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.wb-changes__list-action:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.wb-changes__list-action:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg-hover);
}

.wb-changes__action:focus-visible,
.wb-changes__list-action:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.wb-changes__entry:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* Section actions are quiet until the header is pointed at or focused, the way
   a source-control list keeps its headers readable. They take the trailing edge
   on appearance; the title and its count stay where they were. */
.wb-changes__group-actions {
  display: flex;
  flex: none;
  gap: 0.125rem;
  margin-inline-start: auto;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

/* Revealed by pointing at the header, or by focusing an action itself. Not by
   `:focus-within` on the header: clicking the toggle to fold a section leaves
   focus there, which latched the actions on for that section alone — the same
   header behaving differently depending on what was last clicked. */
.wb-changes__group-head:hover .wb-changes__group-actions,
.wb-changes__group-actions:focus-within {
  opacity: 1;
}

.wb-changes__note {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin: 0;
  color: var(--text-muted);
}

.wb-changes__note--error {
  color: var(--danger);
}

.wb-changes__body {
  display: grid;
  flex: 1;
  /* The list takes the height it needs (bounded, and draggable) instead of
     reserving a fixed share of the panel. */
  grid-template-rows: var(--wb-changes-list-height, auto) 0.375rem minmax(0, 1fr);
  min-height: 0;
}

.wb-changes__splitter {
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: row-resize;
  touch-action: none;
}

.wb-changes__splitter-grip {
  width: 2.5rem;
  height: 2px;
  background: var(--border);
  border-radius: var(--radius-full);
}

.wb-changes__splitter:hover .wb-changes__splitter-grip,
.wb-changes__splitter.is-dragging .wb-changes__splitter-grip {
  background: var(--accent);
}

.wb-changes__splitter:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
  border-radius: var(--radius-sm);
}

.wb-changes__list {
  overflow-y: auto;
  max-height: 45vh;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.wb-changes__list.is-sized {
  max-height: none;
}

.wb-changes__commit {
  display: flex;
  flex: none;
  gap: 0.375rem;
  /* Both actions sit on the bottom edge, the way a composer keeps its send
     button under the text. On one row they are one cluster: splitting them
     across the box's top and bottom corners read as two unrelated controls,
     and centring them left both floating beside a tall message. */
  align-items: flex-end;
}

/* Layout only. The field's surface, border, radius and focus treatment come from
   the shared input rules (base.css and the active skin), which is what makes it
   look like every other field in the app instead of a hand-rolled one. A panel
   rule here also lost to those rules on specificity, so it was dead weight. */
.wb-changes__commit-input {
  min-width: 0;
  flex: 1;
  /* Layout only: the height follows the message (see `fitCommitInput`) up to
     this cap, past which the field scrolls. A hand-drag handle would fight
     the content-driven height, so the field does not offer one. */
  max-height: 9rem;
  resize: none;
  overflow: auto;
  /* Overflow still scrolls; only the bar is hidden, the way the workbench tab
     row hides its own — a 6px thumb inside the rounded field edge reads as a
     defect rather than a control. */
  scrollbar-width: none;
}

.wb-changes__commit-input::-webkit-scrollbar {
  display: none;
}

.wb-changes__commit-input:disabled {
  color: var(--text-muted);
}

.wb-changes__group-head {
  display: flex;
  position: sticky;
  top: 0;
  z-index: 1;
  gap: 0.375rem;
  align-items: center;
  margin: 0;
  padding: 0.25rem 0.5rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
}

/* Folding a section is how a long unstaged list stops hiding the staged and
   untracked ones. The header is one disclosure row with a rounded hover fill,
   matching the sidebar's own collapsible rows rather than inventing a chrome
   for this panel. */
.wb-changes__group-toggle {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: var(--sp-1);
  align-items: center;
  padding: var(--sp-1);
  color: inherit;
  font: inherit;
  text-align: left;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.wb-changes__group-toggle:hover,
.wb-changes__group-toggle:focus-visible {
  color: var(--text);
  background: var(--bg-elevated);
}

.wb-changes__group-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.wb-changes__group-chevron {
  flex: none;
  transition: transform var(--dur-fast);
}

.wb-changes__group-toggle[aria-expanded='true'] .wb-changes__group-chevron {
  transform: rotate(90deg);
}

.wb-changes__group-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* A count, not a control: plain secondary text immediately after the title, the
   way a source-control section reads ("Changes 3"). Right-aligning it left a
   void between the two, which read as a missing column rather than a count. */
.wb-changes__group-count {
  flex: none;
  color: var(--text-muted);
  font-weight: 500;
}

.wb-changes__row {
  display: flex;
  align-items: center;
  padding-inline-end: 0.5rem;
  border-left: 2px solid transparent;
}

.wb-changes__row:hover {
  background: var(--bg-elevated);
}

.wb-changes__row.is-selected {
  background: var(--bg-elevated);
  border-left-color: var(--accent);
}

.wb-changes__entry {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  min-width: 0;
  flex: 1;
  padding: 0.25rem 0.25rem 0.25rem 0.5rem;
  color: var(--text);
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}

/* The actions live in their own reserved column to the right of the line
   counts: nothing is covered and nothing shifts, which is the only way to have
   both. The column is reserved rather than filled on demand, and rows never
   wrap, so revealing it cannot change a row's height either. */
.wb-changes__row-actions {
  display: flex;
  flex: none;
  width: var(--wb-changes-action-slot);
  justify-content: flex-end;
  gap: 0.125rem;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

.wb-changes__row-action {
  /* An action, not a label: the muted tier is for metadata, so the glyph keeps
     the normal foreground. No chip by default — the row already swapped its
     counts out, so a filled box would be a second thing to look at. */
  color: var(--text);
  background: none;
  border-color: transparent;
}

/* Same rule as a section header: the pointer reveals, the action's own focus
   reveals, and the row under review keeps its actions. */
.wb-changes__row:hover .wb-changes__row-actions,
.wb-changes__row-actions:focus-within,
.wb-changes__row.is-selected .wb-changes__row-actions {
  opacity: 1;
}

.wb-changes__row-action:hover:not(:disabled) {
  background: var(--bg-surface);
  border-color: var(--border);
}

.wb-changes__row-action:disabled {
  cursor: default;
  opacity: 0.5;
}

.wb-changes__row-action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.wb-changes__type {
  flex: none;
  width: 1.125rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-weight: 600;
  text-align: center;
}

.wb-changes__type[data-type="added"],
.wb-changes__type[data-type="untracked"] {
  color: var(--syntax-string);
}

.wb-changes__type[data-type="deleted"],
.wb-changes__type[data-type="unmerged"] {
  color: var(--danger);
}

.wb-changes__type[data-type="renamed"],
.wb-changes__type[data-type="copied"] {
  color: var(--accent);
}

.wb-changes__path {
  display: flex;
  overflow: hidden;
  min-width: 0;
  font-family: var(--font-mono);
  white-space: nowrap;
}

.wb-changes__path-dir {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
}

.wb-changes__path-base {
  flex: none;
  color: var(--text);
}

/* The trailing column holds one cluster at a time: the line counts and state
   at rest, the actions once the row is hovered or selected. Swapping (rather
   than drawing the actions over the counts) is what keeps it readable — the
   counts are hidden, not showing through the gaps between the icons. */
.wb-changes__row-status {
  display: flex;
  flex: none;
  gap: 0.375rem;
  align-items: center;
  margin-left: auto;
}



.wb-changes__stats {
  display: flex;
  flex: none;
  gap: 0.25rem;
  font-family: var(--font-mono);
  font-size: 0.6875rem;
}

.wb-changes__stats + .wb-changes__both,
.wb-changes__stats + .wb-changes__check {
  margin-left: 0.375rem;
}

.wb-changes__both {
  flex: none;
  margin-left: auto;
  padding: 0 0.3125rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.wb-changes__check {
  flex: none;
  margin-left: auto;
  color: var(--syntax-string);
}

.wb-changes__diff {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  min-height: 0;
}

.wb-changes__diff-head {
  display: flex;
  flex: none;
  gap: 0.5rem;
  align-items: center;
  padding: 0 0.125rem;
  font-family: var(--font-mono);
  font-size: 0.75rem;
}

.wb-changes__diff-path {
  overflow: hidden;
  color: var(--text);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* One wrap preference governs the whole panel: the file rows, the diff header,
   and the patch body. A path long enough to need wrapping is long in whichever
   column shows it, and a header that truncates what the body wraps is one more
   inconsistency to decode. */
/* A list row is one line, always: a wrapped path would change the row's height
   and with it the reserved action column's alignment. The name truncates
   instead, which is what a source-control list does. Wrapping belongs to the
   patch, where a long line has nowhere else to go. */
.wb-changes.is-wrapped .wb-changes__diff-path {
  overflow: visible;
  text-overflow: clip;
  white-space: normal;
  overflow-wrap: anywhere;
}

.wb-changes__diff-side {
  flex: none;
  padding: 0 0.3125rem;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

/* The prompt shares the row so the pager is never a lone control above an
   empty pane. */
.wb-changes__diff-prompt {
  overflow: hidden;
  color: var(--text-muted);
  font-family: var(--font-sans);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* The +N/-N counts are metadata; the patch itself carries the add/remove
   colour. Colouring 12px counts with --syntax-string lands at 4.46:1 on one
   theme, just under the 4.5:1 floor for body text. */
.wb-changes__added,
.wb-changes__removed {
  color: var(--text-muted);
}

.wb-changes__code {
  overflow: auto;
  /* Hug the patch: a short diff should not sit inside a mostly empty frame,
     and a long one scrolls once it reaches the pane height. */
  flex: 0 1 auto;
  max-height: 100%;
  min-height: 0;
  padding: 0.25rem 0;
  font-family: var(--font-mono);
  font-size: 0.75rem;
  line-height: 1.5;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.wb-changes__line {
  display: flex;
  align-items: baseline;
  white-space: pre;
}

/* Wrapping is the default because the dock is often narrower than the patch;
   horizontal scrolling stays available by turning it off. */
.wb-changes.is-wrapped .wb-changes__line,
.wb-changes.is-wrapped .wb-changes__line-code {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* Added and removed lines carry the add/remove colour, the way every diff view
   does. The colour is the row background plus a left stripe — the text itself
   stays in the normal foreground, because coloured text on a tint of the same
   colour is the pairing that drops below the contrast floor. */
.wb-changes__line[data-kind="added"] {
  background: color-mix(in srgb, var(--syntax-string) 26%, transparent);
  border-left: 2px solid var(--syntax-string);
}

.wb-changes__line[data-kind="removed"] {
  background: color-mix(in srgb, var(--danger) 26%, transparent);
  border-left: 2px solid var(--danger);
}

.wb-changes__line[data-kind="context"],
.wb-changes__line[data-kind="notice"] {
  border-left: 2px solid transparent;
}

.wb-changes__line[data-kind="hunk"] {
  margin: 0.125rem 0;
  /* --syntax-comment is a comment tier: it falls to 1.68:1 on a light
     elevated band, so the hunk header uses the secondary text token. */
  color: var(--text-muted);
  background: var(--bg-elevated);
}

.wb-changes__line[data-kind="notice"] {
  color: var(--text-muted);
}

/* Sized to the digits, not to a fixed box: the two numbers belong next to each
   other, and a fixed 2.375rem column left a wide gap between them. */
.wb-changes__gutter {
  flex: none;
  width: 2.75ch;
  color: var(--text-muted);
  text-align: right;
  user-select: none;
}

.wb-changes__gutter + .wb-changes__gutter {
  margin-left: 0.5rem;
  margin-right: 0.625rem;
}

/* The marker keeps the row's own colour instead of an add/remove accent: a
   green marker on a green tint (and red on red) measures as low as 3.71:1,
   because those two accent tokens are chosen against the plain surface. */

.wb-changes__line-code {
  padding-right: 0.5rem;
  font: inherit;
  color: inherit;
}
</style>
