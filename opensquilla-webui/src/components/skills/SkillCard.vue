<template>
  <button
    type="button"
    class="sk-card control-card control-card--interactive"
    :class="{ 'sk-card--meta': isMetaSkill(skill) || meta }"
    :title="skill.name + (skill.description ? ': ' + skill.description : '')"
    @click="emit('open', skill)"
  >
    <div class="sk-card__head">
      <span class="sk-card__dot" :class="skillStatusDotClass(skill)" :title="skillStatusDotTitle(skill)" />
      <span v-if="skill.emoji" class="sk-card__emoji">{{ skill.emoji }}</span>
      <span class="sk-card__name" :title="skill.name">{{ skill.name }}</span>
      <span v-if="skill.kind === 'meta_sop'" class="sk-card__kind-badge" title="meta_sop">SOP</span>
      <span v-else-if="isMetaSkill(skill)" class="sk-card__kind-badge" title="meta">META</span>
    </div>
    <p class="sk-card__desc" :title="skill.description || ''">{{ skill.description || '' }}</p>
    <span
      v-if="skillProviderCheckAtLaunch(skill)"
      class="sk-card__provider-status"
      :title="skillStatusChipText(skill)"
    >
      {{ skillStatusChipText(skill) }}
    </span>
    <div class="sk-card__deps" :aria-label="t('cronSkills.skillCard.dependencies')">
      <span class="sk-card__dep" :title="t('cronSkills.skillCard.pythonCount', { count: dependencyCounts.python })">
        <span aria-hidden="true">PY</span> {{ dependencyCounts.python }}
      </span>
      <span class="sk-card__dep" :title="t('cronSkills.skillCard.binaryCount', { count: dependencyCounts.binaries })">
        <span aria-hidden="true">BIN</span> {{ dependencyCounts.binaries }}
      </span>
      <span class="sk-card__dep" :title="t('cronSkills.skillCard.envCount', { count: dependencyCounts.env })">
        <span aria-hidden="true">ENV</span> {{ dependencyCounts.env }}
      </span>
      <span
        class="sk-card__dep"
        :class="{ 'sk-card__dep--missing': dependencyCounts.missing > 0 }"
        :title="t('cronSkills.skillCard.missingCount', { count: dependencyCounts.missing })"
      >
        <span aria-hidden="true">MISS</span> {{ dependencyCounts.missing }}
      </span>
      <span
        class="sk-card__dep"
        :class="{ 'sk-card__dep--advisory': dependencyCounts.advisory > 0 }"
        :title="t('cronSkills.skillCard.advisoryCount', { count: dependencyCounts.advisory })"
      >
        <span aria-hidden="true">ADV</span> {{ dependencyCounts.advisory }}
      </span>
    </div>
    <div v-if="skill.sub_skills && skill.sub_skills.length" class="sk-card__sub-row" :title="t('cronSkills.skillCard.subSkillsTitle')">
      <span class="sk-card__sub-label">{{ t('cronSkills.skillCard.uses') }}</span>
      <span v-for="n in skill.sub_skills.slice(0, 6)" :key="n" class="sk-card__sub-chip">{{ n }}</span>
      <span v-if="skill.sub_skills.length > 6" class="sk-card__sub-chip sk-card__sub-chip--more">+{{ skill.sub_skills.length - 6 }}</span>
    </div>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Skill } from '@/types/skills'
import {
  isMetaSkill,
  skillDependencyCounts,
  skillProviderCheckAtLaunch,
  skillStatusChipText,
  skillStatusDotClass,
  skillStatusDotTitle,
} from '@/composables/skills/useSkillsCatalog'

const { t } = useI18n()

const props = defineProps<{
  skill: Skill
  meta?: boolean
}>()

const dependencyCounts = computed(() => skillDependencyCounts(props.skill))

const emit = defineEmits<{
  open: [skill: Skill]
}>()
</script>
