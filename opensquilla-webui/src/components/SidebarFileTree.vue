<script setup lang="ts">
/**
 * Workspace file-tree sidebar view.
 *
 * Render approach (virtualized flat rows + per-directory lazy loading) is
 * adapted from anomalyco/opencode `packages/app/src/components/file-tree-v2.tsx`
 * (MIT, Copyright (c) 2025 opencode). See THIRD_PARTY_NOTICES.md.
 */
import { computed, inject, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useVirtualizer } from '@tanstack/vue-virtual'

import Icon from '@/components/Icon.vue'
import { useFileTreeStore, type FileTreeWorkspace } from '@/stores/fileTree'
import { normalizeFileTreePath } from '@/lib/fileTreeModel'
import { WORKSPACE_FILES_KEY } from '@/modules/workspaceFiles'

const props = defineProps<{
  workspace: FileTreeWorkspace
  /** Path currently highlighted as "being worked on" (optional). */
  activePath?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'preview', payload: { workspace: FileTreeWorkspace; path: string }): void
  (e: 'attach', payload: {
    workspace: FileTreeWorkspace
    path: string
    name: string
    size?: number
  }): void
}>()

const { t } = useI18n()
const store = useFileTreeStore()

// The store stays transport-agnostic; the app-level WorkspaceFiles seam is
// attached here before the immediate workspace watcher triggers the root
// listing.
const workspaceFiles = inject(WORKSPACE_FILES_KEY, null)
if (workspaceFiles) store.attachFiles(workspaceFiles)

const scroller = ref<HTMLElement | null>(null)
const virtualizer = useVirtualizer(
  computed(() => ({
    count: store.rows.length,
    getScrollElement: () => scroller.value,
    estimateSize: () => 28,
    overscan: 10,
    getItemKey: (index: number) => store.rows[index]?.node.path ?? index,
  })),
)

const activePath = computed(() => normalizeFileTreePath(props.activePath ?? ''))

function openWorkspace(): void {
  store.openWorkspace(props.workspace)
}

// Re-open (and re-fetch) when the sidebar returns to this workspace view.
watch(
  () => props.workspace,
  (ws) => {
    if (ws) openWorkspace()
  },
  { immediate: true },
)

// Scroll the active file into view when it appears (first time only per path).
let scrolledActive: string | undefined
watch(
  [activePath, () => store.rows.length],
  async () => {
    const path = activePath.value
    if (!path || scrolledActive === path) return
    const index = store.rows.findIndex((row) => row.node.path === path)
    if (index < 0) return
    scrolledActive = path
    await nextTick()
    virtualizer.value.scrollToIndex(index, { align: 'auto' })
  },
)

function onRowClick(path: string, type: 'file' | 'directory') {
  if (type === 'directory') {
    store.toggleDir(path)
    return
  }
  emit('preview', { workspace: props.workspace, path })
}

function onAttachClick(event: Event, path: string, name: string, size?: number) {
  event.stopPropagation()
  emit('attach', { workspace: props.workspace, path, name, size })
}

function copyPath(event: Event, path: string) {
  event.stopPropagation()
  const absolute = path ? `${props.workspace.path.replace(/[\\/]+$/, '')}/${path}` : props.workspace.path
  void navigator.clipboard?.writeText(absolute).catch(() => {
    /* clipboard unavailable (permissions) — no-op for v1 */
  })
}
</script>

<template>
  <div class="file-tree" data-testid="workspace-file-tree">
    <div class="file-tree__header">
      <button
        type="button"
        class="file-tree__back"
        data-testid="file-tree-back"
        :aria-label="t('fileTree.backToTasks')"
        @click="emit('close')"
      >
        <Icon name="chevronLeft" :size="14" />
        <span class="file-tree__back-label">{{ t('fileTree.backToTasks') }}</span>
      </button>
      <button
        type="button"
        class="file-tree__refresh"
        data-testid="file-tree-refresh"
        :disabled="store.rootLoading"
        :title="t('fileTree.refresh')"
        :aria-label="t('fileTree.refresh')"
        @click="() => void store.refreshAll()"
      >
        <Icon name="refresh" :size="13" />
      </button>
    </div>

    <div class="file-tree__workspace" :title="workspace.path">
      <Icon name="folder" :size="13" />
      <span class="file-tree__workspace-name">{{ workspace.name }}</span>
    </div>

    <div v-if="store.rootError" class="file-tree__state" role="alert">
      <p class="file-tree__state-text">{{ store.rootError }}</p>
      <button type="button" class="file-tree__retry" @click="() => void store.refreshAll()">
        {{ t('fileTree.retry') }}
      </button>
    </div>

    <div v-else-if="store.ready && store.rows.length === 0" class="file-tree__state">
      <p class="file-tree__state-text">{{ t('fileTree.empty') }}</p>
    </div>

    <div v-else ref="scroller" class="file-tree__scroller">
      <div
        v-if="store.rootLoading && store.rows.length === 0"
        class="file-tree__state file-tree__state--loading"
      >
        {{ t('fileTree.loading') }}
      </div>
      <div
        class="file-tree__viewport"
        :style="{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }"
      >
        <div
          v-for="item in virtualizer.getVirtualItems()"
          :key="String(item.key)"
          class="file-tree__row"
          data-slot="file-tree-row"
          :data-path="String(item.key)"
          :data-selected="item.key === activePath ? '' : undefined"
          :style="{
            position: 'absolute',
            top: '0',
            left: '0',
            width: '100%',
            height: `${item.size}px`,
            transform: `translateY(${item.start}px)`,
          }"
        >
          <template v-if="store.rows[item.index]">
            <button
              v-if="store.rows[item.index].node.type === 'directory'"
              type="button"
              class="file-tree__item"
              :aria-expanded="store.dirState(store.rows[item.index].node.path)?.expanded ? 'true' : 'false'"
              :style="{ paddingLeft: `${10 + store.rows[item.index].level * 14}px` }"
              @click="
                () =>
                  onRowClick(
                    store.rows[item.index].node.path,
                    store.rows[item.index].node.type,
                  )
              "
            >
              <Icon
                :name="store.dirState(store.rows[item.index].node.path)?.expanded ? 'chevronDown' : 'chevronRight'"
                :size="11"
                class="file-tree__chevron"
              />
              <Icon name="folder" :size="14" class="file-tree__icon" />
              <span class="file-tree__name">
                <bdi dir="auto">{{ store.rows[item.index].node.name }}</bdi>
              </span>
            </button>
            <button
              v-else
              type="button"
              class="file-tree__item"
              :style="{ paddingLeft: `${22 + store.rows[item.index].level * 14}px` }"
              @click="
                () =>
                  onRowClick(
                    store.rows[item.index].node.path,
                    store.rows[item.index].node.type,
                  )
              "
            >
              <Icon name="fileText" :size="14" class="file-tree__icon" />
              <span class="file-tree__name">
                <bdi dir="auto">{{ store.rows[item.index].node.name }}</bdi>
              </span>
              <span class="file-tree__row-actions">
                <button
                  type="button"
                  class="file-tree__row-action"
                  :title="t('fileTree.attachToChat')"
                  :aria-label="t('fileTree.attachToChat')"
                  @click="
                    (event: Event) =>
                      onAttachClick(
                        event,
                        store.rows[item.index].node.path,
                        store.rows[item.index].node.name,
                        store.rows[item.index].node.size,
                      )
                  "
                >
                  <Icon name="plus" :size="11" />
                </button>
                <button
                  type="button"
                  class="file-tree__row-action"
                  :title="t('fileTree.copyPath')"
                  :aria-label="t('fileTree.copyPath')"
                  @click="(event: Event) => copyPath(event, store.rows[item.index].node.path)"
                >
                  <Icon name="copy" :size="11" />
                </button>
              </span>
            </button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-tree {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.file-tree__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
}

.file-tree__back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 0;
  background: transparent;
  color: var(--sidebar-text-soft);
  font-size: 12px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.file-tree__back:hover,
.file-tree__back:focus-visible {
  background: var(--sidebar-item-hover);
  color: var(--sidebar-text-strong);
}

.file-tree__refresh {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 0;
  background: transparent;
  color: var(--sidebar-text-soft);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.file-tree__refresh:hover:not(:disabled),
.file-tree__refresh:focus-visible {
  background: var(--sidebar-item-hover);
  color: var(--sidebar-text-strong);
}

.file-tree__refresh:disabled {
  opacity: 0.4;
  cursor: default;
}

.file-tree__workspace {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 12px 8px;
  color: var(--sidebar-text-soft);
  min-width: 0;
}

.file-tree__workspace-name {
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-tree__scroller {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 0 4px 8px;
}

.file-tree__row {
  display: block;
}

.file-tree__item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  height: 100%;
  border: 0;
  background: transparent;
  color: var(--sidebar-text);
  font-size: 13px;
  text-align: start;
  border-radius: var(--radius-sm);
  cursor: pointer;
  position: relative;
}

.file-tree__item:hover {
  background: var(--sidebar-item-hover);
}

.file-tree__item:hover .file-tree__row-actions {
  opacity: 1;
}

.file-tree__row[data-selected] .file-tree__item {
  background: var(--sidebar-selection-bg);
  color: var(--sidebar-text-strong);
}

.file-tree__chevron {
  flex-shrink: 0;
  color: var(--sidebar-text-soft);
}

.file-tree__icon {
  flex-shrink: 0;
  color: var(--sidebar-text-soft);
}

.file-tree__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-tree__row-actions {
  display: inline-flex;
  gap: 2px;
  opacity: 0;
  transition: opacity var(--dur-fast, 120ms) var(--ease-standard);
}

.file-tree__row-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border: 0;
  background: transparent;
  color: var(--sidebar-text-soft);
  border-radius: var(--radius-xs);
  cursor: pointer;
}

.file-tree__row-action:hover {
  background: var(--sidebar-selection-bg-hover);
  color: var(--sidebar-text-strong);
}

.file-tree__state {
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.file-tree__state-text {
  margin: 0;
  font-size: 12px;
  color: var(--sidebar-text-soft);
  word-break: break-word;
}

.file-tree__retry {
  border: 1px solid var(--border-subtle, var(--sidebar-item-hover));
  background: transparent;
  color: var(--sidebar-text-strong);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.file-tree__retry:hover {
  background: var(--sidebar-item-hover);
}
</style>
