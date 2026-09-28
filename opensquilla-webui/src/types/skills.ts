import type { SkillStatTile } from '@/components/skills/SkillsStats.vue'
import type { SelectedSkillRef } from '@/types/selectedSkills'

/** Metadata-only slash candidate. Skill instructions remain on the Gateway. */
export interface SkillCandidate extends SelectedSkillRef {
  generation: number
  description: string
  descriptionZh?: string
  aliases: readonly string[]
  kind: 'skill'
  source: 'extra' | 'bundled' | 'managed' | 'personal' | 'project' | 'workspace'
  disabled: boolean
  manualOnly: boolean
  ready: boolean
  reason?: string
  reasonCode?: string
}

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

export type SkillInstallState = 'tracked' | 'untracked' | 'missing' | 'drifted'
export type SkillLoadState =
  | 'loaded'
  | 'rejected'
  | 'not_discovered'
  | 'serving_previous'
  | 'validated_offline'
export type SkillSelectionState = 'active' | 'shadowed' | 'disabled' | 'hidden'
export type SkillCompatibilityState =
  | 'native'
  | 'instruction_only'
  | 'degraded'
  | 'unsupported'
export type SkillReadinessState = 'ready' | 'needs_setup' | 'unknown'

export interface SkillLifecycle {
  install_state: SkillInstallState
  load_state: SkillLoadState
  selection_state: SkillSelectionState
  compatibility_state: SkillCompatibilityState
  readiness_state: SkillReadinessState
}

export interface SkillDiagnostic {
  code: string
  severity: string
  phase: string
  blocking: boolean
  message: string
  hint?: string
  details?: Record<string, unknown>
}

export interface SkillSourceResolution {
  source?: string
  requestedIdentifier?: string
  canonicalIdentifier?: string
  packageIdentifier?: string
  publisher?: string
  version?: string
  immutableRevision?: string
  upstreamUrl?: string
  artifactKind?: string
  artifactDigest?: string
  resolverContentHash?: string
  trustState?: string
  immutable?: boolean
  diagnostics?: SkillDiagnostic[]
}

export interface SkillInvocationCapability {
  model_catalog: boolean
  skill_view: boolean
  user_completion: boolean
  direct_command: boolean
  argument_substitution: boolean
  scoped_tool_permissions: boolean
  sandbox_execution: 'unknown' | string
}

export interface SkillDependencyInstallOutcome {
  success: boolean
  complete: boolean
  message: string
  missingStill: SkillInstallMissingStill
}

export interface Skill {
  name: string
  disabled?: boolean
  user_invocable?: boolean
  disable_model_invocation?: boolean
  description?: string
  description_zh?: string
  emoji?: string
  status?: string
  status_detail?: string
  eligible?: boolean
  layer?: string
  kind?: string
  triggers?: string[]
  missing_bins?: string[]
  missing_env?: string[]
  missing_env_any?: string[][]
  dependency_summary?: SkillDependencySummary
  install?: SkillInstall[]
  homepage?: string
  file_path?: string
  content?: string
  instance_id?: string
  install_id?: string
  installed?: boolean
  active?: boolean
  instruction_usable?: boolean
  lifecycle?: SkillLifecycle
  diagnostics?: SkillDiagnostic[]
  invocation?: SkillInvocationCapability
  visibility?: 'public' | 'internal' | 'tombstone' | 'experimental' | string
  invocation_mode?: 'direct' | 'historical_only' | 'experimental_internal' | string
  generation?: number
  digest?: string
  source?: string
}

export interface RegistryResult {
  name: string
  description?: string
  version?: string
  author?: string
  identifier?: string
  source?: string
  license?: string
  homepage?: string
  upstream_url?: string
  origin_source?: string
  signature_status?: string
  content_hash?: string
  trust_level?: string
  installed?: boolean
  installId?: string
  install_reference?: string
  installReference?: string
  lifecycle?: SkillLifecycle
  instruction_usable?: boolean
  diagnostics?: SkillDiagnostic[]
}

export interface SkillLayerGroup {
  key: string
  skills: Skill[]
}

export type { SkillStatTile }
