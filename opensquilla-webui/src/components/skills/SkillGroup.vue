<template>
  <details v-if="skills.length" class="sk-group sk-group--skills" :class="groupClass" open>
    <summary class="sk-group__head">
      <span class="sk-group__icon" :class="{ 'is-meta': meta }"><Icon name="skills" :size="15" /></span>
      <span class="sk-group__label">{{ title }}</span>
      <span class="sk-group__count">{{ skills.length }}</span>
      <span class="sk-group__meta">{{ description }}</span>
    </summary>
    <div class="sk-grid sk-tile-grid">
      <SkillTile
        v-for="skill in skills"
        :key="skill.name"
        variant="installed"
        :name="skill.name"
        :description="skill.description"
        :description-zh="skill.description_zh"
        :emoji="skill.emoji"
        :meta="meta"
        :status-dot-class="skillStatusDotClass(skill)"
        :status-dot-title="skillStatusDotTitle(skill)"
        @open="emit('open', skill)"
      />
    </div>
  </details>
</template>

<script setup lang="ts">
import Icon from '@/components/Icon.vue'
import SkillTile from '@/components/skills/SkillTile.vue'
import type { Skill } from '@/types/skills'
import {
  skillStatusDotClass,
  skillStatusDotTitle,
} from '@/composables/skills/useSkillsCatalog'

defineProps<{
  title: string
  description: string
  skills: Skill[]
  groupClass?: string
  meta?: boolean
}>()

const emit = defineEmits<{
  open: [skill: Skill]
}>()
</script>

<style scoped>
.sk-group__icon {
  align-items: center;
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  display: inline-flex;
  height: 28px;
  justify-content: center;
  width: 28px;
}
.sk-group__icon.is-meta {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
}
.sk-tile-grid {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
}
@media (max-width: 480px) {
  .sk-group__icon {
  align-items: center;
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  display: inline-flex;
  height: 28px;
  justify-content: center;
  width: 28px;
}
.sk-group__icon.is-meta {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
}
.sk-tile-grid { grid-template-columns: 1fr; }
}

/* WorkBuddy-like roomy three-column layout. */
.sk-tile-grid {
  gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
}
@media (max-width: 820px) {
  .sk-tile-grid { grid-template-columns: 1fr; }
}

/* Two wide columns keep names and two-line descriptions breathable. */
.sk-tile-grid {
  gap: 16px;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
}
@media (max-width: 900px) {
  .sk-tile-grid { grid-template-columns: 1fr; }
}

/* Keep the section glyph on the same baseline and canvas as its label. */
.sk-group__icon,
.sk-group__icon.is-meta {
  align-items: center;
  background: transparent;
  display: inline-flex;
  flex: 0 0 24px;
  height: 24px;
  justify-content: center;
  line-height: 0;
  width: 24px;
}
.sk-group__icon :deep(.icon) {
  align-items: center;
  display: inline-flex;
  height: 18px;
  justify-content: center;
  width: 18px;
}
.sk-group__icon :deep(svg) {
  display: block;
  height: 18px;
  width: 18px;
}
</style>
