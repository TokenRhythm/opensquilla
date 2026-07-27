<template>
  <Transition name="cmdp">
  <div
    v-if="open"
    class="cmdp-backdrop"
    role="presentation"
    @mousedown="onBackdrop"
  >
    <section
      ref="dialogRef"
      class="cmdp-dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="t('shared.cmdp.dialogLabel')"
    >
      <div class="cmdp-search">
        <!-- The magnifier sits inside the field so the input reads as the search
             box itself; dismissal stays outside it. -->
        <div class="cmdp-search__field">
          <Icon name="search" :size="16" class="cmdp-search__icon" />
          <input
            ref="inputRef"
            v-model="query"
            type="text"
            class="cmdp-search__input"
            :placeholder="t('shared.cmdp.placeholder')"
            role="combobox"
            aria-expanded="true"
            aria-controls="cmdp-listbox"
            aria-autocomplete="list"
            :aria-activedescendant="activeId"
            autocomplete="off"
            spellcheck="false"
            @keydown="onInputKeydown"
          />
        </div>
        <button
          type="button"
          class="cmdp-search__close"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          @click="close()"
        >
          <Icon name="x" :size="15" />
        </button>
      </div>

      <div
        id="cmdp-listbox"
        ref="listRef"
        class="cmdp-list"
        role="listbox"
        :aria-label="t('shared.cmdp.resultsLabel')"
      >
        <p v-if="flatItems.length === 0 && !searching" class="cmdp-empty">
          {{ query.trim() ? t('shared.cmdp.noMatches') : t('shared.cmdp.recentsEmpty') }}
        </p>
        <template v-for="group in groups" :key="group.label">
          <template v-if="group.items.length > 0">
            <p class="cmdp-group-label" aria-hidden="true">{{ groupLabel(group.label) }}</p>
            <button
              v-for="item in group.items"
              :id="`cmdp-opt-${item.index}`"
              :key="item.id"
              type="button"
              class="cmdp-option"
              :class="{ 'is-active': item.index === activeIndex }"
              role="option"
              :aria-selected="item.index === activeIndex"
              @click="runItem(item)"
              @mousemove="activeIndex = item.index"
            >
              <Icon v-if="item.icon" :name="item.icon" :size="16" class="cmdp-option__icon" />
              <span class="cmdp-option__body">
                <span class="cmdp-option__label">{{ item.title }}</span>
                <span v-if="item.subtitle" class="cmdp-option__sub">{{ item.subtitle }}</span>
                <!-- eslint-disable-next-line vue/no-v-html — snippet is HTML-escaped in renderSnippet; only <mark> is injected -->
                <span v-if="item.snippetHtml" class="cmdp-option__snippet" v-html="item.snippetHtml"></span>
              </span>
            </button>
          </template>
        </template>
        <p v-if="searching" class="cmdp-searching" aria-live="polite">{{ t('shared.cmdp.searching') }}</p>
      </div>
    </section>
  </div>
  </Transition>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from './Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useBgm } from '@/composables/useBgm'
import { getWorkNavigationSection } from '@/router/nav'
import { useRpcStore } from '@/stores/rpc'
import { highlightFtsSnippet } from '@/utils/searchSnippet'
import type { SidebarSection } from '@/composables/useSessions'
import type { IconName } from '@/utils/icons'
import type { MessageSearchHit, SessionSearchHit, SessionsSearchResponse } from '@/types/rpc'

const props = defineProps<{
  open: boolean
  /**
   * Already-loaded sidebar sections. The empty-query state lists these as
   * "Recent tasks" so opening the palette answers "which task?" immediately,
   * with no extra round trip. Destinations and actions stay reachable, but only
   * once the query matches one — an untyped palette full of nav rows is not what
   * a button labelled "search tasks" promises.
   */
  recents: SidebarSection[]
}>()
const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'new-chat'): void
  (e: 'open-settings'): void
  (e: 'toggle-theme'): void
  (e: 'select-session', key: string): void
}>()

const { t } = useI18n()
const router = useRouter()
const rpcStore = useRpcStore()
const { enabled: bgmEnabled, setEnabled: setBgmEnabled } = useBgm()

const dialogRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const query = ref('')
const activeIndex = ref(0)

const open = computed(() => props.open)

// Trap focus, Escape-to-close, and restore focus to the invoker on close. The
// input is the first focusable, so it receives focus on open automatically.
useDialogA11y(dialogRef, open, () => close())

// ---------------------------------------------------------------------------
// Command source — data-driven from the existing nav helpers plus a small set
// of actions. Each entry carries optional keywords for the substring filter.
// ---------------------------------------------------------------------------
type Run = () => void
interface Command {
  id: string
  title: string
  /** Omitted for task rows: a column of identical chat glyphs is not a signal. */
  icon?: IconName
  keywords: string
  group: string
  /** Secondary line (e.g. a conversation's source surface). */
  subtitle?: string
  /** Pre-sanitized highlight HTML for a transcript snippet (escaped upstream). */
  snippetHtml?: string
  run: Run
}

// Stable group order for the rendered palette. Conversation groups sort below
// the static nav/action groups so "go to page" stays the top, instant result.
const GROUP_ORDER = ['Recents', 'Work', 'Observe', 'Actions', 'Conversations', 'Messages'] as const

// Display label for a group id. Nav bands resolve through the same nav.* keys
// the sidebar uses, so the palette and the rail always agree per locale.
const GROUP_LABEL_KEYS: Record<string, string> = {
  Recents: 'shared.cmdp.groupRecents',
  Work: 'shared.cmdp.groupWork',
  Observe: 'nav.groupMonitor',
  Actions: 'shared.cmdp.groupActions',
  Conversations: 'shared.cmdp.groupConversations',
  Messages: 'shared.cmdp.groupMessages',
}
function groupLabel(label: string): string {
  const key = GROUP_LABEL_KEYS[label]
  return key ? t(key) : label
}

function navTo(path: string): Run {
  return () => {
    void router.push(path)
  }
}

// Build the full command set once per open (nav helpers are platform-filtered
// and cheap; recomputing on open keeps it correct after platform changes).
const allCommands = computed<Command[]>(() => {
  const out: Command[] = []

  // Work: the pinned rail destinations, from the single Work-band helper so the
  // palette tracks the route taxonomy instead of a hardcoded path list (which
  // silently dropped promoted routes and double-listed demoted ones).
  for (const item of getWorkNavigationSection()) {
    const searchAliases = item.path === '/usage' ? ' usage 用量' : ''
    out.push({
      id: `nav:${item.path}`,
      title: item.title,
      icon: item.icon,
      keywords: `${item.title} ${item.path}${searchAliases}`.toLowerCase(),
      group: 'Work',
      run: navTo(item.path),
    })
    // Channels is the second route in the Skills & Channels hub. Keep its
    // direct command beside the pinned hub entry instead of burying it among
    // Overview's operational subpages.
    if (item.path === '/skills') {
      const title = t('nav.channels')
      out.push({
        id: 'nav:/channels',
        title,
        icon: 'channels',
        keywords: `${title} /channels`.toLowerCase(),
        group: 'Work',
        run: navTo('/channels'),
      })
    }
  }

  // Diagnostic Logs remains directly reachable without promoting it back into
  // the rail; the Overview command already opens Status.
  const demoted: Array<{ path: string; name: string; icon: IconName; group: string }> = [
    { path: '/logs', name: 'logs', icon: 'logs', group: 'Observe' },
  ]
  for (const item of demoted) {
    const title = t(`nav.${item.name}`)
    out.push({
      id: `nav:${item.path}`,
      title,
      icon: item.icon,
      keywords: `${title} ${item.path}`.toLowerCase(),
      group: item.group,
      run: navTo(item.path),
    })
  }

  // The Channels hub entry opens the workspace; this separate action lands in
  // its add-channel compose takeover.
  const channelSetupTitle = t('nav.channelSetup')
  out.push({
    id: 'nav:/channels?compose=1',
    title: channelSetupTitle,
    icon: 'channels',
    keywords: `${channelSetupTitle} channels setup config add /channels`.toLowerCase(),
    group: 'Actions',
    run: navTo('/channels?compose=1'),
  })

  // Actions: app-level commands that are not routes.
  out.push(
    {
      id: 'action:new-chat',
      title: t('shared.cmdp.actionNewChat'),
      icon: 'plus',
      keywords: 'new chat conversation compose start',
      group: 'Actions',
      run: () => emit('new-chat'),
    },
    {
      id: 'action:settings',
      title: t('shared.cmdp.actionOpenSettings'),
      icon: 'settings',
      keywords: 'settings preferences configure options',
      group: 'Actions',
      run: () => emit('open-settings'),
    },
    {
      id: 'action:toggle-theme',
      title: t('shared.cmdp.actionToggleTheme'),
      icon: 'monitor',
      keywords: 'theme dark light appearance toggle',
      group: 'Actions',
      run: () => emit('toggle-theme'),
    },
    // Background-music gate: writes the useBgm singleton directly (no App-level
    // routing or handler to reuse, unlike the emit-based actions above). The
    // title tracks the current state via the reactive `enabled` ref.
    {
      id: 'action:toggle-bgm',
      title: bgmEnabled.value ? t('shared.cmdp.actionBgmDisable') : t('shared.cmdp.actionBgmEnable'),
      icon: 'music',
      keywords: 'music bgm background sound audio 音乐 背景音乐',
      group: 'Actions',
      run: () => setBgmEnabled(!bgmEnabled.value),
    },
  )

  return out
})

// Case-insensitive substring filter on title + keywords. Only consulted for a
// non-empty query: the untyped palette lists recent tasks instead (see
// visibleCommands), so destinations surface by name rather than by default.
const filtered = computed<Command[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return []
  return allCommands.value.filter(
    (cmd) => cmd.title.toLowerCase().includes(q) || cmd.keywords.includes(q),
  )
})

// ---------------------------------------------------------------------------
// Conversation search — async, debounced, server-side over titles + transcript
// content (sessions.search). Results append below the static nav/action groups.
// ---------------------------------------------------------------------------
const sessionHits = ref<SessionSearchHit[]>([])
const messageHits = ref<MessageSearchHit[]>([])
const searching = ref(false)
let debounceTimer: ReturnType<typeof setTimeout> | null = null
// Monotonic token so a slow response from an earlier keystroke can't overwrite
// the results of a later one.
let searchToken = 0

function clearResults() {
  sessionHits.value = []
  messageHits.value = []
  searching.value = false
}

async function runSearch(q: string) {
  const token = ++searchToken
  searching.value = true
  try {
    const res = await rpcStore.call<SessionsSearchResponse>('sessions.search', { query: q, limit: 12 })
    if (token !== searchToken) return
    sessionHits.value = res?.sessions ?? []
    messageHits.value = res?.messages ?? []
  } catch {
    if (token !== searchToken) return
    sessionHits.value = []
    messageHits.value = []
  } finally {
    if (token === searchToken) searching.value = false
  }
}

// Conversation search is async + debounced and never blocks typing: the nav /
// action filter above is synchronous, so "go to page" stays instant regardless
// of search latency. A 2-char floor avoids firing on a lone ASCII letter (which
// matches almost everything), but a single CJK/non-ASCII character is a whole
// word, so allow length-1 when the query is non-ASCII.
const MIN_SEARCH_LEN = 2
const NON_ASCII = /[^\x00-\x7F]/
function shouldSearch(q: string): boolean {
  return q.length >= MIN_SEARCH_LEN || NON_ASCII.test(q)
}
watch(query, (q) => {
  const trimmed = q.trim()
  if (debounceTimer) clearTimeout(debounceTimer)
  if (!shouldSearch(trimmed)) {
    searchToken++ // cancel any in-flight result
    clearResults()
    return
  }
  debounceTimer = setTimeout(() => runSearch(trimmed), 180)
})

const conversationCommands = computed<Command[]>(() => {
  const out: Command[] = []
  for (const hit of sessionHits.value) {
    out.push({
      id: `session:${hit.key}`,
      title: hit.title || t('shared.cmdp.untitledChat'),
      keywords: '',
      group: 'Conversations',
      subtitle: hit.surface && hit.surface !== 'webchat' ? hit.surface : undefined,
      run: () => emit('select-session', hit.key),
    })
  }
  messageHits.value.forEach((hit, i) => {
    out.push({
      id: `message:${hit.key}:${hit.createdAt ?? i}:${i}`,
      title: hit.title || t('shared.cmdp.untitledChat'),
      keywords: '',
      group: 'Messages',
      snippetHtml: highlightFtsSnippet(hit.snippet || ''),
      run: () => emit('select-session', hit.key),
    })
  })
  return out
})

// Recent tasks, flattened from the sidebar sections the app already loaded.
// Capped so the untyped palette stays a short "pick up where you left off" list
// rather than a scroll of the whole ledger; subagent rows (depth > 0) are folded
// under their parent in the sidebar and would read as duplicates here.
const RECENTS_LIMIT = 8
const recentCommands = computed<Command[]>(() => {
  const out: Command[] = []
  const seen = new Set<string>()
  for (const section of props.recents) {
    for (const row of section.rows) {
      if (out.length >= RECENTS_LIMIT) return out
      if (row.rowKind !== 'session' || row.depth > 0 || seen.has(row.key)) continue
      seen.add(row.key)
      out.push({
        id: `recent:${row.key}`,
        title: row.title || t('shared.cmdp.untitledChat'),
        keywords: '',
        group: 'Recents',
        run: () => emit('select-session', row.key),
      })
    }
  }
  return out
})

// Untyped: recent tasks only. Typed: nav/action matches (instant, synchronous)
// followed by conversation matches (async), so nothing is orphaned but the
// resting state stays about tasks.
const visibleCommands = computed<Command[]>(() => (
  query.value.trim()
    ? [...filtered.value, ...conversationCommands.value]
    : recentCommands.value
))

interface FlatItem extends Command {
  index: number
}

// Flatten in group order, assigning each visible item a stable roving index so
// ↑/↓ and aria-activedescendant address a single sequence across groups.
const flatItems = computed<FlatItem[]>(() => {
  const items: FlatItem[] = []
  let index = 0
  for (const label of GROUP_ORDER) {
    for (const cmd of visibleCommands.value) {
      if (cmd.group !== label) continue
      items.push({ ...cmd, index: index++ })
    }
  }
  return items
})

const groups = computed(() =>
  GROUP_ORDER.map((label) => ({
    label,
    items: flatItems.value.filter((item) => item.group === label),
  })).filter((group) => group.items.length > 0),
)

const activeId = computed(() => {
  const item = flatItems.value[activeIndex.value]
  return item ? `cmdp-opt-${item.index}` : undefined
})

// Reset query + selection whenever the palette opens; clear any conversation
// results and cancel in-flight searches on close so the next open starts clean.
watch(open, (isOpen) => {
  if (isOpen) {
    query.value = ''
    activeIndex.value = 0
    clearResults()
  } else {
    if (debounceTimer) clearTimeout(debounceTimer)
    searchToken++
    clearResults()
  }
})

// Clamp the active index whenever the visible set shrinks under the cursor
// (filter narrowing or async results arriving/clearing).
watch(flatItems, () => {
  if (activeIndex.value >= flatItems.value.length) {
    activeIndex.value = Math.max(0, flatItems.value.length - 1)
  }
})

onBeforeUnmount(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
})

function scrollActiveIntoView() {
  void nextTick(() => {
    const el = listRef.value?.querySelector<HTMLElement>('.cmdp-option.is-active')
    el?.scrollIntoView({ block: 'nearest' })
  })
}

function move(delta: number) {
  const count = flatItems.value.length
  if (count === 0) return
  activeIndex.value = (activeIndex.value + delta + count) % count
  scrollActiveIntoView()
}

function onInputKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const item = flatItems.value[activeIndex.value]
    if (item) runItem(item)
  }
  // Escape is handled by useDialogA11y (document-level), so it is intentionally
  // not intercepted here.
}

function runItem(item: Command) {
  close()
  item.run()
}

function close() {
  emit('update:open', false)
}

function onBackdrop(e: MouseEvent) {
  if (e.target === e.currentTarget) close()
}
</script>

<style scoped>
.cmdp-backdrop {
  position: fixed;
  inset: 0;
  /* Modal tier (above the fixed sidebar at 200) so the scrim dims the rail and
     the palette reads as a true modal — matches the 300 used by other modals. */
  z-index: 300;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 12vh 16px 16px;
  background: var(--scrim);
}

.cmdp-dialog {
  /* Give task titles and message snippets enough horizontal room without
     turning the palette into a full-page surface. */
  width: min(560px, calc(100vw - 32px));
  max-height: min(600px, calc(100vh - 18vh));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
}

/* Open/close motion: scrim fades, dialog slides up + fades (exit a tier faster).
   Matches the modal idiom (SettingsDialog settings-pop). */
.cmdp-enter-active { transition: opacity var(--dur-base) var(--ease-out); }
.cmdp-leave-active { transition: opacity var(--dur-fast) var(--ease-in); }
.cmdp-enter-from,
.cmdp-leave-to { opacity: 0; }
.cmdp-enter-active .cmdp-dialog {
  transition: transform var(--dur-base) var(--ease-out), opacity var(--dur-base) var(--ease-out);
}
.cmdp-leave-active .cmdp-dialog {
  transition: transform var(--dur-fast) var(--ease-in), opacity var(--dur-fast) var(--ease-in);
}
.cmdp-enter-from .cmdp-dialog { opacity: 0; transform: translateY(8px); }
.cmdp-leave-to .cmdp-dialog { opacity: 0; transform: translateY(6px); }

.cmdp-search {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 12px 14px;
}

/* Barely-there fill so the field reads as a field without becoming a slab: a
   hairline border does the work, the tint only hints at depth. The magnifier
   lives inside it rather than floating in the dialog's padding. */
.cmdp-search__field {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 1;
  min-width: 0;
  min-height: 44px;
  padding: 0 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  transition: border-color var(--dur-fast), background var(--dur-fast);
}
.cmdp-search__field:focus-within {
  border-color: color-mix(in srgb, var(--accent) 70%, var(--border));
}

.cmdp-search__field:focus-within .cmdp-search__icon {
  color: var(--accent);
}

.cmdp-search__icon {
  color: var(--text-muted);
  flex-shrink: 0;
}

.cmdp-search__field > input.cmdp-search__input,
.cmdp-search__field > input.cmdp-search__input:focus {
  appearance: none;
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  color: var(--text);
  flex: 1;
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  font-weight: 400;
  line-height: 20px;
  min-height: 42px;
  min-width: 0;
  outline: none;
  padding: 0;
}

.cmdp-search__input::placeholder {
  color: var(--text-muted);
}

/* Explicit dismiss outside the field: the palette is a task picker people open
   and abandon, and Escape alone is not a visible way out. */
.cmdp-search__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  padding: 0;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--dur-fast), color var(--dur-fast);
}
.cmdp-search__close:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.cmdp-search__close:focus-visible {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
  outline: 1px solid var(--accent);
  outline-offset: -1px;
}

.cmdp-list {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 8px;
}

.cmdp-empty {
  margin: 0;
  padding: 18px 10px;
  text-align: center;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.cmdp-group-label {
  margin: var(--sp-2) 0 2px;
  padding: 0 var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 400;
  line-height: 18px;
  color: var(--text-muted);
}
.cmdp-group-label:first-child {
  margin-top: 0;
}

.cmdp-option {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  /* Roomier rows now that each one is a single line of title: the list is a
     "pick one" surface, so touchable spacing beats packing more in. */
  min-height: 42px;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  line-height: 20px;
  /* Regular weight: the active row is marked by its tint, not by getting
     bolder, so a list of task titles stays quiet to read down. */
  font-weight: 400;
  text-align: left;
  cursor: pointer;
}

.cmdp-option__icon {
  flex-shrink: 0;
  color: var(--text-muted);
}

.cmdp-option__body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.cmdp-option__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Conversation result secondary lines: muted, lighter, single-line ellipsis. */
.cmdp-option__sub,
.cmdp-option__snippet {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 400;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cmdp-option__snippet :deep(.cmdp-mark) {
  background: color-mix(in srgb, var(--accent) 22%, transparent);
  border-radius: var(--radius-xs);
  color: var(--text);
  padding: 0 1px;
}

.cmdp-searching {
  margin: 0;
  padding: 10px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}


/* Lark marks the highlighted row with a plain neutral fill — no leading bar, no
   tinted border, no recoloured text. The row is being pointed at, not flagged;
   brand colour stays on the action you press, not on where the cursor is. */
.cmdp-option.is-active {
  background: var(--bg-hover);
  border-color: transparent;
}
.cmdp-option.is-active .cmdp-option__icon {
  color: var(--text-muted);
}

@media (max-width: 768px) {
  .cmdp-backdrop {
    padding: 8vh 12px 12px;
  }
}
</style>
