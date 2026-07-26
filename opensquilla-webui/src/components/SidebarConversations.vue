<script lang="ts">
import type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

export type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

/** Legacy family id kept for the agent-initial filter callers. */
export type SidebarFamilyId = SidebarSectionFamily

/**
 * A rendered sidebar row: the pure `SidebarSectionRow` produced by
 * `arrangeSidebarSections`, with `agentName` resolved by App.vue (the composable
 * leaves it empty so the display-name lookup stays in one place).
 */
export type SidebarConversationItem = SidebarSectionRow

const COLLAPSE_STORAGE_KEY = 'opensquilla-sidebar-sections'

export function readSidebarCollapsedState(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed as Record<string, boolean> : {}
  } catch {
    return {}
  }
}

function writeSidebarCollapsedState(state: Record<string, boolean>) {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}

export { COLLAPSE_STORAGE_KEY, writeSidebarCollapsedState }
export type { SidebarSection as SidebarSectionType }
</script>

<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from './Icon.vue'
import { useConfirm } from '@/composables/useConfirm'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { shouldShowAgentFilterBadge } from '@/utils/sidebarConversations'

const props = defineProps<{
  sections: SidebarSection[]
  error: boolean
  loading: boolean
  currentKey: string
  contractDebugEnabled: boolean
}>()

const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'refresh'): void
  (e: 'rename', payload: { key: string; title: string }): void
  (e: 'delete', key: string): void
  (e: 'bulk-delete', keys: string[]): void
  (e: 'new-chat'): void
}>()

const { confirm } = useConfirm()
const { t } = useI18n()

/* ── Agent filter (lives within the Chats section) ─────────────────── */

const agentFilter = ref('')

function toggleAgentFilter(agentId: string) {
  agentFilter.value = agentFilter.value === agentId ? '' : agentId
}

function clearAgentFilter() {
  agentFilter.value = ''
}

const agentFilterName = computed(() => {
  if (!agentFilter.value) return ''
  for (const section of props.sections) {
    const match = section.rows.find(row => row.effectiveAgentId === agentFilter.value)
    if (match) return match.agentName || agentFilter.value
  }
  return agentFilter.value
})

function agentInitial(name: string): string {
  return name.trim().charAt(0).toUpperCase() || '?'
}

function isWorkspaceRow(row: SidebarConversationItem): boolean {
  return row.rowKind === 'workspace'
}

function filterChatRowsByAgent(rows: SidebarConversationItem[], agentId: string): SidebarConversationItem[] {
  const result: SidebarConversationItem[] = []
  let pendingWorkspace: SidebarConversationItem | null = null
  let pendingWorkspaceHasMatch = false

  const flushPendingWorkspace = () => {
    if (pendingWorkspace && pendingWorkspaceHasMatch) result.push(pendingWorkspace)
    pendingWorkspace = null
    pendingWorkspaceHasMatch = false
  }

  for (const row of rows) {
    if (isWorkspaceRow(row)) {
      flushPendingWorkspace()
      pendingWorkspace = row
      continue
    }
    if (row.effectiveAgentId !== agentId) continue
    if (pendingWorkspace && !pendingWorkspaceHasMatch) {
      result.push(pendingWorkspace)
      pendingWorkspaceHasMatch = true
    }
    result.push(row)
  }
  flushPendingWorkspace()
  return result
}

/* ── Collapsible sections ──────────────────────────────────────────── */

// Persisted collapse state, keyed by family. A family is open unless an
// explicit `true` (collapsed) flag was stored for it; Chats opens by default.
const collapsed = ref<Record<string, boolean>>(readSidebarCollapsedState())

function isCollapsed(family: SidebarFamilyId): boolean {
  return collapsed.value[family] === true
}

function toggleSection(family: SidebarFamilyId) {
  const next = { ...collapsed.value, [family]: !isCollapsed(family) }
  collapsed.value = next
  writeSidebarCollapsedState(next)
}

// Sections with at least one row, honoring the agent filter inside Chats.
const visibleSections = computed(() => {
  return props.sections
    .map(section => ({
      ...section,
      rows: section.family === 'chats' && agentFilter.value
        ? filterChatRowsByAgent(section.rows, agentFilter.value)
        : section.rows,
    }))
    .filter(section => section.rows.some(row => !isWorkspaceRow(row)))
})

// Total rendered rows: drives the onboarding empty-state and the filter's
// "No matches" message separately from a true first-run empty list.
const totalRows = computed(() =>
  props.sections.reduce((sum, section) => sum + section.rows.filter(row => !isWorkspaceRow(row)).length, 0),
)

const hasFilterMatches = computed(() =>
  visibleSections.value.some(section => section.rows.some(row => !isWorkspaceRow(row))),
)

/* ── Bulk selection ───────────────────────────────────────────────── */

const selectedKeys = ref<Set<string>>(new Set())
const selectionMode = ref(false)

const visibleSelectableRows = computed(() =>
  visibleSections.value.flatMap(section =>
    isCollapsed(section.family) ? [] : section.rows.filter(row => !isWorkspaceRow(row)),
  ),
)

const visibleSelectableKeySet = computed(() =>
  new Set(visibleSelectableRows.value.map(row => row.key)),
)

const selectedCount = computed(() => selectedKeys.value.size)
const visibleSelectableCount = computed(() => visibleSelectableRows.value.length)

const allVisibleSelected = computed(() =>
  visibleSelectableCount.value > 0
  && visibleSelectableRows.value.every(row => selectedKeys.value.has(row.key)),
)

watch(visibleSelectableKeySet, (keys) => {
  const next = new Set([...selectedKeys.value].filter(key => keys.has(key)))
  if (next.size !== selectedKeys.value.size) selectedKeys.value = next
})

function isRowSelected(key: string): boolean {
  return selectedKeys.value.has(key)
}

function setRowSelected(key: string, checked: boolean) {
  const next = new Set(selectedKeys.value)
  if (checked) next.add(key)
  else next.delete(key)
  selectedKeys.value = next
}

function toggleVisibleSelection() {
  const checked = !allVisibleSelected.value
  const next = new Set(selectedKeys.value)
  for (const row of visibleSelectableRows.value) {
    if (checked) next.add(row.key)
    else next.delete(row.key)
  }
  selectedKeys.value = next
}

function clearSelection() {
  selectedKeys.value = new Set()
}

function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value
  if (!selectionMode.value) clearSelection()
}

async function requestBulkDelete() {
  closeMenu()
  const keys = [...selectedKeys.value].filter(key => visibleSelectableKeySet.value.has(key))
  if (keys.length === 0) return
  const ok = await confirm({
    title: t('shared.sidebar.bulkDeleteTitle'),
    body: t('shared.sidebar.bulkDeleteBody', { count: keys.length }),
    primaryLabel: t('shared.sidebar.bulkDeleteConfirm'),
  })
  if (!ok) return
  clearSelection()
  selectionMode.value = false
  emit('bulk-delete', keys)
}

/* ── Per-row ⋯ menu + inline rename ────────────────────────────────── */

const openMenuKey = ref('')
// The ⋯ trigger that opened the active menu, captured so Escape can return
// focus to it. A function-ref on the single open .sidebar-row-menu scopes the
// roving-focus queries (only one menu renders at a time).
const menuTriggerEl = ref<HTMLElement | null>(null)
const openMenuEl = ref<HTMLElement | null>(null)
function setOpenMenu(el: Element | ComponentPublicInstance | null) {
  openMenuEl.value = el instanceof HTMLElement ? el : null
}
// Fixed-position style for the teleported menu, computed from the trigger rect
// on open so the menu escapes the Recents scroll-clip.
const menuStyle = ref<Record<string, string>>({})
const renamingKey = ref('')
const renameDraft = ref('')
// A function ref captures the single active rename input. A string ref inside
// the v-for would collect into an array even though only one input renders, so
// the explicit callback keeps a direct element handle for focus/select.
const renameInputEl = ref<HTMLInputElement | null>(null)
function setRenameInput(el: Element | ComponentPublicInstance | null) {
  renameInputEl.value = el instanceof HTMLInputElement ? el : null
}
// Guards the blur-saves behavior so an Enter/Esc keystroke does not also fire a
// duplicate save through the input's blur handler.
let renameCommitting = false

function toggleMenu(key: string, event?: Event) {
  if (openMenuKey.value === key) {
    closeMenu()
    return
  }
  openMenuKey.value = key
  const trigger = event?.currentTarget
  menuTriggerEl.value = trigger instanceof HTMLElement ? trigger : null
  // The menu is teleported to <body>; anchor it to the trigger, flipping upward
  // near the viewport bottom so the Delete item is never clipped off-screen.
  if (menuTriggerEl.value) {
    const r = menuTriggerEl.value.getBoundingClientRect()
    const openUp = r.bottom + 100 > window.innerHeight
    menuStyle.value = {
      position: 'fixed',
      left: `${r.right}px`,
      top: `${openUp ? r.top : r.bottom + 4}px`,
      transform: openUp ? 'translate(-100%, -100%)' : 'translateX(-100%)',
    }
  }
  // Move focus into the menu so keyboard users land on an actionable item.
  nextTick(() => {
    const items = openMenuEl.value?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item')
    items?.[0]?.focus()
  })
}

function closeMenu() {
  openMenuKey.value = ''
  openMenuEl.value = null
  menuTriggerEl.value = null
}

// Escape closes and returns focus to the row's ⋯ trigger; arrows rove between
// the menu items, wrapping at the ends.
function onMenuKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    const trigger = menuTriggerEl.value
    closeMenu()
    nextTick(() => trigger?.focus())
    return
  }
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  const items = Array.from(
    openMenuEl.value?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item') ?? [],
  )
  if (!items.length) return
  e.preventDefault()
  const current = items.indexOf(document.activeElement as HTMLElement)
  const delta = e.key === 'ArrowDown' ? 1 : -1
  const next = (current + delta + items.length) % items.length
  items[next]?.focus()
}

useDocumentEvent('click', (e) => {
  if (!openMenuKey.value) return
  if (e.target instanceof Node) {
    const host = (e.target as Element).closest?.('.sidebar-row-menu-wrap, .sidebar-row-menu')
    if (host) return
  }
  closeMenu()
})

function startRename(row: SidebarConversationItem) {
  closeMenu()
  renamingKey.value = row.key
  renameDraft.value = row.title
  renameCommitting = false
  nextTick(() => {
    renameInputEl.value?.focus()
    renameInputEl.value?.select()
  })
}

function commitRename() {
  if (renameCommitting) return
  const key = renamingKey.value
  if (!key) return
  renameCommitting = true
  const title = renameDraft.value.trim()
  const original = props.sections
    .flatMap(section => section.rows)
    .find(row => row.key === key)?.title || ''
  renamingKey.value = ''
  renameDraft.value = ''
  if (title && title !== original) emit('rename', { key, title })
}

function cancelRename() {
  renameCommitting = true
  renamingKey.value = ''
  renameDraft.value = ''
}

function onRenameBlur() {
  // Enter/Esc already settled this row; only a genuine focus-loss commits.
  if (renameCommitting) return
  commitRename()
}

async function requestDelete(row: SidebarConversationItem) {
  closeMenu()
  const ok = await confirm({
    title: t('shared.sidebar.deleteSessionTitle'),
    body: t('shared.sidebar.deleteSessionBody', { title: row.title }),
    primaryLabel: t('shared.sidebar.deleteSessionConfirm'),
  })
  if (!ok) return
  emit('delete', row.key)
}

function onSelectRow(row: SidebarConversationItem) {
  if (renamingKey.value === row.key) return
  if (selectionMode.value) {
    setRowSelected(row.key, !isRowSelected(row.key))
    return
  }
  emit('select', row.key)
}
</script>

<template>
  <div
    class="sidebar-section sidebar-history"
    :class="{ 'is-selecting': selectionMode }"
    :aria-label="t('shared.sidebar.recentConversations')"
  >
    <div class="sidebar-recents-header">
      <span class="sidebar-recents-eyebrow">
        {{
          selectionMode
            ? selectedCount > 0
              ? t('shared.sidebar.selectedCountLabel', { count: selectedCount })
              : t('shared.sidebar.selectionModeLabel')
            : visibleSections.length === 1
              ? visibleSections[0].label
              : t('shared.sidebar.recents')
        }}
      </span>
      <span
        v-if="!selectionMode && visibleSections.length === 1 && totalRows > 0"
        class="sidebar-recents-count"
      >{{ totalRows }}</span>
      <button
        v-if="selectionMode"
        type="button"
        class="sidebar-select-all-btn"
        :disabled="visibleSelectableCount === 0"
        :aria-label="allVisibleSelected ? t('shared.sidebar.clearVisibleSelection') : t('shared.sidebar.selectVisible')"
        :title="allVisibleSelected ? t('shared.sidebar.clearVisibleSelection') : t('shared.sidebar.selectVisible')"
        @click="toggleVisibleSelection"
      >
        {{ allVisibleSelected ? t('shared.sidebar.clearAllShort') : t('shared.sidebar.selectAllShort') }}
      </button>
      <button
        v-if="selectionMode && selectedCount > 0"
        type="button"
        class="sidebar-bulk-delete-btn"
        :aria-label="t('shared.sidebar.deleteSelectedAria', { count: selectedCount })"
        :title="t('shared.sidebar.deleteSelectedAria', { count: selectedCount })"
        @click="requestBulkDelete"
      >
        <Icon name="trash" :size="12" />
      </button>
      <button
        v-if="totalRows > 0"
        type="button"
        class="sidebar-bulk-mode-btn"
        :class="{ 'is-active': selectionMode }"
        :aria-pressed="selectionMode"
        :aria-label="selectionMode ? t('shared.sidebar.exitSelectionMode') : t('shared.sidebar.enterSelectionMode')"
        :title="selectionMode ? t('shared.sidebar.exitSelectionMode') : t('shared.sidebar.enterSelectionMode')"
        @click="toggleSelectionMode"
      >
        <Icon :name="selectionMode ? 'x' : 'listChecks'" :size="13" />
      </button>
      <button
        class="sidebar-refresh-btn"
        :title="t('shared.sidebar.refresh')"
        :aria-label="t('shared.sidebar.refresh')"
        :class="{ spinning: loading }"
        @click="emit('refresh')"
      >
        <Icon name="refresh" :size="12" />
      </button>
    </div>

    <div v-if="agentFilter" class="sidebar-filter-row">
      <button
        type="button"
        class="sidebar-agent-chip"
        :aria-label="t('shared.sidebar.clearAgentFilter', { name: agentFilterName })"
        @click="clearAgentFilter"
      >
        {{ agentFilterName }} <span aria-hidden="true">&times;</span>
      </button>
    </div>

    <div v-if="error" class="sidebar-history-empty">
      {{ t('shared.sidebar.loadError') }}
    </div>

    <!-- Filtered to nothing within the Chats agent filter -->
    <div v-else-if="agentFilter && !hasFilterMatches" class="sidebar-history-empty">
      {{ t('shared.sidebar.noMatches') }}
    </div>

    <!-- First-run onboarding: no sessions exist yet -->
    <div v-else-if="totalRows === 0" class="sidebar-onboarding">
      <p class="sidebar-onboarding__lead">{{ t('shared.sidebar.noConversations') }}</p>
      <button type="button" class="sidebar-onboarding__cta" @click="emit('new-chat')">
        <Icon name="plus" :size="14" />
        <span>{{ t('shared.sidebar.startChat') }}</span>
      </button>
      <div class="sidebar-onboarding__links">
        <router-link to="/sessions" class="sidebar-onboarding__link">{{ t('shared.sidebar.linkSessions') }}</router-link>
        <router-link to="/overview" class="sidebar-onboarding__link">{{ t('shared.sidebar.linkOverview') }}</router-link>
      </div>
    </div>

    <div v-else class="sidebar-history-list">
      <div
        v-for="section in visibleSections"
        :key="section.family"
        class="sidebar-group"
        :data-family="section.family"
      >
        <!-- One vocabulary, one header: with a single family the panel's own
             "Chats" eyebrow already labels the list, so the per-family header
             renders only when there are actually multiple families to tell
             apart (chats vs cron vs channels). -->
        <button
          v-if="visibleSections.length > 1"
          type="button"
          class="sidebar-group__header"
          :aria-expanded="!isCollapsed(section.family)"
          :aria-controls="`sidebar-group-${section.family}`"
          @click="toggleSection(section.family)"
        >
          <Icon class="sidebar-group__chevron" name="chevronRight" :size="12" />
          <span class="sidebar-group__label">{{ section.label }}</span>
          <span class="sidebar-group__count">{{ section.rows.length }}</span>
        </button>

        <div
          v-show="visibleSections.length === 1 || !isCollapsed(section.family)"
          :id="`sidebar-group-${section.family}`"
          class="sidebar-group__body"
        >
          <div
            v-for="row in section.rows"
            :key="row.key"
            class="sidebar-history-row"
            :class="{
              'is-selected': row.rowKind !== 'workspace' && isRowSelected(row.key),
              'sidebar-history-row--workspace': row.rowKind === 'workspace',
            }"
            :data-family="section.family"
            :data-depth="row.depth"
            :style="{ '--row-depth': row.depth }"
          >
            <div
              v-if="row.rowKind === 'workspace'"
              class="sidebar-workspace-header"
              :title="row.workspaceDisplayPath || row.title"
            >
              <span class="sidebar-workspace-label">{{ row.title }}</span>
            </div>

            <span
              v-if="row.depth > 0 && row.rowKind !== 'workspace'"
              class="sidebar-history-rail"
              aria-hidden="true"
            />

            <!-- Inline rename input replaces the row button while editing -->
            <input
              v-if="row.rowKind !== 'workspace' && renamingKey === row.key"
              :ref="setRenameInput"
              v-model="renameDraft"
              class="sidebar-history-rename"
              type="text"
              :aria-label="t('shared.sidebar.renameLabel', { title: row.title })"
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="cancelRename"
              @blur="onRenameBlur"
            />

            <button
              v-else-if="row.rowKind !== 'workspace'"
              class="sidebar-history-item"
              :class="{ 'is-current': row.key === currentKey }"
              :title="row.title"
              :aria-pressed="selectionMode ? isRowSelected(row.key) : undefined"
              @click="onSelectRow(row)"
            >
              <span
                v-if="selectionMode"
                class="sidebar-selection-box"
                :class="{ 'is-checked': isRowSelected(row.key) }"
                aria-hidden="true"
              >
                <Icon v-if="isRowSelected(row.key)" name="check" :size="11" />
              </span>
              <span
                v-else
                class="sidebar-history-dot"
                :class="`status--${row.runStatus}`"
                role="img"
                :aria-label="t('shared.sidebar.statusLabel', { status: row.runLabel })"
              />
              <span class="sidebar-history-title">{{ row.title }}</span>
              <span
                v-if="contractDebugEnabled && row.hasContractGaps"
                class="sidebar-history-gap"
                :aria-label="t('shared.sidebar.contractGap')"
                :title="t('shared.sidebar.contractGap')"
              >{{ t('shared.sidebar.contractGapBadge') }}</span>
              <span v-if="row.runStatus !== 'idle'" class="sidebar-history-run">{{ row.runLabel }}</span>
            </button>

            <!-- Chat-only ⋯ menu: rename + delete -->
            <div
              v-if="row.rowKind !== 'workspace' && row.sessionKind === 'chat' && renamingKey !== row.key && !selectionMode"
              class="sidebar-row-menu-wrap"
            >
              <button
                type="button"
                class="sidebar-row-menu-btn"
                aria-haspopup="menu"
                :aria-expanded="openMenuKey === row.key"
                :aria-label="t('shared.sidebar.rowActions', { title: row.title })"
                :title="t('shared.sidebar.rowActions', { title: row.title })"
                @click.stop="toggleMenu(row.key, $event)"
              >
                <span aria-hidden="true">&#8943;</span>
              </button>
              <Teleport to="body">
              <div
                v-if="openMenuKey === row.key"
                :ref="setOpenMenu"
                class="sidebar-row-menu"
                :style="menuStyle"
                role="menu"
                :aria-label="t('shared.sidebar.rowActions', { title: row.title })"
                @keydown="onMenuKeydown"
              >
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  role="menuitem"
                  @click.stop="startRename(row)"
                >
                  <Icon name="pencil" :size="14" />
                  <span>{{ t('shared.sidebar.rename') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item sidebar-row-menu__item--danger"
                  role="menuitem"
                  @click.stop="requestDelete(row)"
                >
                  <Icon name="trash" :size="14" />
                  <span>{{ t('shared.sidebar.delete') }}</span>
                </button>
              </div>
              </Teleport>
            </div>

            <!-- Agent-initial badge: indicator + click-to-filter (Chats only) -->
            <button
              v-else-if="row.rowKind !== 'workspace' && shouldShowAgentFilterBadge(section.family, row) && renamingKey !== row.key && !selectionMode"
              type="button"
              class="sidebar-agent-badge"
              :class="{ 'is-active': agentFilter === row.effectiveAgentId }"
              :aria-pressed="agentFilter === row.effectiveAgentId"
              :aria-label="t('shared.sidebar.filterByAgent', { name: row.agentName })"
              :title="t('shared.sidebar.filterByAgent', { name: row.agentName })"
              @click.stop="toggleAgentFilter(row.effectiveAgentId)"
            >
              {{ agentInitial(row.agentName) }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
