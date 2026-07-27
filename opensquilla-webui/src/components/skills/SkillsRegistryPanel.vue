<template>
  <div class="sk-registry">
    <div class="sk-registry__head">
      <div class="sk-search-wrap sk-search-wrap--lg">
        <span class="sk-search-icon">
          <Icon name="search" :size="16" />
        </span>
        <input
          :value="registryQuery"
          class="sk-search-input sk-search-input--lg"
          type="search"
          :placeholder="t('cronSkills.registry.searchPlaceholder')"
          autocomplete="off"
          @input="emit('update:registryQuery', ($event.target as HTMLInputElement).value)"
          @keydown.enter="emit('search')"
        />
      </div>
      <button class="btn btn--primary" @click="emit('search')">{{ t('cronSkills.registry.search') }}</button>
    </div>
    <div class="sk-github-install">
      <div class="sk-search-wrap sk-search-wrap--lg">
        <span class="sk-search-icon">
          <Icon name="download" :size="16" />
        </span>
        <input
          :value="githubUrl"
          class="sk-search-input sk-search-input--lg"
          type="url"
          placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
          autocomplete="off"
          @input="emit('update:githubUrl', ($event.target as HTMLInputElement).value)"
          @keydown.enter="emit('installGithub')"
        />
      </div>
      <button class="btn btn--primary" @click="emit('installGithub')">{{ t('cronSkills.registry.installGithub') }}</button>
    </div>
    <div class="sk-registry__results">
      <template v-if="loading">
        <div class="sk-registry__loading">
          <span class="sk-spinner" />
          {{ t('cronSkills.registry.searching') }}
        </div>
      </template>
      <template v-else-if="results.length === 0">
        <div class="sk-registry__hint">
          <div class="sk-registry__hint-icon">
            <Icon name="skills" :size="36" />
          </div>
          <p>{{ t('cronSkills.registry.hintBrowse') }}</p>
          <p class="sk-dim">{{ t('cronSkills.registry.hintGithub') }}</p>
        </div>
      </template>
      <template v-else>
        <div class="sk-grid sk-tile-grid">
          <SkillTile
            v-for="row in resultRows"
            :key="row.installId"
            variant="registry"
            :name="row.name"
            :description="row.description"
            :source="row.source"
            :trust-level="row.trustLevel"
            :installed="row.installed"
            :busy="installingId === row.installId"
            @install="emit('install', String(row.installId), String(row.installSource))"
          />
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import SkillTile from '@/components/skills/SkillTile.vue'
import type { RegistryResult } from '@/types/skills'

const { t } = useI18n()

const props = defineProps<{
  registryQuery: string
  githubUrl: string
  results: RegistryResult[]
  loading: boolean
  installingId: string | null
}>()

const emit = defineEmits<{
  'update:registryQuery': [value: string]
  'update:githubUrl': [value: string]
  search: []
  installGithub: []
  install: [identifier: string, source: string]
}>()

const resultRows = computed(() =>
  props.results.map(r => ({
    name: r.name,
    description: (r.description || '').slice(0, 120),
    source: r.source || '',
    trustLevel: r.trust_level || 'community',
    installed: !!r.installed,
    installId: r.identifier || r.name,
    installSource: r.source || 'clawhub',
  })),
)
</script>

<style scoped>
.sk-tile-grid {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
}
@media (max-width: 480px) {
  .sk-tile-grid { grid-template-columns: 1fr; }
}
</style>
