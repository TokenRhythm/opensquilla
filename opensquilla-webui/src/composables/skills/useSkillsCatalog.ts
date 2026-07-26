import { computed, ref, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { useRpcStore } from '@/stores/rpc'
import type {
  AutoEnabledSkill,
  ProposalsSettings,
  Skill,
  SkillDependencyCounts,
  SkillDependencySummary,
  SkillInstall,
  SkillLayerGroup,
  SkillStatTile,
} from '@/types/skills'

interface SkillsListData {
  skills?: Skill[]
}

export interface SkillsCatalogOptions {
  proposals: Ref<unknown[]>
  autoEnabledSkills: Ref<AutoEnabledSkill[]>
  proposalsSettings: Ref<ProposalsSettings>
  loadProposals: () => Promise<void>
}

export interface SkillsCatalog {
  allSkills: Ref<Skill[]>
  filterText: Ref<string>
  statusFilter: Ref<string>
  filteredSkills: ComputedRef<Skill[]>
  metaSkills: ComputedRef<Skill[]>
  visibleLayerGroups: ComputedRef<SkillLayerGroup[]>
  installedEmpty: ComputedRef<boolean>
  emptyMessage: ComputedRef<string>
  statTiles: ComputedRef<SkillStatTile[]>
  loadData: () => Promise<boolean>
  setStatusFilter: (key: string) => void
}

const LAYER_ORDER = ['workspace', 'bundled', 'managed', 'personal', 'project', 'extra']

// Known layer keys; labels/help text resolve through i18n by key.
const KNOWN_LAYERS = new Set(LAYER_ORDER)

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is string => typeof item === 'string')
    .map(item => item.trim())
    .filter(Boolean)
}

function stringGroups(value: unknown): string[][] {
  if (!Array.isArray(value)) return []
  return value
    .map(stringList)
    .filter(group => group.length > 0)
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function boolean(value: unknown): boolean {
  return value === true
}

function unique(values: string[]): string[] {
  return [...new Set(values)]
}

function normalizeDependencySummary(
  raw: unknown,
  fallback: Pick<Skill, 'missing_bins' | 'missing_env' | 'missing_env_any'> = {},
  depth = 0,
): SkillDependencySummary {
  const root = asRecord(raw)
  const declared = asRecord(root.declared)
  const declaredBinaries = asRecord(declared.binaries)
  const declaredApiEnv = asRecord(declared.api_env)
  const missing = asRecord(root.missing)
  const missingBinaries = asRecord(missing.binaries)
  const missingApiEnv = asRecord(missing.api_env)
  const inferred = asRecord(root.inferred)
  const subSkills = asRecord(root.sub_skill_dependencies)

  const legacyBins = stringList(fallback.missing_bins)
  const legacyEnv = stringList(fallback.missing_env)
  const legacyEnvAny = stringGroups(fallback.missing_env_any)
  const missingBinAll = stringList(missingBinaries.all)
  const missingBinAny = stringGroups(missingBinaries.any)
  const missingEnvAll = stringList(missingApiEnv.all)
  const missingEnvAny = stringGroups(missingApiEnv.any)
  const resolvedMissingBins = missingBinAll.length || missingBinAny.length
    ? missingBinAll
    : legacyBins
  const resolvedMissingEnv = missingEnvAll.length || missingEnvAny.length
    ? missingEnvAll
    : legacyEnv
  const resolvedMissingEnvAny = missingEnvAny.length ? missingEnvAny : legacyEnvAny

  const pythonPackages = Array.isArray(declared.python_packages)
    ? declared.python_packages.map((item) => {
      const packageRecord = asRecord(item)
      return {
        install_id: text(packageRecord.install_id),
        label: text(packageRecord.label),
        package: text(packageRecord.package),
        module: text(packageRecord.module),
      }
    }).filter(item => item.install_id || item.package || item.module)
    : []

  const pythonImports = Array.isArray(inferred.python_imports)
    ? inferred.python_imports.map((item) => {
      const importRecord = asRecord(item)
      return {
        module: text(importRecord.module),
        source: text(importRecord.source),
        not_enforced: boolean(importRecord.not_enforced),
      }
    }).filter(item => item.module)
    : []

  const inferredApiEnv = Array.isArray(inferred.api_env)
    ? inferred.api_env.map((item) => {
      const envRecord = asRecord(item)
      return {
        name: text(envRecord.name),
        sources: stringList(envRecord.sources),
        not_enforced: boolean(envRecord.not_enforced),
      }
    }).filter(item => item.name)
    : []

  // Dependency summaries can recursively contain sub-skill summaries. Bound
  // client-side recursion so a malformed Gateway payload cannot grow the UI
  // tree without limit; the current backend emits one-hop composition here.
  const childSkills = depth >= 4 || !Array.isArray(subSkills.skills)
    ? []
    : subSkills.skills.map((item) => {
      const child = asRecord(item)
      const name = text(child.name)
      if (!name) return null
      return {
        name,
        summary: normalizeDependencySummary(child.summary, {}, depth + 1),
      }
    }).filter((item): item is NonNullable<typeof item> => item !== null)

  const declaredBinAll = stringList(declaredBinaries.all)
  const declaredBinAny = stringList(declaredBinaries.any)
  const declaredEnvAll = stringList(declaredApiEnv.all)
  const declaredEnvAny = stringList(declaredApiEnv.any)
  const hasStructuredSummary = Object.keys(root).length > 0

  const summary: SkillDependencySummary = {
    declared: {
      binaries: {
        all: declaredBinAll.length || hasStructuredSummary ? declaredBinAll : legacyBins,
        any: declaredBinAny,
      },
      python_packages: pythonPackages,
      api_env: {
        all: declaredEnvAll.length || hasStructuredSummary ? declaredEnvAll : legacyEnv,
        any: declaredEnvAny.length || hasStructuredSummary
          ? declaredEnvAny
          : unique(legacyEnvAny.flat()),
      },
    },
    missing: {
      binaries: {
        all: resolvedMissingBins,
        any: missingBinAny,
      },
      api_env: {
        all: resolvedMissingEnv,
        any: resolvedMissingEnvAny,
      },
      count: 0,
    },
    inferred: {
      python_imports: pythonImports,
      api_env: inferredApiEnv,
      scan_errors: stringList(inferred.scan_errors),
    },
    sub_skill_dependencies: {
      skills: childSkills,
      missing_count: number(subSkills.missing_count),
      inferred_count: number(subSkills.inferred_count),
      missing_references: stringList(subSkills.missing_references),
    },
    declaration_quality: text(root.declaration_quality) || (
      legacyBins.length || legacyEnv.length || legacyEnvAny.length ? 'declared' : 'none'
    ),
  }

  // OR groups each represent one readiness requirement, so their alternatives
  // must not inflate the missing count.
  summary.missing.count = summary.missing.binaries.all.length
    + summary.missing.binaries.any.length
    + summary.missing.api_env.all.length
    + summary.missing.api_env.any.length
  return summary
}

export function normalizeSkill(skill: Skill): Skill {
  return {
    ...skill,
    missing_bins: stringList(skill.missing_bins),
    missing_env: stringList(skill.missing_env),
    missing_env_any: stringGroups(skill.missing_env_any),
    dependency_summary: normalizeDependencySummary(skill.dependency_summary, skill),
  }
}

export function skillDependencySummary(skill: Skill): SkillDependencySummary {
  return normalizeDependencySummary(skill.dependency_summary, skill)
}

export function skillDependencyCounts(skill: Skill): SkillDependencyCounts {
  const summary = skillDependencySummary(skill)
  return {
    python: summary.declared.python_packages.length,
    binaries: summary.declared.binaries.all.length + (summary.declared.binaries.any.length ? 1 : 0),
    env: summary.declared.api_env.all.length + (summary.declared.api_env.any.length ? 1 : 0),
    missing: summary.missing.count,
    advisory: summary.inferred.python_imports.length
      + summary.inferred.api_env.length
      + summary.inferred.scan_errors.length
      + summary.sub_skill_dependencies.inferred_count
      + summary.sub_skill_dependencies.missing_references.length,
  }
}

export function installActionsForCurrentDependencies(skill: Skill): SkillInstall[] {
  const summary = skillDependencySummary(skill)
  const missingBins = new Set([
    ...summary.missing.binaries.all,
    ...summary.missing.binaries.any.flat(),
  ])
  const declaredPackageIds = new Set(
    summary.declared.python_packages.map(item => item.install_id).filter(Boolean),
  )

  return (skill.install || []).filter((action) => {
    const actionBins = stringList(action.bins)
    if (actionBins.length) return actionBins.some(bin => missingBins.has(bin))
    return action.kind === 'uv'
      && skill.status !== 'ready'
      && declaredPackageIds.has(action.id)
  })
}

export function isMetaSkill(skill: Skill): boolean {
  return skill.kind === 'meta' || skill.kind === 'meta_sop'
}

export function skillReadyRank(skill: Skill): number {
  if (skill.status === 'ready') return 0
  if (skill.status === 'not_declared') return 1
  return 2
}

export function sortSkillsByReady(list: Skill[]): Skill[] {
  return [...list].sort((a, b) => {
    const ra = skillReadyRank(a)
    const rb = skillReadyRank(b)
    if (ra !== rb) return ra - rb
    return (a.name || '').localeCompare(b.name || '')
  })
}

export function skillStatusDotClass(skill: Skill): string {
  const status = skill.status || (skill.eligible ? 'ready' : 'needs_setup')
  if (status === 'ready') return 'is-ready'
  if (status === 'needs_setup') return 'is-needs'
  return 'is-unverified'
}

export function skillStatusDotTitle(skill: Skill): string {
  return skill.status_detail || (skill.eligible ? i18n.global.t('cronSkills.skills.dotReady') : i18n.global.t('cronSkills.skills.dotNeedsSetup'))
}

export function skillStatusChipClass(skill: Skill): string {
  const status = skill.status || (skill.eligible ? 'ready' : 'needs_setup')
  if (status === 'ready') return 'sk-chip--ok'
  if (status === 'not_declared') return 'sk-chip--unverified'
  return 'sk-chip--warn'
}

export function skillStatusChipText(skill: Skill): string {
  const status = skill.status || (skill.eligible ? 'ready' : 'needs_setup')
  if (status === 'ready') return i18n.global.t('cronSkills.skills.statusReady')
  if (status === 'not_declared') return i18n.global.t('cronSkills.skills.statusNoDeps')
  return i18n.global.t('cronSkills.skills.statusNeedsDeps')
}

export function skillLayerLabel(layer: string | undefined): string {
  if (layer && KNOWN_LAYERS.has(layer)) return i18n.global.t(`cronSkills.skills.layerLabel.${layer}`)
  return layer || i18n.global.t('cronSkills.skills.layerLabel.unknown')
}

export function skillLayerHelp(layer: string | undefined): string {
  if (layer && KNOWN_LAYERS.has(layer)) return i18n.global.t(`cronSkills.skills.layerHelp.${layer}`)
  return i18n.global.t('cronSkills.skills.layerHelp.default')
}

export function useSkillsCatalog(
  rpc: ReturnType<typeof useRpcStore>,
  options: SkillsCatalogOptions,
): SkillsCatalog {
  const t = i18n.global.t
  const allSkills = ref<Skill[]>([])
  const filterText = ref('')
  const statusFilter = ref('all')

  const filteredSkills = computed(() => {
    let skills = allSkills.value
    if (filterText.value) {
      const ft = filterText.value.toLowerCase()
      skills = skills.filter(s =>
        (s.name || '').toLowerCase().includes(ft) ||
        (s.description || '').toLowerCase().includes(ft) ||
        (s.triggers || []).some(t => t.toLowerCase().includes(ft))
      )
    }
    if (statusFilter.value === 'ready') {
      skills = skills.filter(s => s.status === 'ready')
    } else if (statusFilter.value === 'needs-setup') {
      skills = skills.filter(s => s.status === 'needs_setup')
    } else if (statusFilter.value === 'not-declared') {
      skills = skills.filter(s => s.status === 'not_declared')
    }
    return skills
  })

  const metaSkills = computed(() => sortSkillsByReady(filteredSkills.value.filter(s => isMetaSkill(s))))

  const layerGroups = computed(() => {
    const groups: Record<string, Skill[]> = {}
    filteredSkills.value.forEach(s => {
      if (isMetaSkill(s)) return
      const l = s.layer || 'extra'
      if (!groups[l]) groups[l] = []
      groups[l].push(s)
    })
    return groups
  })

  const visibleLayerGroups = computed(() => {
    return LAYER_ORDER
      .map(key => ({ key, skills: sortSkillsByReady(layerGroups.value[key] || []) }))
      .filter(g => g.skills.length > 0)
  })

  const installedEmpty = computed(() => {
    return filteredSkills.value.length === 0 &&
      !options.proposals.value.length &&
      !options.autoEnabledSkills.value.length &&
      !options.proposalsSettings.value.available
  })

  const emptyMessage = computed(() => {
    if (filterText.value) return t('cronSkills.skills.emptyFilter')
    if (statusFilter.value === 'ready') return t('cronSkills.skills.emptyReady')
    if (statusFilter.value === 'needs-setup') return t('cronSkills.skills.emptyNeedsSetup')
    if (statusFilter.value === 'not-declared') return t('cronSkills.skills.emptyNotDeclared')
    return t('cronSkills.skills.emptyNone')
  })

  const statTiles = computed<SkillStatTile[]>(() => {
    const total = allSkills.value.length
    const ready = allSkills.value.filter(s => s.status === 'ready').length
    const needs = allSkills.value.filter(s => s.status === 'needs_setup').length
    const notDeclared = allSkills.value.filter(s => s.status === 'not_declared').length
    const layers = new Set(allSkills.value.map(s => s.layer).filter(Boolean))

    return [
      { key: 'all', label: t('cronSkills.skills.tileAll'), value: String(total), hint: t('cronSkills.skills.tileLayerCount', { count: layers.size }), mods: 'sk-stat--accent' },
      { key: 'ready', label: t('cronSkills.skills.tileReady'), value: String(ready), hint: ready ? t('cronSkills.skills.tileReadyHintSome') : t('cronSkills.skills.tileReadyHintNone'), mods: '', tone: 'sk-stat__ok' },
      { key: 'needs-setup', label: t('cronSkills.skills.tileNeedsSetup'), value: String(needs), hint: needs ? t('cronSkills.skills.tileNeedsSetupHintSome') : t('cronSkills.skills.tileNeedsSetupHintNone'), mods: '', tone: 'sk-stat__warn' },
      { key: 'not-declared', label: t('cronSkills.skills.tileNotDeclared'), value: String(notDeclared), hint: t('cronSkills.skills.tileNotDeclaredHint'), mods: '' },
    ]
  })

  function setStatusFilter(key: string) {
    statusFilter.value = key
  }

  async function loadData() {
    try {
      await rpc.waitForConnection()
    } catch {
      return false
    }
    try {
      const data = await rpc.call<SkillsListData>('skills.list')
      allSkills.value = (data.skills || []).map(normalizeSkill)
      await options.loadProposals()
      return true
    } catch (err) {
      console.warn('Failed to load skills:', (err as Error).message)
      return false
    }
  }

  return {
    allSkills,
    filterText,
    statusFilter,
    filteredSkills,
    metaSkills,
    visibleLayerGroups,
    installedEmpty,
    emptyMessage,
    statTiles,
    loadData,
    setStatusFilter,
  }
}
