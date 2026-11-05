import { getCurrentScope, nextTick, onScopeDispose, ref, watch, type Ref } from 'vue'
import i18n from '@/i18n'
import type { SessionDirectory, SessionItem } from '@/modules/sessionDirectory'
import type { WorkspaceDirectoryEntry, WorkspaceFiles } from '@/modules/workspaceFiles'
import {
  mentionMatchRank,
  mentionQueryAt,
  mentionTarget,
  replaceMentionQuery,
  type MentionQuery,
} from '@/utils/chat/mentionQuery'

export type MentionCandidateKind = 'workspace_directory' | 'workspace_file' | 'session'

export interface MentionCandidate {
  id: string
  kind: MentionCandidateKind
  label: string
  detail: string
  insertion: string
  path?: string
  sessionKey?: string
}

export interface UseChatMentionsOptions {
  inputText: Ref<string>
  sessionKey: Ref<string>
  workspaceFiles?: WorkspaceFiles | null
  sessionDirectory?: SessionDirectory | null
  getCaret?: () => number
  setCaret?: (offset: number) => void
  autoResizeTextarea?: () => void
}

/** One listing covers a whole directory level: the palette never paginates. */
const DIRECTORY_PAGE_LIMIT = 200
const WORKSPACE_CANDIDATE_LIMIT = 8
const SESSION_PAGE_LIMIT = 20
const SESSION_CANDIDATE_LIMIT = 6
/** Directory candidates lead, recent sessions follow; the panel stays one screen. */
const MENTION_CANDIDATE_LIMIT = 14

function workspaceCandidate(entry: WorkspaceDirectoryEntry): MentionCandidate | null {
  const name = typeof entry?.name === 'string' ? entry.name : ''
  const path = typeof entry?.path === 'string' ? entry.path : ''
  if (!name || !path) return null
  if (entry.type === 'directory') {
    // The trailing slash keeps the token a directory prefix, so the palette can
    // list its children right after the insertion.
    return {
      id: `workspace:${path}`,
      kind: 'workspace_directory',
      label: `${name}/`,
      detail: path,
      insertion: `@${path}/`,
      path,
    }
  }
  return {
    id: `workspace:${path}`,
    kind: 'workspace_file',
    label: name,
    detail: path,
    insertion: `@${path}`,
    path,
  }
}

function sessionCandidate(session: SessionItem): MentionCandidate | null {
  const key = typeof session?.key === 'string' ? session.key : ''
  if (!key) return null
  const label = (typeof session.title === 'string' && session.title) || key
  // The `(session:<key>)` suffix is the reference the sender resolves; the
  // visible title alone is not unique across sessions.
  return {
    id: `session:${key}`,
    kind: 'session',
    label,
    detail: typeof session.groupLabel === 'string' ? session.groupLabel : '',
    insertion: `@${label} (session:${key})`,
    sessionKey: key,
  }
}

/** Rank by match quality first, then keep the listing stable by name. */
function rankMatches<T>(
  entries: readonly T[],
  name: (entry: T) => string,
  fragment: string,
  limit: number,
): T[] {
  return entries
    .map(entry => ({ entry, rank: mentionMatchRank(name(entry), fragment) }))
    .filter(item => item.rank >= 0)
    .sort((a, b) => a.rank - b.rank || name(a.entry).localeCompare(name(b.entry)))
    .slice(0, limit)
    .map(item => item.entry)
}

export function useChatMentions(options: UseChatMentionsOptions) {
  const mentionOpen = ref(false)
  const mentionIdx = ref(0)
  const mentionItems = ref<MentionCandidate[]>([])
  const mentionLoading = ref(false)
  const mentionError = ref('')
  /** The live `@` token; every insertion replaces exactly this range. */
  let token: MentionQuery | null = null
  /** Fences stale refreshes: only the newest request may touch panel state. */
  let epoch = 0
  /**
   * Listings are cached per directory for the lifetime of one workspace scope.
   * The binding is echoed back by the first successful page and reused so a
   * mid-palette rebind cannot splice two workspaces into one menu.
   */
  const directoryLoads = new Map<string, Promise<WorkspaceDirectoryEntry[]>>()
  let workspaceBinding: string | undefined
  let sessionLoad: Promise<SessionItem[]> | null = null

  function closeMention() {
    epoch += 1
    token = null
    mentionOpen.value = false
    mentionLoading.value = false
    mentionItems.value = []
    mentionError.value = ''
  }

  function resetCaches() {
    directoryLoads.clear()
    workspaceBinding = undefined
    sessionLoad = null
  }

  // Another session owns another workspace root and another recents list.
  watch(options.sessionKey, () => {
    resetCaches()
    closeMention()
  }, { flush: 'sync' })

  if (getCurrentScope()) onScopeDispose(() => {
    epoch += 1
    resetCaches()
  })

  function listDirectory(directory: string): Promise<WorkspaceDirectoryEntry[]> {
    const cached = directoryLoads.get(directory)
    if (cached) return cached
    const files = options.workspaceFiles
    const listDir = files?.listDir
    if (!files || typeof listDir !== 'function') return Promise.resolve([])
    const request = Promise.resolve(listDir.call(
      files,
      options.sessionKey.value,
      directory,
      '0',
      DIRECTORY_PAGE_LIMIT,
      workspaceBinding,
    )).then((page) => {
      if (typeof page?.workspaceBinding === 'string' && page.workspaceBinding) {
        workspaceBinding = page.workspaceBinding
      }
      return Array.isArray(page?.entries) ? page.entries : []
    })
    directoryLoads.set(directory, request)
    // A refused listing must not poison the cache; the next keystroke retries.
    void request.catch(() => {
      if (directoryLoads.get(directory) === request) directoryLoads.delete(directory)
    })
    return request
  }

  function listSessions(): Promise<SessionItem[]> {
    if (sessionLoad) return sessionLoad
    const directory = options.sessionDirectory
    if (!directory) return Promise.resolve([])
    const request = Promise.resolve(directory.listPage({ limit: SESSION_PAGE_LIMIT }))
      .then(page => (Array.isArray(page?.items) ? page.items : []))
    sessionLoad = request
    void request.catch(() => { if (sessionLoad === request) sessionLoad = null })
    return request
  }

  async function collectWorkspace(
    directory: string,
    fragment: string,
  ): Promise<{ items: MentionCandidate[]; failed: boolean }> {
    // No workspace (older Gateway, no injection) simply means no file candidates.
    if (typeof options.workspaceFiles?.listDir !== 'function') return { items: [], failed: false }
    try {
      const entries = await listDirectory(directory)
      const ranked = rankMatches(entries, entry => entry.name, fragment, WORKSPACE_CANDIDATE_LIMIT)
      return {
        items: ranked.map(workspaceCandidate).filter(
          (candidate): candidate is MentionCandidate => candidate !== null,
        ),
        failed: false,
      }
    } catch {
      return { items: [], failed: true }
    }
  }

  async function collectSessions(fragment: string): Promise<MentionCandidate[]> {
    if (!options.sessionDirectory) return []
    try {
      const sessions = await listSessions()
      // Sessions are not paths: match the fragment left after any directory part.
      const ranked = rankMatches(
        sessions,
        session => session.title || session.key,
        fragment,
        SESSION_CANDIDATE_LIMIT,
      )
      return ranked.map(sessionCandidate).filter(
        (candidate): candidate is MentionCandidate => candidate !== null,
      )
    } catch {
      return []
    }
  }

  async function refreshCandidates(query: string) {
    const operation = ++epoch
    const { directory, fragment } = mentionTarget(query)
    mentionLoading.value = true
    mentionError.value = ''
    const [workspace, sessions] = await Promise.all([
      collectWorkspace(directory, fragment),
      collectSessions(fragment),
    ])
    // The caret moved, the palette closed, or the session switched: the newest
    // operation owns the panel, so drop this response without touching state.
    if (operation !== epoch) return
    mentionLoading.value = false
    mentionItems.value = [...workspace.items, ...sessions].slice(0, MENTION_CANDIDATE_LIMIT)
    mentionIdx.value = Math.max(0, Math.min(mentionIdx.value, mentionItems.value.length - 1))
    if (workspace.failed) mentionError.value = i18n.global.t('chat.mention.unavailable')
  }

  function handleMentionInput() {
    const text = options.inputText.value
    const caret = options.getCaret?.() ?? text.length
    const next = mentionQueryAt(text, caret)
    if (!next) {
      closeMention()
      return
    }
    token = next
    mentionOpen.value = true
    mentionIdx.value = 0
    mentionLoading.value = true
    void refreshCandidates(next.query)
  }

  function chooseMention(candidate: MentionCandidate) {
    const range = token
    if (!range) return
    const { text, caret } = replaceMentionQuery(options.inputText.value, range, `${candidate.insertion} `)
    options.inputText.value = text
    options.autoResizeTextarea?.()
    // A directory keeps the palette alive to drill into its children, so the
    // caret must stay inside the token: mentionQueryAt ends a token at
    // whitespace, and a caret after the separator space would close the panel.
    options.setCaret?.(candidate.kind === 'workspace_directory' ? Math.max(range.start, caret - 1) : caret)
    token = null
    if (candidate.kind === 'workspace_directory') {
      mentionOpen.value = true
      mentionItems.value = []
      void nextTick(() => handleMentionInput())
      return
    }
    closeMention()
  }

  return {
    mentionOpen,
    mentionIdx,
    mentionItems,
    mentionLoading,
    mentionError,
    handleMentionInput,
    chooseMention,
    closeMention,
  }
}
