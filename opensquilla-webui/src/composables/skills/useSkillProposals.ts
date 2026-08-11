import { computed, ref, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { useRpcStore } from '@/stores/rpc'
import { useConfirm } from '@/composables/useConfirm'
import { useToasts } from '@/composables/useToasts'
import {
  createSkillMutationGate,
  type SkillMutationGate,
} from '@/composables/skills/useSkillMutationGate'
import type { AutoEnabledSkill, Proposal, ProposalsSettings } from '@/types/skills'

interface ProposalsListData {
  proposals?: Proposal[]
}

interface AutoEnabledListData {
  skills?: AutoEnabledSkill[]
}

interface ProposalSettingsData {
  settings?: ProposalsSettings
  status?: string
  reason?: string
}

interface ProposalShowData {
  status?: string
  reason?: string
  skill_md?: string
  gates?: Record<string, unknown>
  auto_enable_audit?: Proposal['auto_enable_audit']
}

interface ProposalActionData {
  status?: string
  reason?: string
}

export interface SkillProposals {
  proposals: Ref<Proposal[]>
  autoEnabledSkills: Ref<AutoEnabledSkill[]>
  proposalsSettings: Ref<ProposalsSettings>
  proposalsSettingsOn: ComputedRef<boolean>
  loadProposals: () => Promise<void>
  toggleAutoPropose: (key: string, value: boolean) => Promise<void>
  setAutoEnableRisk: (value: string) => Promise<void>
  showProposal: (proposalId: string) => Promise<Proposal | null>
  acceptProposal: (proposalId: string) => Promise<void>
  rejectProposal: (proposalId: string) => Promise<void>
  disableAutoEnabled: (name: string) => Promise<void>
}

const DEFAULT_PROPOSAL_SETTINGS: ProposalsSettings = {
  available: false,
  enabled: false,
  on_dream_complete: false,
  auto_enable: false,
  auto_enable_max_risk: 'low',
}

export function useSkillProposals(
  rpc: ReturnType<typeof useRpcStore>,
  loadData: () => Promise<void>,
  mutationGate: SkillMutationGate = createSkillMutationGate(),
): SkillProposals {
  const { confirm } = useConfirm()
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const proposals = ref<Proposal[]>([])
  const autoEnabledSkills = ref<AutoEnabledSkill[]>([])
  const proposalsSettings = ref<ProposalsSettings>({ ...DEFAULT_PROPOSAL_SETTINGS })

  const proposalsSettingsOn = computed(() => {
    const s = proposalsSettings.value
    return s.enabled || s.on_dream_complete || s.auto_enable
  })

  async function loadProposals() {
    try {
      const data = await rpc.call<ProposalsListData>('exec.proposals.list')
      proposals.value = data.proposals || []
    } catch {
      proposals.value = []
    }
    try {
      const data = await rpc.call<AutoEnabledListData>('exec.proposals.auto_enabled.list')
      autoEnabledSkills.value = data.skills || []
    } catch {
      autoEnabledSkills.value = []
    }
    try {
      const data = await rpc.call<ProposalSettingsData>('exec.proposals.settings.get')
      proposalsSettings.value = data.settings || proposalsSettings.value
    } catch {
      proposalsSettings.value = { ...DEFAULT_PROPOSAL_SETTINGS }
    }
  }

  async function toggleAutoPropose(key: string, value: boolean) {
    if (!mutationGate.acquire('proposal')) return
    try {
      const out = await rpc.call<ProposalSettingsData>('exec.proposals.settings.set', { [key]: value })
      if (out && out.status === 'error') {
        pushToast(t('cronSkills.proposals.toastSettingsFailed', { reason: out.reason || t('cronSkills.proposals.unknown') }), { tone: 'danger' })
        return
      }
      proposalsSettings.value = out.settings || proposalsSettings.value
      await loadData()
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastSettingsFailed', { reason: (err as Error).message }), { tone: 'danger' })
    } finally {
      mutationGate.release('proposal')
    }
  }

  async function setAutoEnableRisk(value: string) {
    if (!mutationGate.acquire('proposal')) return
    try {
      const out = await rpc.call<ProposalSettingsData>('exec.proposals.settings.set', { auto_enable_max_risk: value })
      if (out && out.status === 'error') {
        pushToast(t('cronSkills.proposals.toastSettingsFailed', { reason: out.reason || t('cronSkills.proposals.unknown') }), { tone: 'danger' })
        return
      }
      proposalsSettings.value = out.settings || proposalsSettings.value
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastSettingsFailed', { reason: (err as Error).message }), { tone: 'danger' })
    } finally {
      mutationGate.release('proposal')
    }
  }

  async function showProposal(proposalId: string): Promise<Proposal | null> {
    try {
      const data = await rpc.call<ProposalShowData>('exec.proposals.show', { proposal_id: proposalId })
      if (data.status !== 'ok') {
        pushToast(t('cronSkills.proposals.toastShowFailed', { reason: data.reason || t('cronSkills.proposals.unknown') }), { tone: 'danger' })
        return null
      }
      return { proposal_id: proposalId, ...data }
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastShowFailed', { reason: (err as Error).message }), { tone: 'danger' })
      return null
    }
  }

  async function acceptProposal(proposalId: string) {
    if (!mutationGate.acquire('proposal')) return
    try {
      let data = await rpc.call<ProposalActionData>('exec.proposals.accept', { proposal_id: proposalId })
      if (data.status === 'refused' && data.reason && data.reason.indexOf('gates') !== -1) {
        const ok = await confirm({
          title: t('cronSkills.proposals.acceptAnywayTitle'),
          body: t('cronSkills.proposals.acceptAnywayBody', { id: proposalId, reason: data.reason }),
          primaryLabel: t('cronSkills.proposals.forceAccept'),
        })
        if (!ok) return
        data = await rpc.call<ProposalActionData>('exec.proposals.accept', { proposal_id: proposalId, force: true })
      }
      if (data.status !== 'ok') {
        pushToast(t('cronSkills.proposals.toastAcceptFailed', { reason: data.reason || data.status }), { tone: 'danger' })
        return
      }
      await loadData()
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastAcceptFailed', { reason: (err as Error).message }), { tone: 'danger' })
    } finally {
      mutationGate.release('proposal')
    }
  }

  async function rejectProposal(proposalId: string) {
    const ok = await confirm({
      title: t('cronSkills.proposals.rejectTitle'),
      body: t('cronSkills.proposals.rejectBody', { id: proposalId }),
      primaryLabel: t('cronSkills.proposals.reject'),
    })
    if (!ok) return
    if (!mutationGate.acquire('proposal')) return
    try {
      const data = await rpc.call<ProposalActionData>('exec.proposals.reject', { proposal_id: proposalId })
      if (data.status !== 'ok') {
        pushToast(t('cronSkills.proposals.toastRejectFailed', { reason: data.reason || data.status }), { tone: 'danger' })
        return
      }
      await loadData()
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastRejectFailed', { reason: (err as Error).message }), { tone: 'danger' })
    } finally {
      mutationGate.release('proposal')
    }
  }

  async function disableAutoEnabled(name: string) {
    const ok = await confirm({
      title: t('cronSkills.proposals.disableTitle'),
      body: t('cronSkills.proposals.disableBody', { name }),
      primaryLabel: t('cronSkills.proposals.disable'),
    })
    if (!ok) return
    if (!mutationGate.acquire('proposal')) return
    try {
      const data = await rpc.call<ProposalActionData>('exec.proposals.auto_enabled.disable', { name })
      if (data.status !== 'ok') {
        pushToast(t('cronSkills.proposals.toastDisableFailed', { reason: data.reason || data.status }), { tone: 'danger' })
        return
      }
      await loadData()
    } catch (err) {
      pushToast(t('cronSkills.proposals.toastDisableFailed', { reason: (err as Error).message }), { tone: 'danger' })
    } finally {
      mutationGate.release('proposal')
    }
  }

  return {
    proposals,
    autoEnabledSkills,
    proposalsSettings,
    proposalsSettingsOn,
    loadProposals,
    toggleAutoPropose,
    setAutoEnableRisk,
    showProposal,
    acceptProposal,
    rejectProposal,
    disableAutoEnabled,
  }
}
