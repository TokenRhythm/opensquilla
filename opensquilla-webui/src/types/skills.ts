import type { SkillStatTile } from '@/components/skills/SkillsStats.vue'

export interface SkillInstall {
  id: string
  kind: string
  label?: string
  bins?: string[]
}

export interface SkillDeclaredPythonPackage {
  install_id: string
  label: string
  package: string
  module: string
}

export interface SkillInferredPythonImport {
  module: string
  source: string
  not_enforced: boolean
}

export interface SkillInferredApiEnv {
  name: string
  sources: string[]
  not_enforced: boolean
}

export interface SkillDependencySummary {
  declared: {
    binaries: {
      all: string[]
      any: string[]
    }
    python_packages: SkillDeclaredPythonPackage[]
    api_env: {
      all: string[]
      any: string[]
    }
  }
  missing: {
    binaries: {
      all: string[]
      any: string[][]
    }
    api_env: {
      all: string[]
      any: string[][]
    }
    count: number
  }
  inferred: {
    python_imports: SkillInferredPythonImport[]
    api_env: SkillInferredApiEnv[]
    scan_errors: string[]
  }
  sub_skill_dependencies: {
    skills: Array<{
      name: string
      summary: SkillDependencySummary
    }>
    missing_count: number
    inferred_count: number
    missing_references: string[]
  }
  declaration_quality: 'declared' | 'partial' | 'undeclared_inferred' | 'none' | string
}

export interface SkillDependencyCounts {
  python: number
  binaries: number
  env: number
  missing: number
  advisory: number
}

export interface SkillInstallMissingStill {
  bins: string[]
  env: string[]
  env_any: string[][]
}

export interface SkillDependencyInstallOutcome {
  success: boolean
  complete: boolean
  message: string
  missingStill: SkillInstallMissingStill
}

export interface Skill {
  name: string
  description?: string
  emoji?: string
  status?: string
  status_detail?: string
  eligible?: boolean
  layer?: string
  kind?: string
  sub_skills?: string[]
  triggers?: string[]
  missing_bins?: string[]
  missing_env?: string[]
  missing_env_any?: string[][]
  dependency_summary?: SkillDependencySummary
  install?: SkillInstall[]
  homepage?: string
  file_path?: string
  content?: string
}

export interface Proposal {
  proposal_id: string
  auto_enable_eligible?: boolean
  triggered_by?: string
  auto_enable?: {
    status?: string
    reason?: string
    validation_profile?: string
  }
  chain_hash?: string
  skill_md?: string
  gates?: Record<string, unknown>
  auto_enable_audit?: {
    status?: string
    risk_level?: string
    max_risk?: string
    validation_profile?: string
    reason?: string
    skills?: string[]
    tools?: string[]
    reasons?: string[]
  }
}

export interface AutoEnabledSkill {
  name: string
  risk_level?: string
  triggered_by?: string
  validation_profile?: string
  skills?: string[]
  proposal_id?: string
}

export interface ProposalsSettings {
  available: boolean
  enabled: boolean
  on_dream_complete: boolean
  auto_enable: boolean
  auto_enable_max_risk: string
  cron?: string
}

export interface RegistryResult {
  name: string
  description?: string
  identifier?: string
  source?: string
  trust_level?: string
  installed?: boolean
}

export interface SkillLayerGroup {
  key: string
  skills: Skill[]
}

export type { SkillStatTile }
