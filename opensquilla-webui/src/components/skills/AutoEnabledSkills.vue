<template>
  <details v-if="skills.length" class="sk-group sk-group--proposals" open>
    <summary class="sk-group__head">
      <span class="sk-group__caret">▾</span>
      <span class="sk-group__label">{{ t('cronSkills.autoEnabled.title') }}</span>
      <span class="sk-group__count">{{ skills.length }}</span>
      <span class="sk-group__meta">{{ t('cronSkills.autoEnabled.meta') }}</span>
    </summary>
    <div class="sk-proposals-list">
      <div v-for="s in skills" :key="s.name" class="sk-proposal-row">
        <div class="sk-proposal-row__head">
          <code class="sk-proposal-row__id">{{ s.name }}</code>
          <span class="sk-prop-chip sk-prop-chip--ok">{{ t('cronSkills.autoEnabled.enabled') }}</span>
          <span class="sk-prop-chip sk-prop-chip--auto">{{ s.triggered_by || t('cronSkills.autoEnabled.unknown') }}</span>
          <span class="sk-prop-chip">{{ t('cronSkills.autoEnabled.risk', { level: s.risk_level || t('cronSkills.autoEnabled.unknown') }) }}</span>
          <span class="sk-prop-chip">{{ s.validation_profile || t('cronSkills.autoEnabled.unknown') }}</span>
          <span v-if="Array.isArray(s.skills) && s.skills.length" class="sk-prop-chip" :title="s.skills.join(', ')">{{ s.skills.slice(0, 4).join(', ') }}</span>
          <span v-if="s.proposal_id" class="sk-prop-hash" :title="t('cronSkills.autoEnabled.proposalId')">{{ s.proposal_id }}</span>
        </div>
        <div class="sk-proposal-row__actions">
          <button class="btn btn--ghost btn--sm" type="button" :disabled="mutationDisabled" @click="emit('disable', s.name)">{{ t('cronSkills.autoEnabled.disable') }}</button>
        </div>
      </div>
    </div>
  </details>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { AutoEnabledSkill } from '@/types/skills'

const { t } = useI18n()

defineProps<{
  skills: AutoEnabledSkill[]
  mutationDisabled?: boolean
}>()

const emit = defineEmits<{
  disable: [name: string]
}>()
</script>
