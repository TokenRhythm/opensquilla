# Sidebar Pinned, Projects, and Recents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a unique global Pinned collection above peer Projects and Recents sections, with independent counts and a viewport-safe session hover card that shows optional project identity.

**Architecture:** Keep backend session/project ownership and browser-local pin persistence unchanged. Add a pure display projection that extracts pinned rows, preserves project membership for hover metadata, and returns project/Recents rows for the existing sidebar component; pass the persisted manual session order into the component so cross-project pinned ordering remains deterministic. Render the hover card as a focused Teleported component.

**Tech Stack:** Vue 3 Composition API, TypeScript, Vitest + happy-dom, vue-i18n, existing OpenSquilla CSS tokens.

## Global Constraints

- Work only on `codex/fix-sidebar-section-hierarchy`.
- A session appears in exactly one of Pinned, a project, or Recents.
- Pinning never mutates backend ownership or canonical `workspaceId`.
- Pinned is hidden at count zero; Projects and Recents keep their empty states.
- Never display a raw workspace ID or inferred filesystem path as a project name.
- Reuse existing localization and design tokens; add no dependencies.

---

### Task 1: Pure sidebar display projection

**Files:**
- Create: `opensquilla-webui/src/utils/sidebarDisplayProjection.ts`
- Create: `opensquilla-webui/src/utils/sidebarDisplayProjection.test.ts`

**Interfaces:**
- Consumes: `SidebarSection`, `SidebarSectionFamily`, `SidebarSectionRow` from `@/composables/useSessions` plus `sessionOrder: readonly string[]`.
- Produces: `buildSidebarDisplayProjection(sections, sessionOrder): SidebarDisplayProjection`, where rows carry `displayZone`, `displayFamily`, and a resolved `displayProjectName`.

- [ ] **Step 1: Write failing projection tests**

```ts
import type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

function session(key: string, overrides: Partial<SidebarSectionRow> = {}): SidebarSectionRow {
  return {
    rowKind: 'session', key, title: key, effectiveAgentId: 'main', agentName: 'Main',
    sessionKind: 'chat', depth: 0, runStatus: 'idle', runLabel: 'Idle',
    taskAttention: 'none', updatedAt: 100, hasContractGaps: false, ...overrides,
  }
}

function project(id: string, title: string): SidebarSectionRow {
  return {
    ...session(`workspace:${id}`, { title, workspaceId: id, workspaceTaskCount: 1 }),
    rowKind: 'workspace', sessionKind: 'workspace',
  }
}

function section(family: SidebarSectionFamily, rows: SidebarSectionRow[]): SidebarSection {
  return { family, label: family, rows }
}

it('extracts pinned rows once and resolves their project name from the header', () => {
  const result = buildSidebarDisplayProjection([
    section('chats', [project('p1', 'OpenSquilla'), session('project-pin', { workspaceId: 'p1', pinned: true }), session('project-live', { workspaceId: 'p1' }), session('recent-pin', { pinned: true }), session('recent-live')]),
  ], ['recent-pin', 'project-pin'])

  expect(result.pinned.map(row => [row.key, row.displayProjectName])).toEqual([
    ['recent-pin', ''],
    ['project-pin', 'OpenSquilla'],
  ])
  expect(result.projects.map(row => row.key)).toEqual(['workspace:p1', 'project-live'])
  expect(result.recents[0].rows.map(row => row.key)).toEqual(['recent-live'])
  expect(result.allRows.filter(row => row.key === 'project-pin')).toHaveLength(1)
})

it('returns an unpinned row to its canonical project or Recents zone', () => {
  const projectResult = buildSidebarDisplayProjection([
    section('chats', [project('p1', 'OpenSquilla'), session('project-task', { workspaceId: 'p1', pinned: false }), session('recent-task', { pinned: false })]),
  ], [])

  expect(projectResult.projects.find(row => row.key === 'project-task')?.displayZone).toBe('projects')
  expect(projectResult.recents[0].rows.find(row => row.key === 'recent-task')?.displayZone).toBe('recents')
})
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
$env:Path = 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:Path
& 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd' exec vitest run src/utils/sidebarDisplayProjection.test.ts
```

Expected: FAIL because `sidebarDisplayProjection` does not exist.

- [ ] **Step 3: Implement the projection**

```ts
import type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

export type SidebarDisplayZone = 'pinned' | 'projects' | 'recents'

export interface SidebarDisplayRow extends SidebarSectionRow {
  displayZone: SidebarDisplayZone
  displayFamily: SidebarSectionFamily
  displayProjectName: string
}

export interface SidebarDisplayRecentSection {
  family: SidebarSectionFamily
  label: string
  rows: SidebarDisplayRow[]
}

export interface SidebarDisplayProjection {
  pinned: SidebarDisplayRow[]
  projects: SidebarDisplayRow[]
  recents: SidebarDisplayRecentSection[]
  projectCount: number
  recentCount: number
  allRows: SidebarDisplayRow[]
}

export function buildSidebarDisplayProjection(
  sections: readonly SidebarSection[],
  sessionOrder: readonly string[] = [],
): SidebarDisplayProjection {
  const pinned: Array<SidebarDisplayRow & { sourceIndex: number }> = []
  const projects: SidebarDisplayRow[] = []
  const recents: SidebarDisplayRecentSection[] = []
  let sourceIndex = 0

  for (const section of sections) {
    const recentRows: SidebarDisplayRow[] = []
    let activeProject: SidebarSectionRow | null = null
    for (const row of section.rows) {
      if (row.rowKind === 'workspace') {
        activeProject = section.family === 'chats' ? row : null
        projects.push({ ...row, displayZone: 'projects', displayFamily: section.family, displayProjectName: row.title })
        continue
      }
      if (row.rowKind === 'workspace-empty') continue

      const belongsToProject = Boolean(
        section.family === 'chats'
        && activeProject
        && (activeProject.workspaceId
          ? row.workspaceId === activeProject.workspaceId
          : Boolean(activeProject.workspace && row.workspace === activeProject.workspace)),
      )
      if (!belongsToProject) activeProject = null
      const displayProjectName = belongsToProject && activeProject ? activeProject.title : ''
      const displayZone: SidebarDisplayZone = row.pinned ? 'pinned' : belongsToProject ? 'projects' : 'recents'
      const displayRow = { ...row, displayZone, displayFamily: section.family, displayProjectName }
      if (displayZone === 'pinned') pinned.push({ ...displayRow, sourceIndex: sourceIndex++ })
      else if (displayZone === 'projects') projects.push(displayRow)
      else recentRows.push(displayRow)
    }
    if (recentRows.length > 0) recents.push({ family: section.family, label: section.label, rows: recentRows })
  }

  const orderIndex = new Map(sessionOrder.map((key, index) => [key, index]))
  pinned.sort((a, b) => {
    const ai = orderIndex.get(a.key)
    const bi = orderIndex.get(b.key)
    if (ai !== undefined && bi !== undefined) return ai - bi
    if (ai !== undefined) return -1
    if (bi !== undefined) return 1
    return a.sourceIndex - b.sourceIndex
  })

  const pinnedRows = pinned.map(({ sourceIndex: _sourceIndex, ...row }) => row)
  const recentCount = recents.reduce((sum, section) => sum + section.rows.length, 0)
  const projectCount = projects.filter(row => row.rowKind === 'workspace').length
  return { pinned: pinnedRows, projects, recents, projectCount, recentCount, allRows: [...pinnedRows, ...projects, ...recents.flatMap(section => section.rows)] }
}
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: the new test file passes.

- [ ] **Step 5: Commit**

```powershell
git add opensquilla-webui/src/utils/sidebarDisplayProjection.ts opensquilla-webui/src/utils/sidebarDisplayProjection.test.ts
git commit -m "test(webui): define sidebar display zones"
```

### Task 2: Peer zones, counts, and global pinned ordering

**Files:**
- Modify: `opensquilla-webui/src/App.vue:81-105,937-1026`
- Modify: `opensquilla-webui/src/components/SidebarConversations.vue:1-1160`
- Modify: `opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts:1-450`
- Modify: `opensquilla-webui/src/locales/en.json:4048-4070`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json:4048-4070`
- Modify: `opensquilla-webui/src/locales/de.json:3995-4020`
- Modify: `opensquilla-webui/src/locales/es.json:3995-4020`
- Modify: `opensquilla-webui/src/locales/fr.json:3995-4020`
- Modify: `opensquilla-webui/src/locales/ja.json:3995-4020`

**Interfaces:**
- Consumes: `SidebarDisplayProjection` from Task 1 and `sessionOrder?: string[]` from `App.vue`.
- Produces: DOM zones identified by `data-sidebar-zone="pinned|projects|recents"`, `.sidebar-zone-heading`, and emitted reorder payloads that preserve the existing App contract.

- [ ] **Step 1: Add failing component tests**

Extend the existing mount helper with a fourth argument and pass it to the
component (all existing arguments and event handlers remain unchanged):

```ts
async function mountSidebar(
  rows: SidebarSectionRow[],
  canManageProjects = true,
  canCreateProjects = canManageProjects,
  sessionOrder: string[] = [],
) {
  const sections: SidebarSection[] = [{ family: 'chats', label: 'Tasks', rows }]
  const events = {
    select: vi.fn(),
    newProject: vi.fn(),
    newProjectTask: vi.fn(),
    projectPin: vi.fn(),
    projectEdit: vi.fn(),
    projectDeleteHistory: vi.fn(),
    projectRemove: vi.fn(),
    reorder: vi.fn(),
    sessionPin: vi.fn(),
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(SidebarConversations, {
    sections,
    sessionOrder,
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: 'Ctrl+K',
    canManageProjects,
    canCreateProjects,
    onSelect: events.select,
    onNewProject: events.newProject,
    onNewProjectTask: events.newProjectTask,
    onProjectPin: events.projectPin,
    onProjectEdit: events.projectEdit,
    onProjectDeleteHistory: events.projectDeleteHistory,
    onProjectRemove: events.projectRemove,
    onReorder: events.reorder,
    onSessionPin: events.sessionPin,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  return { host, events }
}
```

```ts
it('renders peer Pinned, Projects, and Recents headings with independent counts', async () => {
  const { host } = await mountSidebar([
    projectRow(),
    taskRow({ key: 'project-pin', pinned: true }),
    taskRow({ key: 'project-live' }),
    taskRow({ key: 'recent-pin', workspaceId: undefined, depth: 0, pinned: true }),
    taskRow({ key: 'recent-live', workspaceId: undefined, depth: 0 }),
  ], true, true, ['recent-pin', 'project-pin'])

  expect(Array.from(host.querySelectorAll('.sidebar-zone-heading')).map(node => node.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
    'Pinned 2', 'Projects 1', 'Recents 1',
  ])
  expect(host.querySelectorAll('[data-session-key="project-pin"]')).toHaveLength(1)
  expect(host.querySelector('[data-session-key="project-pin"]')?.getAttribute('data-sidebar-zone')).toBe('pinned')
})

it('allows pinned chats from different projects to reorder in one scope', async () => {
  const { host, events } = await mountSidebar([
    projectRow({ workspaceId: 'a', key: 'workspace:a' }),
    taskRow({ key: 'a-pin', workspaceId: 'a', pinned: true }),
    projectRow({ workspaceId: 'b', key: 'workspace:b' }),
    taskRow({ key: 'b-pin', workspaceId: 'b', pinned: true }),
  ], true, true, ['a-pin', 'b-pin'])
  const source = host.querySelector<HTMLElement>('[data-session-key="a-pin"]')
  const target = host.querySelector<HTMLElement>('[data-session-key="b-pin"]')
  vi.spyOn(document, 'elementFromPoint').mockReturnValue(target || null)
  source?.dispatchEvent(new MouseEvent('pointerdown', {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX: 10,
    clientY: 10,
  }))
  document.dispatchEvent(new MouseEvent('pointermove', {
    bubbles: true,
    cancelable: true,
    clientX: 20,
    clientY: 20,
  }))
  document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
  await nextTick()
  expect(events.reorder).toHaveBeenCalledWith({ draggedKey: 'a-pin', targetKey: 'b-pin', position: 'after' })
})
```

- [ ] **Step 2: Run component tests and verify RED**

Run:

```powershell
$env:Path = 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:Path
& 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd' exec vitest run src/components/SidebarConversations.workspaces.test.ts
```

Expected: FAIL because no Pinned zone or independent headings exist.

- [ ] **Step 3: Pass persisted manual order into the sidebar and retain every pinnable row in App ordering**

Add this exact binding to the existing `<SidebarConversations>` invocation:

```vue
:session-order="sidebarSessionOrder"
```

```ts
const isLocallyOrderableSession = (row: SidebarSectionRow) =>
  row.rowKind === 'session'
  && (row.sessionKind === 'chat' || row.sessionKind === 'cron')
  && !row.provisional

const currentOrder = sidebarSections.value
  .flatMap(section => section.rows)
  .filter(isLocallyOrderableSession)
  .map(row => row.key)
  .filter(key => key !== payload.key)
```

- [ ] **Step 4: Build display blocks and render zone headings in `SidebarConversations.vue`**

```ts
import { buildSidebarDisplayProjection, type SidebarDisplayRow } from '@/utils/sidebarDisplayProjection'

const props = withDefaults(defineProps<{
  sections: SidebarSection[]
  error: boolean
  loading: boolean
  currentKey: string
  contractDebugEnabled: boolean
  searchHint: string
  sessionOrder?: string[]
  canManageProjects?: boolean
  canCreateProjects?: boolean
}>(), { sessionOrder: () => [], canManageProjects: false, canCreateProjects: false })

const filteredSections = computed(() => props.sections.map(section => ({
  ...section,
  rows: (section.family === 'chats' && agentFilter.value
    ? filterChatRowsByAgent(section.rows, agentFilter.value)
    : section.rows).filter(row => row.rowKind !== 'workspace-empty'),
})).filter(section => section.rows.length > 0))

const displayProjection = computed(() => buildSidebarDisplayProjection(filteredSections.value, props.sessionOrder))
const visibleProjectRows = computed(() => filterCollapsedProjectRows(displayProjection.value.projects))

function reorderScope(row: SidebarDisplayRow): string {
  if (row.pinned) return 'pinned'
  if (row.displayZone === 'recents') return 'recents'
  return `project:${row.workspaceId || row.workspace || ''}`
}
```

Render one `.sidebar-zone` for non-empty Pinned, one for Projects, and one for Recents. Each uses the existing session/project row markup and this heading structure:

```vue
<div class="sidebar-zone-heading" :data-sidebar-zone-heading="zone">
  <span class="sidebar-zone-heading__label">{{ label }}</span>
  <span class="sidebar-zone-heading__count">{{ count }}</span>
  <!-- Project create remains on Projects; search and bulk controls remain available. -->
</div>
```

Remove `firstRecentRowKey`, `sidebar-history-row--recent-start`, and `data-zone-label`. Set `data-sidebar-zone` from each display row's `displayZone`.

- [ ] **Step 5: Add the Pinned translation to all supported locales**

```json
// en
"pinned": "Pinned"
// zh-Hans
"pinned": "置顶"
// de
"pinned": "Angeheftet"
// es
"pinned": "Fijadas"
// fr
"pinned": "Épinglées"
// ja
"pinned": "ピン留め"
```

- [ ] **Step 6: Run focused projection/component tests and verify GREEN**

Run both Task 1 Step 2 and Task 2 Step 2 commands. Expected: both files pass.

- [ ] **Step 7: Commit**

```powershell
git add opensquilla-webui/src/App.vue opensquilla-webui/src/components/SidebarConversations.vue opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts opensquilla-webui/src/locales/*.json
git commit -m "feat(webui): add global pinned sidebar zone"
```

### Task 3: Session hover/focus card

**Files:**
- Create: `opensquilla-webui/src/components/SidebarSessionHoverCard.vue`
- Create: `opensquilla-webui/src/components/SidebarSessionHoverCard.test.ts`
- Modify: `opensquilla-webui/src/components/SidebarConversations.vue`

**Interfaces:**
- Consumes: `title: string`, `updatedAt: number`, `projectName?: string`, and `position: { left: string; top: string }`.
- Produces: a non-interactive `role="tooltip"` card and `sessionPreviewPosition(rect, viewport)` for deterministic viewport clamping.

- [ ] **Step 1: Write failing hover-card tests**

```ts
import { createApp, h } from 'vue'
import SidebarSessionHoverCard, { sessionPreviewPosition } from './SidebarSessionHoverCard.vue'

function mountCard(props: { title: string; updatedAt: number; projectName?: string }) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  createApp({ render: () => h(SidebarSessionHoverCard, { ...props, position: { left: '12px', top: '12px' } }) }).mount(host)
  return host
}

it('shows title, relative time, and resolved project name', () => {
  const host = mountCard({ title: 'Investigate failure', updatedAt: Date.now() - 7_200_000, projectName: 'opensquilla' })
  expect(host.textContent).toContain('Investigate failure')
  expect(host.textContent).toContain('opensquilla')
  expect(host.querySelector('[data-testid="sidebar-session-project"]')).toBeTruthy()
})

it('omits only the project row for an unbound session', () => {
  const host = mountCard({ title: 'General task', updatedAt: Date.now() - 60_000, projectName: '' })
  expect(host.textContent).toContain('General task')
  expect(host.querySelector('[data-testid="sidebar-session-project"]')).toBeNull()
})

it('clamps the card inside the viewport and flips it left when needed', () => {
  expect(sessionPreviewPosition({ left: 900, right: 980, top: 740 }, { width: 1024, height: 768 })).toEqual({ left: '620px', top: '652px' })
})
```

- [ ] **Step 2: Run the hover-card tests and verify RED**

Run:

```powershell
$env:Path = 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:Path
& 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd' exec vitest run src/components/SidebarSessionHoverCard.test.ts
```

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement the card and position helper**

```vue
<script lang="ts">
export const SESSION_PREVIEW_WIDTH = 272
export const SESSION_PREVIEW_HEIGHT = 104
export function sessionPreviewPosition(rect: Pick<DOMRect, 'left' | 'right' | 'top'>, viewport: { width: number; height: number }) {
  const gap = 8
  const edge = 12
  const left = rect.right + gap + SESSION_PREVIEW_WIDTH <= viewport.width - edge
    ? rect.right + gap
    : Math.max(edge, rect.left - gap - SESSION_PREVIEW_WIDTH)
  const top = Math.max(edge, Math.min(rect.top, viewport.height - edge - SESSION_PREVIEW_HEIGHT))
  return { left: `${Math.round(left)}px`, top: `${Math.round(top)}px` }
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import { formatRelativeTime } from './sessions/sessionDisplay'
import Icon from './Icon.vue'
const props = defineProps<{ title: string; updatedAt: number; projectName?: string; position: { left: string; top: string } }>()
const relativeUpdatedAt = computed(() => formatRelativeTime(props.updatedAt))
</script>

<template>
  <div class="sidebar-session-preview" role="tooltip" :style="position">
    <div class="sidebar-session-preview__head">
      <strong>{{ title }}</strong>
      <span>{{ relativeUpdatedAt }}</span>
    </div>
    <div v-if="projectName" class="sidebar-session-preview__project" data-testid="sidebar-session-project">
      <Icon name="folder" :size="15" />
      <span>{{ projectName }}</span>
    </div>
  </div>
</template>
```

- [ ] **Step 4: Integrate hover, focus, Teleport, and teardown behavior**

```ts
const sessionPreview = ref<{ row: SidebarDisplayRow; position: { left: string; top: string } } | null>(null)

function openSessionPreview(row: SidebarDisplayRow, event: Event) {
  if (selectionMode.value || openMenuKey.value || row.rowKind !== 'session') return
  const anchor = event.currentTarget
  if (!(anchor instanceof HTMLElement)) return
  sessionPreview.value = {
    row,
    position: sessionPreviewPosition(anchor.getBoundingClientRect(), { width: window.innerWidth, height: window.innerHeight }),
  }
}

function closeSessionPreview() {
  sessionPreview.value = null
}

watch([selectionMode, openMenuKey], closeSessionPreview)
useDocumentEvent('scroll', closeSessionPreview, true)
onMounted(() => window.addEventListener('resize', closeSessionPreview))
onUnmounted(() => window.removeEventListener('resize', closeSessionPreview))
```

Attach `mouseenter`, `mouseleave`, `focusin`, and `focusout` handlers to session rows. Render the card outside the scroll container:

```vue
<Teleport to="body">
  <SidebarSessionHoverCard
    v-if="sessionPreview"
    :title="sessionPreview.row.title"
    :updated-at="sessionPreview.row.updatedAt"
    :project-name="sessionPreview.row.displayProjectName"
    :position="sessionPreview.position"
  />
</Teleport>
```

- [ ] **Step 5: Run hover and sidebar tests and verify GREEN**

Run Task 2 Step 2 and Task 3 Step 2. Expected: both files pass.

- [ ] **Step 6: Commit**

```powershell
git add opensquilla-webui/src/components/SidebarSessionHoverCard.vue opensquilla-webui/src/components/SidebarSessionHoverCard.test.ts opensquilla-webui/src/components/SidebarConversations.vue
git commit -m "feat(webui): show session hover details"
```

### Task 4: Styling and full verification

**Files:**
- Modify: `opensquilla-webui/src/assets/base.css:658-1155`
- Modify: `opensquilla-webui/src/styles/apple-modern.css:261-294`
- Test: `opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts`
- Test: `opensquilla-webui/src/components/SidebarSessionHoverCard.test.ts`

**Interfaces:**
- Consumes: `.sidebar-zone-heading` and `.sidebar-session-preview` markup from Tasks 2-3.
- Produces: aligned peer headings and an unclipped, pointer-transparent hover card using existing design tokens.

- [ ] **Step 1: Add failing CSS contract assertions**

```ts
import { readFileSync } from 'node:fs'

const baseCss = readFileSync(new URL('../assets/base.css', import.meta.url), 'utf8')

expect(baseCss).toContain('.sidebar-zone-heading')
expect(baseCss).toContain('.sidebar-session-preview')
expect(baseCss).not.toContain('.sidebar-history-row--recent-start::before')
```

- [ ] **Step 2: Run focused tests and verify RED**

Run Task 2 Step 2. Expected: FAIL because the new CSS selectors are missing.

- [ ] **Step 3: Implement shared heading and preview styles**

```css
.sidebar-zone-heading {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-height: 40px;
  padding: 4px 10px;
  color: var(--sidebar-text-soft);
  font-size: var(--fs-sm);
  font-weight: 400;
  line-height: 20px;
}

.sidebar-zone-heading__count {
  color: var(--sidebar-text-soft);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.sidebar-session-preview {
  position: fixed;
  z-index: 320;
  width: 272px;
  min-height: 72px;
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-lg);
  color: var(--text);
  pointer-events: none;
}

.sidebar-session-preview__head,
.sidebar-session-preview__project {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.sidebar-session-preview__head {
  justify-content: space-between;
}

.sidebar-session-preview__project {
  margin-top: var(--sp-3);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
```

Delete `.sidebar-history-row--recent-start` and its `::before` rule. Update `apple-modern.css` to target `.sidebar-zone-heading` and its count instead of only the old recents header selectors.

- [ ] **Step 4: Run all required verification**

```powershell
$env:Path = 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:Path
$pnpm = 'C:\Users\lrk\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd'
& $pnpm exec vitest run src/utils/sidebarDisplayProjection.test.ts src/components/SidebarConversations.workspaces.test.ts src/components/SidebarSessionHoverCard.test.ts
& $pnpm test:unit
& $pnpm typecheck
& $pnpm build
git diff --check
```

Expected: focused tests pass, all unit tests pass with zero failures, architecture/type checking exits 0, production build exits 0, and `git diff --check` reports no issues.

- [ ] **Step 5: Commit**

```powershell
git add opensquilla-webui/src/assets/base.css opensquilla-webui/src/styles/apple-modern.css opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts opensquilla-webui/src/components/SidebarSessionHoverCard.test.ts
git commit -m "style(webui): align sidebar zones and preview"
```
