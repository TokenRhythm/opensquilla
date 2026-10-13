<template>
  <Teleport to="body">
    <Transition name="sk-mdlg">
      <div v-if="row" class="sk-mdlg" data-testid="market-detail-dialog">
        <div class="sk-mdlg__scrim" data-testid="market-detail-scrim" @click="emit('close')" />
        <section
          ref="panelRef"
          class="sk-mdlg__panel"
          role="dialog"
          aria-modal="true"
          :aria-label="row.name"
        >
          <header class="sk-mdlg__head">
            <span class="sk-mdlg__avatar" aria-hidden="true">
              <img
                v-if="row.icon_url && !iconFailed"
                :src="row.icon_url"
                alt=""
                referrerpolicy="no-referrer"
                @error="iconFailed = true"
              />
              <span v-else>{{ initials(row.name) }}</span>
            </span>
            <div class="sk-mdlg__title">
              <h2>{{ row.name }}</h2>
              <p>
                {{ sourceLabel(row.source) }}
                <template v-if="row.author"> · {{ row.author }}</template>
                <template v-if="row.version"> · {{ row.version }}</template>
              </p>
            </div>
            <button
              ref="closeButtonRef"
              class="btn btn--ghost sk-mdlg__close"
              type="button"
              :aria-label="t('common.close')"
              @click="emit('close')"
            >
              <Icon name="x" :size="18" />
            </button>
          </header>

          <div class="sk-mdlg__body">
            <p v-if="row.description" class="sk-mdlg__desc">{{ row.description }}</p>
            <p v-else class="sk-mdlg__desc sk-mdlg__desc--empty">{{ t('cronSkills.market.noDescription') }}</p>

            <div class="sk-mdlg__tags">
              <span v-if="row.category" class="sk-mdlg__tag sk-mdlg__tag--category">{{ row.category }}</span>
              <span v-for="tag in allTags" :key="tag" class="sk-mdlg__tag">{{ tag }}</span>
            </div>

            <dl class="sk-mdlg__facts">
              <div v-if="row.downloads">
                <dt><Icon name="download" :size="13" /> {{ t('cronSkills.market.downloads', { count: row.downloads }) }}</dt>
                <dd>{{ compactCount(row.downloads) }}</dd>
              </div>
              <div v-if="row.installs">
                <dt><Icon name="skills" :size="13" /> {{ t('cronSkills.market.installs', { count: row.installs }) }}</dt>
                <dd>{{ compactCount(row.installs) }}</dd>
              </div>
              <div v-if="row.license">
                <dt>{{ t('cronSkills.market.license') }}</dt>
                <dd>{{ row.license }}</dd>
              </div>
              <div>
                <dt>{{ t('cronSkills.market.trust') }}</dt>
                <dd>{{ row.trust_level || t('cronSkills.registry.community') }}</dd>
              </div>
              <div v-if="row.signature_status">
                <dt>{{ t('cronSkills.market.signature') }}</dt>
                <dd>{{ row.signature_status }}</dd>
              </div>
              <div v-if="identifier">
                <dt>{{ t('cronSkills.market.identifier') }}</dt>
                <dd><code :title="identifier">{{ identifier }}</code></dd>
              </div>
            </dl>

            <a
              v-if="linkHref"
              class="sk-mdlg__link"
              :href="linkHref"
              target="_blank"
              rel="noopener noreferrer"
              data-testid="market-detail-link"
            >
              <Icon name="externalLink" :size="14" />
              <span>{{ t('cronSkills.market.viewPage') }}</span>
            </a>
          </div>

          <footer class="sk-mdlg__foot">
            <button
              class="sk-mdlg__star"
              :class="{ 'is-active': favorited }"
              type="button"
              data-testid="market-detail-favorite"
              :aria-label="favorited
                ? t('cronSkills.market.unfavorite')
                : t('cronSkills.market.favorite')"
              :aria-pressed="favorited"
              @click="emit('toggle-favorite')"
            >
              <Icon name="star" :size="15" />
            </button>
            <span
              v-if="status"
              class="sk-mdlg__status"
              :data-status="status"
              role="status"
            >
              <span v-if="running" class="sk-spinner" aria-hidden="true" />
              <Icon v-else-if="status === 'installed' || status === 'unchanged'" name="check" :size="14" />
              <span>{{ statusLabel }}</span>
            </span>

            <button
              v-if="showRetry"
              class="btn"
              :class="requiresRisk ? 'btn--primary' : 'btn--ghost'"
              type="button"
              data-testid="market-detail-retry"
              :disabled="blocked"
              @click="emit('retry')"
            >
              <Icon name="refresh" :size="14" />
              <span>{{ requiresRisk ? t('cronSkills.registry.installAnyway') : t('cronSkills.registry.retry') }}</span>
            </button>
            <button
              v-else
              class="btn"
              :class="row.installed ? 'btn--ghost' : 'btn--primary'"
              type="button"
              data-testid="market-detail-install"
              :disabled="blocked || running || row.installed"
              :aria-busy="running"
              @click="emit('install')"
            >
              <Icon v-if="row.installed" name="check" :size="14" />
              <span>{{ installLabel }}</span>
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import type { RegistryResult } from '@/types/skills'

const props = defineProps<{
  row: RegistryResult | null
  status: string
  running: boolean
  blocked: boolean
  showRetry: boolean
  requiresRisk: boolean
  installLabel: string
  statusLabel: string
  favorited: boolean
}>()

const emit = defineEmits<{
  close: []
  install: []
  retry: []
  'toggle-favorite': []
}>()

const { t } = useI18n()
const panelRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLButtonElement | null>(null)
const iconFailed = ref(false)
const open = computed(() => props.row !== null)

useDialogA11y(panelRef, open, () => emit('close'), {
  initialFocus: closeButtonRef,
})

watch(
  () => props.row && props.row.installReference + props.row.name,
  () => { iconFailed.value = false },
)

const allTags = computed(() => {
  const tags = Array.isArray(props.row?.tags) ? props.row.tags : []
  return tags.filter((tag): tag is string => typeof tag === 'string' && tag.trim().length > 0)
})

const identifier = computed(() => {
  const row = props.row
  if (!row) return ''
  return row.installReference || row.install_reference || row.identifier || ''
})

const linkHref = computed(() => {
  const row = props.row
  if (!row) return ''
  const candidate = row.homepage || row.upstream_url || ''
  return /^https?:\/\//i.test(candidate) ? candidate : ''
})

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

function compactCount(value: number | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return ''
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`
  return String(value)
}
</script>

<style scoped>
.sk-mdlg {
  align-items: center;
  display: flex;
  inset: 0;
  justify-content: center;
  padding: 24px;
  position: fixed;
  z-index: 1200;
}

.sk-mdlg__scrim {
  background: color-mix(in srgb, var(--scrim) 45%, transparent);
  inset: 0;
  position: absolute;
}

.sk-mdlg__panel {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--elev-3);
  color: var(--text);
  display: flex;
  flex-direction: column;
  max-height: min(680px, calc(100dvh - 48px));
  max-width: 640px;
  overflow: hidden;
  position: relative;
  width: 100%;
}

.sk-mdlg__head {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 12px;
  padding: 18px 20px 14px;
}

.sk-mdlg__avatar {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 auto;
  font-size: 15px;
  font-weight: 700;
  height: 44px;
  justify-content: center;
  overflow: hidden;
  width: 44px;
}

.sk-mdlg__avatar img {
  height: 100%;
  object-fit: cover;
  width: 100%;
}

.sk-mdlg__title {
  min-width: 0;
}

.sk-mdlg__title h2 {
  font-size: 1.1rem;
  margin: 0;
  overflow-wrap: anywhere;
}

.sk-mdlg__title p {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  margin: 3px 0 0;
}

.sk-mdlg__close {
  flex: 0 0 auto;
  margin-left: auto;
  padding: 7px;
}

.sk-mdlg__body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  overflow-y: auto;
  padding: 16px 20px;
}

.sk-mdlg__desc {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.6;
  margin: 0;
  white-space: pre-wrap;
}

.sk-mdlg__desc--empty {
  color: var(--text-dim);
  font-style: italic;
}

.sk-mdlg__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sk-mdlg__tag {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 2px 7px;
}

.sk-mdlg__tag--category {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--accent);
}

.sk-mdlg__facts {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  margin: 0;
}

.sk-mdlg__facts > div {
  align-items: baseline;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  min-width: 0;
}

.sk-mdlg__facts dt {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: 4px;
  white-space: nowrap;
}

.sk-mdlg__facts dd {
  color: var(--text);
  font-size: var(--fs-xs);
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-align: right;
  text-overflow: ellipsis;
}

.sk-mdlg__facts code {
  font-family: var(--font-mono);
  font-size: 10px;
}

.sk-mdlg__link {
  align-items: center;
  color: var(--accent);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: 5px;
  text-decoration: none;
  width: fit-content;
}

.sk-mdlg__link:hover {
  text-decoration: underline;
}

.sk-mdlg__foot {
  align-items: center;
  border-top: 1px solid var(--border);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: flex-end;
  padding: 14px 20px;
}

.sk-mdlg__status {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: 6px;
  margin-right: auto;
}

.sk-mdlg__status[data-status="installed"],
.sk-mdlg__status[data-status="unchanged"] {
  color: var(--ok);
}

.sk-mdlg__status[data-status="failed"],
.sk-mdlg__status[data-status="unknown"] {
  color: var(--danger);
}

.sk-mdlg__foot .btn {
  align-items: center;
  display: inline-flex;
  gap: 5px;
  min-width: 130px;
  justify-content: center;
}

.sk-mdlg__star {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  height: 30px;
  justify-content: center;
  margin-right: auto;
  padding: 5px;
}

.sk-mdlg__star:hover {
  color: var(--text-muted);
}

.sk-mdlg__star.is-active {
  color: var(--warn);
}

.sk-mdlg__star:focus-visible {
  box-shadow: var(--focus-ring);
  outline: 0;
}

.sk-mdlg-enter-active,
.sk-mdlg-leave-active {
  transition: opacity var(--dur-base) var(--ease-standard),
    transform var(--dur-base) var(--ease-standard);
}

.sk-mdlg-enter-from,
.sk-mdlg-leave-to {
  opacity: 0;
}

.sk-mdlg-enter-from .sk-mdlg__panel,
.sk-mdlg-leave-to .sk-mdlg__panel {
  opacity: 0;
  transform: translateY(10px) scale(.98);
}

@media (max-width: 720px) {
  .sk-mdlg {
    align-items: flex-end;
    padding: 0;
  }

  .sk-mdlg__panel {
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    max-height: 92dvh;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sk-mdlg-enter-active,
  .sk-mdlg-leave-active {
    transition: none;
  }
}
</style>
