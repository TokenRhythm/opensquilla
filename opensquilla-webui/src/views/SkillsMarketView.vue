<template>
  <div class="sk-mkt control-stage control-stage--spacious">
    <header class="sk-mkt__header control-stage__header">
      <div class="control-stage__title-block">
        <h1 class="control-stage__title">{{ t('cronSkills.market.title') }}</h1>
        <p class="control-stage__subtitle">{{ t('cronSkills.market.subtitle') }}</p>
      </div>
      <div class="control-stage__actions">
        <div class="sk-mkt__search">
          <span class="sk-mkt__search-icon"><Icon name="search" :size="16" /></span>
          <input
            v-model="market.query.value"
            data-testid="market-search-input"
            class="sk-mkt__search-input"
            type="search"
            :placeholder="t('cronSkills.market.searchPlaceholder')"
            :aria-label="t('cronSkills.market.searchLabel')"
            autocomplete="off"
            @keydown.enter="runSearch"
          />
        </div>
        <button
          class="btn btn--primary"
          data-testid="market-search-button"
          type="button"
          :disabled="market.loading.value"
          :aria-busy="market.loading.value"
          @click="runSearch"
        >
          {{ market.loading.value
            ? t('cronSkills.registry.searchingShort')
            : t('cronSkills.registry.search') }}
        </button>
      </div>
    </header>

    <div class="sk-mkt__toolbar" role="group" :aria-label="t('cronSkills.market.filterLabel')">
      <div class="sk-mkt__sources">
        <button
          v-for="tab in sourceTabs"
          :key="tab.id"
          class="sk-mkt__chip"
          :class="{ 'is-active': market.source.value === tab.id }"
          :data-testid="`market-source-${tab.id}`"
          type="button"
          :aria-pressed="market.source.value === tab.id"
          @click="market.source.value = tab.id"
        >
          <Icon :name="tab.icon" :size="14" />
          <span>{{ tab.label }}</span>
        </button>
        <button
          v-if="favorites.favoriteCount.value || favoritesOnly"
          class="sk-mkt__chip"
          :class="{ 'is-active': favoritesOnly }"
          data-testid="market-favorites-filter"
          type="button"
          :aria-pressed="favoritesOnly"
          @click="favoritesOnly = !favoritesOnly"
        >
          <Icon name="star" :size="14" />
          <span>{{ t('cronSkills.market.favorites') }}</span>
          <span v-if="favorites.favoriteCount.value" class="sk-mkt__chip-count"
            data-testid="market-favorites-count">{{ favorites.favoriteCount.value }}</span>
        </button>
      </div>

      <div class="sk-mkt__sort">
        <label class="sk-mkt__sort-label" for="market-sort">{{ t('cronSkills.market.sortLabel') }}</label>
        <select id="market-sort" v-model="market.sort.value" class="sk-mkt__sort-select" data-testid="market-sort">
          <option value="relevance">{{ t('cronSkills.market.sortRelevance') }}</option>
          <option value="downloads">{{ t('cronSkills.market.sortDownloads') }}</option>
          <option value="installs">{{ t('cronSkills.market.sortInstalls') }}</option>
          <option value="name">{{ t('cronSkills.market.sortName') }}</option>
        </select>
      </div>
    </div>

    <div
      v-if="market.categories.value.length"
      class="sk-mkt__categories"
      role="group"
      :aria-label="t('cronSkills.market.categoryAll')"
    >
      <button
        class="sk-mkt__chip sk-mkt__chip--small"
        :class="{ 'is-active': !market.category.value }"
        type="button"
        :aria-pressed="!market.category.value"
        @click="market.category.value = ''"
      >{{ t('cronSkills.market.categoryAll') }}</button>
      <button
        v-for="name in market.categories.value"
        :key="name"
        class="sk-mkt__chip sk-mkt__chip--small"
        :class="{ 'is-active': market.category.value === name }"
        :data-testid="`market-category-${name}`"
        type="button"
        :aria-pressed="market.category.value === name"
        @click="market.category.value = name"
      >{{ name }}</button>
    </div>

    <div v-if="market.message.value" class="sk-mkt__callout sk-mkt__callout--danger" role="alert" data-testid="market-error">
      {{ market.message.value }}
    </div>
    <div
      v-for="diagnostic in market.diagnostics.value"
      :key="`${diagnostic.phase}:${diagnostic.code}:${diagnostic.message}`"
      class="sk-mkt__callout"
      :class="diagnostic.blocking ? 'sk-mkt__callout--danger' : 'sk-mkt__callout--warn'"
      role="status"
    >
      <strong>{{ diagnostic.code }}</strong>
      <span>{{ diagnostic.message }}</span>
    </div>

    <p v-if="displayedRows.length" class="sk-mkt__count" role="status">
      {{ t('cronSkills.market.resultsCount', { count: displayedRows.length }) }}
    </p>

    <section
      v-if="featuredRows.length"
      class="sk-mkt__featured"
      data-testid="market-featured"
      :aria-label="t('cronSkills.market.featured')"
    >
      <h2 class="sk-mkt__featured-title">
        <Icon name="store" :size="15" />
        {{ t('cronSkills.market.featured') }}
      </h2>
      <div class="sk-mkt__featured-row">
        <button
          v-for="row in featuredRows"
          :key="`featured-${rowKey(row)}`"
          class="sk-mkt__featured-item"
          type="button"
          :data-testid="`market-featured-${row.name}`"
          @click="openDetail(row)"
        >
          <span class="sk-mkt__avatar sk-mkt__avatar--sm" aria-hidden="true">
            <img
              v-if="row.icon_url && !failedIcons.has(rowKey(row))"
              :src="row.icon_url"
              alt=""
              loading="lazy"
              referrerpolicy="no-referrer"
              @error="failedIcons.add(rowKey(row))"
            />
            <span v-else>{{ initials(row.name) }}</span>
          </span>
          <span class="sk-mkt__featured-body">
            <strong :title="row.name">{{ row.name }}</strong>
            <span class="sk-mkt__featured-meta">
              <Icon name="download" :size="11" />
              {{ compactCount(row.downloads) }}
            </span>
          </span>
        </button>
      </div>
    </section>

    <div v-if="market.loading.value && !displayedRows.length" class="sk-mkt__empty" data-testid="market-loading" role="status">
      <span class="sk-spinner" aria-hidden="true" />
      <span>{{ t('cronSkills.market.loading') }}</span>
    </div>

    <div v-else-if="displayedRows.length" class="sk-mkt__grid" data-testid="market-grid">
      <article
        v-for="row in displayedRows"
        :key="rowKey(row)"
        class="sk-mkt__card"
        :data-installed="row.installed || undefined"
        :data-testid="`market-card-${row.name}`"
      >
        <div class="sk-mkt__card-head">
          <span class="sk-mkt__avatar" aria-hidden="true">
            <img
              v-if="row.icon_url && !failedIcons.has(rowKey(row))"
              :src="row.icon_url"
              alt=""
              loading="lazy"
              referrerpolicy="no-referrer"
              @error="failedIcons.add(rowKey(row))"
            />
            <span v-else>{{ initials(row.name) }}</span>
          </span>
          <button
            class="sk-mkt__card-open"
            type="button"
            :data-testid="`market-detail-${row.name}`"
            :aria-label="`${t('cronSkills.registry.viewDetails')}: ${row.name}`"
            @click="openDetail(row)"
          >
            <strong :title="row.name">{{ row.name }}</strong>
            <span class="sk-mkt__card-source">{{ sourceLabel(row.source) }}</span>
          </button>
          <span v-if="row.installed" class="sk-mkt__badge sk-mkt__badge--ok" data-testid="market-installed-badge">
            <Icon name="check" :size="12" />
            {{ t('cronSkills.registry.installed') }}
          </span>
          <button
            class="sk-mkt__star"
            :class="{ 'is-active': favorites.isFavorite(row) }"
            type="button"
            :data-testid="`market-favorite-${row.name}`"
            :aria-label="favorites.isFavorite(row)
              ? t('cronSkills.market.unfavorite')
              : t('cronSkills.market.favorite')"
            :aria-pressed="favorites.isFavorite(row)"
            @click="favorites.toggleFavorite(row)"
          >
            <Icon name="star" :size="15" />
          </button>
        </div>

        <p v-if="row.description" class="sk-mkt__card-desc">{{ row.description }}</p>

        <div class="sk-mkt__card-tags">
          <span v-if="row.category" class="sk-mkt__tag sk-mkt__tag--category">{{ row.category }}</span>
          <span v-for="tag in cardTags(row)" :key="tag" class="sk-mkt__tag">{{ tag }}</span>
          <span v-if="row.version" class="sk-mkt__tag">{{ row.version }}</span>
          <span v-if="row.author" class="sk-mkt__tag">{{ row.author }}</span>
          <span v-if="row.license" class="sk-mkt__tag">{{ row.license }}</span>
        </div>

        <div class="sk-mkt__card-foot">
          <span class="sk-mkt__stats">
            <span v-if="row.downloads" class="sk-mkt__stat" :title="t('cronSkills.market.downloads', { count: row.downloads })">
              <Icon name="download" :size="13" />
              {{ compactCount(row.downloads) }}
            </span>
            <span v-if="row.installs" class="sk-mkt__stat" :title="t('cronSkills.market.installs', { count: row.installs })">
              <Icon name="skills" :size="13" />
              {{ compactCount(row.installs) }}
            </span>
            <span v-if="!row.downloads && !row.installs" class="sk-mkt__trust">{{ row.trust_level || t('cronSkills.registry.community') }}</span>
          </span>

          <span
            v-if="queueStatus(row)"
            class="sk-mkt__queue-status"
            :data-status="queueStatus(row)"
            role="status"
          >
            <span v-if="isRunningStatus(queueStatus(row))" class="sk-spinner" aria-hidden="true" />
            <Icon v-else-if="queueStatus(row) === 'installed' || queueStatus(row) === 'unchanged'" name="check" :size="14" />
            <span>{{ t(`cronSkills.registry.queueStatus.${queueStatus(row)}`) }}</span>
          </span>

          <button
            v-if="showRetry(row)"
            class="btn btn--sm"
            :class="requiresRisk(row) ? 'btn--primary' : 'btn--ghost'"
            type="button"
            data-testid="market-retry-button"
            :disabled="installBlocked"
            @click="retryRow(row)"
          >
            <Icon name="refresh" :size="14" />
            <span>{{ requiresRisk(row) ? t('cronSkills.registry.installAnyway') : t('cronSkills.registry.retry') }}</span>
          </button>
          <button
            v-else
            class="btn btn--sm"
            :class="row.installed ? 'btn--ghost' : 'btn--primary'"
            type="button"
            :data-testid="`market-install-${row.name}`"
            :disabled="installBlocked || isRunningStatus(queueStatus(row)) || row.installed"
            :aria-busy="isRunningStatus(queueStatus(row))"
            @click="installRow(row)"
          >
            <Icon v-if="row.installed" name="check" :size="14" />
            <span>{{ installLabel(row) }}</span>
          </button>
        </div>
      </article>
    </div>

    <div v-else class="sk-mkt__empty" data-testid="market-empty">
      <Icon name="store" :size="30" />
      <span>{{ favoritesOnly
        ? t('cronSkills.market.favoritesEmpty')
        : market.loaded.value ? t('cronSkills.market.emptyHint') : t('cronSkills.market.browseHint') }}</span>
    </div>

    <SkillMarketDetailDialog
      :row="detailRow"
      :status="detailRow ? queueStatus(detailRow) : ''"
      :running="detailRow ? isRunningStatus(queueStatus(detailRow)) : false"
      :blocked="installBlocked"
      :show-retry="detailRow ? showRetry(detailRow) : false"
      :requires-risk="detailRow ? requiresRisk(detailRow) : false"
      :install-label="detailRow ? installLabel(detailRow) : ''"
      :status-label="detailStatusLabel"
      :favorited="detailRow ? favorites.isFavorite(detailRow) : false"
      @close="closeDetail"
      @toggle-favorite="detailRow && favorites.toggleFavorite(detailRow)"
      @install="detailRow && installRow(detailRow)"
      @retry="detailRow && retryRow(detailRow)"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import SkillMarketDetailDialog from '@/components/skills/SkillMarketDetailDialog.vue'
import { useSkillMarket, type MarketSource } from '@/composables/skills/useSkillMarket'
import {
  skillInstallRequiresRiskAcknowledgement,
  skillRegistryOperationKey,
  useSkillRegistry,
  type SkillInstallSource,
} from '@/composables/skills/useSkillRegistry'
import { createSkillMutationGate } from '@/composables/skills/useSkillMutationGate'
import { useSkillMarketFavorites } from '@/composables/skills/useSkillMarketFavorites'
import { SKILL_CATALOG_KEY } from '@/modules/skillCatalog'
import type { RegistryResult } from '@/types/skills'

const { t } = useI18n()
const injectedCatalog = inject(SKILL_CATALOG_KEY)
if (!injectedCatalog) throw new Error('SkillCatalog was not provided')
const skillCatalog = injectedCatalog

const market = useSkillMarket(skillCatalog)
const mutationGate = createSkillMutationGate()
const failedIcons = ref(new Set<string>())
const favorites = useSkillMarketFavorites()
const favoritesOnly = ref(false)

async function loadData(): Promise<boolean> {
  await market.search({ silent: true })
  return true
}

const registry = useSkillRegistry(skillCatalog, loadData, mutationGate)

const detailRow = ref<RegistryResult | null>(null)

function openDetail(row: RegistryResult) {
  detailRow.value = row
}

function closeDetail() {
  detailRow.value = null
}

const detailStatusLabel = computed(() => {
  const status = detailRow.value ? queueStatus(detailRow.value) : ''
  return status ? t(`cronSkills.registry.queueStatus.${status}`) : ''
})

// Favorites narrows whatever the market currently shows; it never calls the
// Gateway, so the shelf/search/category filters stay exactly as typed.
const displayedRows = computed<RegistryResult[]>(() => {
  const rows = market.visibleResults.value
  return favoritesOnly.value ? rows.filter(row => favorites.isFavorite(row)) : rows
})

// Trending shelf: browse mode only (no query/category), top rows by downloads.
const featuredRows = computed<RegistryResult[]>(() => {
  if (favoritesOnly.value) return []
  if (market.query.value.trim() || market.category.value) return []
  return [...displayedRows.value]
    .filter(row => (row.downloads || 0) > 0)
    .sort((a, b) => (b.downloads || 0) - (a.downloads || 0))
    .slice(0, 6)
})

const sourceTabs = computed(() => [
  { id: 'all' as MarketSource, label: t('cronSkills.market.sourceAll'), icon: 'store' as const },
  { id: 'clawhub' as MarketSource, label: t('cronSkills.registry.sourceClawHub'), icon: 'download' as const },
  { id: 'skillhub' as MarketSource, label: t('cronSkills.registry.sourceSkillHub'), icon: 'download' as const },
  { id: 'github' as MarketSource, label: t('cronSkills.registry.sourceGitHub'), icon: 'share' as const },
])

const installBlocked = computed(() =>
  registry.queueRunning.value || registry.mutationBusy.value)

onMounted(() => {
  void market.search()
})

function runSearch() {
  void market.search()
}

function rowKey(row: RegistryResult): string {
  return row.installReference || row.install_reference || row.identifier || row.name
}

function activitySource(source: string | undefined): SkillInstallSource {
  return source === 'github' || source === 'skillhub' ? source : 'clawhub'
}

function queueItem(row: RegistryResult) {
  const source = activitySource(row.source)
  const key = skillRegistryOperationKey(rowKey(row), row.source || 'clawhub')
  return registry.installActivities.value[source]?.items.find(item => item.id === key)
}

function queueStatus(row: RegistryResult): string {
  return queueItem(row)?.status || ''
}

function isRunningStatus(status: string): boolean {
  return status === 'queued' || status === 'installing'
    || status === 'waiting' || status === 'cancelling'
}

function showRetry(row: RegistryResult): boolean {
  const status = queueStatus(row)
  return status === 'failed' || status === 'unknown' || status === 'cancelled'
}

function requiresRisk(row: RegistryResult): boolean {
  return skillInstallRequiresRiskAcknowledgement(queueItem(row)?.result)
}

function retryRow(row: RegistryResult) {
  const item = queueItem(row)
  if (!item) return
  void registry.retryQueueItem(item.id, requiresRisk(row))
}

function installRow(row: RegistryResult) {
  if (row.installed) return
  const installId = rowKey(row)
  void registry.installSkill(installId, row.source || 'clawhub', row.name)
}

function installLabel(row: RegistryResult): string {
  const status = queueStatus(row)
  if (isRunningStatus(status)) return t(`cronSkills.registry.queueStatus.${status}`)
  if (row.installed) return t('cronSkills.registry.installed')
  return t('cronSkills.registry.install')
}

function sourceLabel(source: string | undefined): string {
  if (source === 'skillhub') return t('cronSkills.registry.sourceSkillHub')
  if (source === 'github') return t('cronSkills.registry.sourceGitHub')
  if (source === 'clawhub') return t('cronSkills.registry.sourceClawHub')
  return source || ''
}

function initials(name: string): string {
  const parts = name.trim().split(/[\s_-]+/).filter(Boolean)
  if (!parts.length) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

function cardTags(row: RegistryResult): string[] {
  const tags = Array.isArray(row.tags) ? row.tags : []
  return tags
    .filter((tag): tag is string => typeof tag === 'string' && tag.trim().length > 0)
    .slice(0, 3)
}

function compactCount(value: number | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return ''
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`
  return String(value)
}
</script>

<style scoped>
.sk-mkt {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.sk-mkt__header {
  align-items: flex-start;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: space-between;
}

.sk-mkt__header .control-stage__title-block {
  min-width: 0;
}

.sk-mkt__header .control-stage__title {
  font-size: 1.35rem;
  margin: 0;
}

.sk-mkt__header .control-stage__subtitle {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 6px 0 0;
}

.sk-mkt__header .control-stage__actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.sk-mkt__search {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-dim);
  display: flex;
  gap: 8px;
  min-width: 260px;
  padding: 0 10px;
}

.sk-mkt__search:focus-within {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-mkt__search-input {
  background: transparent;
  border: 0;
  box-shadow: none;
  color: var(--text);
  min-width: 0;
  outline: 0;
  padding: 9px 0;
  width: 100%;
}

.sk-mkt__toolbar {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: space-between;
}

.sk-mkt__sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sk-mkt__chip {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 650;
  gap: 5px;
  min-height: 30px;
  padding: 4px 12px;
}

.sk-mkt__chip.is-active {
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  border-color: var(--accent);
  color: var(--accent);
}

.sk-mkt__chip:focus-visible {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-mkt__chip--small {
  min-height: 26px;
  padding: 2px 10px;
}

.sk-mkt__chip-count {
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  border-radius: 999px;
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 1;
  margin-left: 1px;
  min-width: 16px;
  padding: 3px 5px;
  text-align: center;
}

.sk-mkt__sort {
  align-items: center;
  display: flex;
  gap: 7px;
}

.sk-mkt__sort-label {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.sk-mkt__sort-select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-xs);
  min-height: 30px;
  padding: 4px 8px;
}

.sk-mkt__categories {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sk-mkt__callout {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--border));
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-xs);
  gap: 3px;
  padding: 10px 12px;
}

.sk-mkt__callout--danger {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
}

.sk-mkt__callout strong {
  color: var(--text);
}

.sk-mkt__count {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin: 0;
}

.sk-mkt__grid {
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
}

.sk-mkt__card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 14px;
  transition: border-color var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.sk-mkt__card:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  transform: translateY(-1px);
}

.sk-mkt__card[data-installed] {
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
}

.sk-mkt__card-head {
  align-items: center;
  display: flex;
  gap: 10px;
  min-width: 0;
}

.sk-mkt__avatar {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 700;
  height: 34px;
  justify-content: center;
  overflow: hidden;
  width: 34px;
}

.sk-mkt__avatar img {
  height: 100%;
  object-fit: cover;
  width: 100%;
}

.sk-mkt__avatar--sm {
  border-radius: var(--radius-sm);
  font-size: 10px;
  height: 26px;
  width: 26px;
}

.sk-mkt__featured {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sk-mkt__featured-title {
  align-items: center;
  color: var(--text);
  display: flex;
  font-size: var(--fs-sm);
  font-weight: 650;
  gap: 6px;
  margin: 0;
}

.sk-mkt__featured-row {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
}

.sk-mkt__featured-item {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: inherit;
  cursor: pointer;
  display: flex;
  font: inherit;
  gap: 9px;
  min-width: 0;
  padding: 9px 11px;
  text-align: left;
  transition: border-color var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.sk-mkt__featured-item:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  transform: translateY(-1px);
}

.sk-mkt__featured-item:focus-visible {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-mkt__featured-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.sk-mkt__featured-body strong {
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sk-mkt__featured-meta {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  font-size: 10px;
  gap: 3px;
}

.sk-mkt__card-open {
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  display: flex;
  flex: 1;
  flex-direction: column;
  font: inherit;
  min-width: 0;
  padding: 0;
  text-align: left;
}

.sk-mkt__card-open:focus-visible {
  box-shadow: var(--focus-ring);
  border-radius: var(--radius-sm);
  outline: 0;
}

.sk-mkt__card-open strong {
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sk-mkt__card-source {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: .04em;
  text-transform: uppercase;
}

.sk-mkt__badge {
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 999px;
  display: inline-flex;
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 650;
  gap: 3px;
  padding: 2px 8px;
}

.sk-mkt__badge--ok {
  border-color: color-mix(in srgb, var(--ok) 45%, var(--border));
  color: var(--ok);
}

.sk-mkt__star {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  height: 28px;
  justify-content: center;
  padding: 4px;
}

.sk-mkt__star:hover {
  color: var(--text-muted);
}

.sk-mkt__star.is-active {
  color: var(--warn);
}

.sk-mkt__star:focus-visible {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-mkt__card-desc {
  color: var(--text-muted);
  display: -webkit-box;
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: 0;
  min-height: 2.9em;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.sk-mkt__card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.sk-mkt__tag {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  max-width: 100%;
  overflow: hidden;
  padding: 1px 6px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sk-mkt__tag--category {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--accent);
}

.sk-mkt__card-foot {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: space-between;
  margin-top: auto;
}

.sk-mkt__stats {
  align-items: center;
  display: inline-flex;
  gap: 10px;
  min-width: 0;
}

.sk-mkt__stat {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: 3px;
}

.sk-mkt__trust {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  text-transform: uppercase;
}

.sk-mkt__queue-status {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: 5px;
}

.sk-mkt__queue-status[data-status="installed"],
.sk-mkt__queue-status[data-status="unchanged"] {
  color: var(--ok);
}

.sk-mkt__queue-status[data-status="failed"],
.sk-mkt__queue-status[data-status="unknown"] {
  color: var(--danger);
}

.sk-mkt__card-foot .btn {
  align-items: center;
  display: inline-flex;
  gap: 5px;
}

.sk-mkt__empty {
  align-items: center;
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: center;
  min-height: 160px;
  padding: var(--sp-4);
  text-align: center;
}

@media (max-width: 720px) {
  .sk-mkt__search {
    min-width: 0;
    width: 100%;
  }

  .sk-mkt__header .control-stage__actions {
    width: 100%;
  }

  .sk-mkt__grid {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sk-mkt__card,
  .sk-mkt__featured-item {
    transition: none;
  }
}
</style>
