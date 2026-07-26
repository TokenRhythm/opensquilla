<template>
  <dialog
    ref="dialogRef"
    class="sk-dialog"
    @click="onBackdropClick"
    @cancel.prevent="requestClose"
    @close="onNativeClose"
  >
    <div v-if="skill" class="sk-detail">
      <header class="sk-detail__header">
        <div class="sk-detail__head-left">
          <span v-if="skill.emoji" class="sk-detail__emoji">{{ skill.emoji }}</span>
          <strong class="sk-detail__name">{{ skill.name }}</strong>
          <div class="sk-detail__chips">
            <span class="sk-chip" :title="skillLayerHelp(skill.layer)">{{ skillLayerLabel(skill.layer) }}</span>
            <span class="sk-chip" :class="skillStatusChipClass(skill)">{{ skillStatusChipText(skill) }}</span>
          </div>
        </div>
        <button type="button" class="sk-iconbtn" :aria-label="t('common.close')" @click="requestClose">
          <Icon name="x" :size="18" />
        </button>
      </header>
      <section class="sk-detail__body">
        <p class="sk-detail__desc">{{ skill.description || '' }}</p>

        <div v-if="isMetaSkill(skill) && skill.triggers && skill.triggers.length" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.triggers') }}</div>
          <div class="sk-detail__sub-list">
            <code v-for="trg in skill.triggers" :key="trg" class="sk-chip sk-chip--trigger">{{ trg }}</code>
          </div>
        </div>

        <div v-if="isMetaSkill(skill) && skill.sub_skills && skill.sub_skills.length" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.composition', { kind: skill.kind === 'meta_sop' ? 'meta_sop' : 'meta', count: skill.sub_skills.length }) }}</div>
          <div class="sk-detail__sub-list">
            <span v-for="n in skill.sub_skills" :key="n" class="sk-chip sk-chip--sub">{{ n }}</span>
          </div>
        </div>

        <div class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.declaredDependencies') }}</div>
          <div class="sk-detail__dependency-grid">
            <div class="sk-detail__dependency-stat">
              <strong>{{ dependencyCounts.python }}</strong>
              <span>{{ t('cronSkills.skillDetail.pythonPackages') }}</span>
            </div>
            <div class="sk-detail__dependency-stat">
              <strong>{{ dependencyCounts.binaries }}</strong>
              <span>{{ t('cronSkills.skillDetail.binaries') }}</span>
            </div>
            <div class="sk-detail__dependency-stat">
              <strong>{{ dependencyCounts.env }}</strong>
              <span>{{ t('cronSkills.skillDetail.environment') }}</span>
            </div>
            <div class="sk-detail__dependency-stat" :class="{ 'is-missing': dependencyCounts.missing > 0 }">
              <strong>{{ dependencyCounts.missing }}</strong>
              <span>{{ t('cronSkills.skillDetail.missing') }}</span>
            </div>
          </div>
          <ul v-if="hasDeclaredDependencies" class="sk-detail__missing sk-detail__declared">
            <li v-for="pkg in dependencySummary.declared.python_packages" :key="`py:${pkg.install_id}:${pkg.package}`">
              <code>{{ pkg.package || pkg.module || pkg.install_id }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.pythonPackage') }}</span>
            </li>
            <li v-for="binary in dependencySummary.declared.binaries.all" :key="`bin:${binary}`">
              <code>{{ binary }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.binary') }}</span>
            </li>
            <li v-if="dependencySummary.declared.binaries.any.length">
              <code>{{ dependencySummary.declared.binaries.any.join(' / ') }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.binaryAny') }}</span>
            </li>
            <li v-for="env in dependencySummary.declared.api_env.all" :key="`env:${env}`">
              <code>{{ env }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.envVar') }}</span>
            </li>
            <li v-if="dependencySummary.declared.api_env.any.length">
              <code>{{ dependencySummary.declared.api_env.any.join(' / ') }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.envAny') }}</span>
            </li>
          </ul>
          <p v-else class="sk-detail__content-state">{{ t('cronSkills.skillDetail.noDeclaredDependencies') }}</p>
        </div>

        <div v-if="dependencySummary.missing.count" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.missing') }}</div>
          <ul class="sk-detail__missing">
            <li v-for="binary in dependencySummary.missing.binaries.all" :key="`missing-bin:${binary}`">
              <code>{{ binary }}</code> <span class="sk-dim">{{ t('cronSkills.skillDetail.binary') }}</span>
            </li>
            <li v-for="(group, index) in dependencySummary.missing.binaries.any" :key="`missing-bin-any:${index}`">
              <code>{{ group.join(' / ') }}</code> <span class="sk-dim">{{ t('cronSkills.skillDetail.binaryAny') }}</span>
            </li>
            <li v-for="env in dependencySummary.missing.api_env.all" :key="`missing-env:${env}`">
              <code>{{ env }}</code> <span class="sk-dim">{{ t('cronSkills.skillDetail.envVar') }}</span>
            </li>
            <li v-for="(group, index) in dependencySummary.missing.api_env.any" :key="`missing-env-any:${index}`">
              <code>{{ group.join(' / ') }}</code> <span class="sk-dim">{{ t('cronSkills.skillDetail.envAny') }}</span>
            </li>
          </ul>
        </div>

        <div v-if="hasAdvisories" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.advisories') }}</div>
          <p class="sk-detail__advisory-note">{{ t('cronSkills.skillDetail.notReadiness') }}</p>
          <ul class="sk-detail__missing">
            <li v-for="item in dependencySummary.inferred.python_imports" :key="`inferred-py:${item.module}:${item.source}`">
              <code>{{ item.module }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.inferredPython', { source: item.source }) }}</span>
            </li>
            <li v-for="item in dependencySummary.inferred.api_env" :key="`inferred-env:${item.name}`">
              <code>{{ item.name }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.inferredEnv') }}</span>
            </li>
            <li v-for="error in dependencySummary.inferred.scan_errors" :key="`scan:${error}`">
              <code>{{ error }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.scanAdvisory') }}</span>
            </li>
          </ul>
        </div>

        <div v-if="hasSubSkillRollup" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.subSkillRollup') }}</div>
          <ul class="sk-detail__missing">
            <li v-for="child in dependencySummary.sub_skill_dependencies.skills" :key="`child:${child.name}`">
              <code>{{ child.name }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.subSkillStatus', {
                missing: child.summary.missing.count,
                advisory: childAdvisoryCount(child.summary),
              }) }}</span>
            </li>
            <li v-for="name in dependencySummary.sub_skill_dependencies.missing_references" :key="`missing-child:${name}`">
              <code>{{ name }}</code>
              <span class="sk-dim">{{ t('cronSkills.skillDetail.missingSubSkill') }}</span>
            </li>
          </ul>
        </div>

        <div v-if="installFeedback" class="sk-detail__content-state sk-detail__content-state--warn" role="status">
          {{ installFeedback }}
        </div>

        <div v-if="installActions.length" class="sk-detail__section">
          <div class="sk-detail__section-title">{{ t('cronSkills.skillDetail.install') }}</div>
          <div v-for="i in installActions" :key="i.id" class="sk-detail__install-row">
            <span>{{ i.label || t('cronSkills.skillDetail.installVia', { kind: i.kind }) }}{{ i.bins?.length ? ` (${i.bins.join(', ')})` : '' }}</span>
            <button
              class="btn btn--primary btn--sm"
              :disabled="installingDepsId === i.id"
              @click="emit('installDeps', skill.name, i.id)"
            >
              {{ installingDepsId === i.id ? t('cronSkills.skillDetail.installing') : t('cronSkills.skillDetail.installVia', { kind: i.kind }) }}
            </button>
          </div>
        </div>

        <div v-if="skill.homepage" class="sk-detail__section">
          <a :href="skill.homepage" target="_blank" rel="noopener" class="sk-detail__link">{{ t('cronSkills.skillDetail.homepage') }}</a>
        </div>

        <div class="sk-detail__section">
          <div class="sk-detail__section-title">SKILL.md</div>
          <div v-if="loadingContent" class="sk-detail__content-state">{{ t('cronSkills.skillDetail.loadingContent') }}</div>
          <div v-else-if="contentError" class="sk-detail__content-state sk-detail__content-state--error">{{ contentError }}</div>
          <pre v-else class="sk-detail__pre">{{ skill.content || t('cronSkills.skillDetail.emptyContent') }}</pre>
        </div>
      </section>
      <footer class="sk-detail__foot">
        <small v-if="skill.file_path" class="sk-dim sk-detail__path">{{ skill.file_path }}</small>
        <button v-if="skill.layer === 'managed'" class="btn btn--sm" :disabled="uninstallingName === skill.name" @click="emit('uninstall', skill.name)">
          {{ uninstallingName === skill.name ? t('cronSkills.skillDetail.removing') : t('cronSkills.skillDetail.remove') }}
        </button>
      </footer>
    </div>

    <ProposalDetailPanel v-else-if="proposal" :proposal="proposal" @close="requestClose" />
  </dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ProposalDetailPanel from '@/components/skills/ProposalDetailPanel.vue'
import type { Proposal, Skill } from '@/types/skills'
import {
  isMetaSkill,
  installActionsForCurrentDependencies,
  skillDependencyCounts,
  skillDependencySummary,
  skillLayerHelp,
  skillLayerLabel,
  skillStatusChipClass,
  skillStatusChipText,
} from '@/composables/skills/useSkillsCatalog'

const { t } = useI18n()

const props = defineProps<{
  skill: Skill | null
  proposal: Proposal | null
  loadingContent: boolean
  contentError: string
  installFeedback: string
  installingDepsId: string | null
  uninstallingName: string | null
}>()

const emit = defineEmits<{
  close: []
  installDeps: [name: string, installId: string]
  uninstall: [name: string]
}>()

const dialogRef = ref<HTMLDialogElement | null>(null)

const dependencySummary = computed(() => props.skill
  ? skillDependencySummary(props.skill)
  : skillDependencySummary({ name: '' }))
const dependencyCounts = computed(() => props.skill
  ? skillDependencyCounts(props.skill)
  : { python: 0, binaries: 0, env: 0, missing: 0, advisory: 0 })
const installActions = computed(() => props.skill
  ? installActionsForCurrentDependencies(props.skill)
  : [])
const hasDeclaredDependencies = computed(() => {
  const declared = dependencySummary.value.declared
  return declared.python_packages.length > 0
    || declared.binaries.all.length > 0
    || declared.binaries.any.length > 0
    || declared.api_env.all.length > 0
    || declared.api_env.any.length > 0
})
const hasAdvisories = computed(() => dependencyCounts.value.advisory > 0)
const hasSubSkillRollup = computed(() => {
  const rollup = dependencySummary.value.sub_skill_dependencies
  return rollup.skills.length > 0 || rollup.missing_references.length > 0
})

function childAdvisoryCount(summary: ReturnType<typeof skillDependencySummary>): number {
  return summary.inferred.python_imports.length
    + summary.inferred.api_env.length
    + summary.inferred.scan_errors.length
    + summary.sub_skill_dependencies.inferred_count
    + summary.sub_skill_dependencies.missing_references.length
}

function selectionKey(): string {
  return props.skill
    ? `skill:${props.skill.name}`
    : props.proposal
      ? `proposal:${props.proposal.proposal_id}`
      : ''
}

function syncDialog(key = selectionKey()) {
  const dialog = dialogRef.value
  if (!dialog) return
  if (key) {
    if (!dialog.open) dialog.showModal()
    return
  }
  if (dialog.open) dialog.close()
}

watch(selectionKey, syncDialog, { flush: 'post' })
onMounted(() => syncDialog())

function requestClose() {
  emit('close')
}

function onNativeClose() {
  if (props.skill || props.proposal) requestClose()
}

function onBackdropClick(e: MouseEvent) {
  if (e.target === dialogRef.value) {
    requestClose()
  }
}
</script>
