<script setup lang="ts">
// Marketplace-style skill tile: icon + name + one-line (locale-aware)
// description + a top-right action. Two variants share one look:
//   installed — the whole tile is a button that opens the read-only detail
//               dialog; the corner shows a status dot + an "installed" badge.
//   registry  — a static tile whose corner "+" button emits `install`
//               (disabled/"installed" when the skill is already present).
// The dense dependency/sub-skill rows from the old SkillCard live in the
// detail dialog now; the tile stays recognition-first (see ChannelTypeGallery).
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  localizedSkillDescription,
  type SkillLifecycleTone,
} from '@/composables/skills/useSkillsCatalog'
import type { IconName } from '@/utils/icons'
import { assignedFallbackIcon } from '@/utils/skillIcons'

const props = defineProps<{
  name: string
  description?: string
  descriptionZh?: string
  emoji?: string
  variant: 'installed' | 'registry'
  installed?: boolean
  busy?: boolean
  meta?: boolean
  source?: string
  trustLevel?: string
  lifecycleLabel?: string
  lifecycleTone?: SkillLifecycleTone
  statusDotClass?: string
  statusDotTitle?: string
}>()

const emit = defineEmits<{
  open: []
  install: []
}>()

const { t, locale } = useI18n()

const desc = computed(() =>
  localizedSkillDescription(
    { description: props.description, description_zh: props.descriptionZh },
    String(locale.value),
  ),
)

// Letter-avatar fallback when a skill has no emoji: first character of the
// name, tinted with one of a few theme hues so a wall of emoji-less skills
// reads as varied rather than a uniform grey block. The hue is a stable hash
// of the name (deterministic — no Math.random), and every colour is a
// color-mix of existing theme tokens so it adapts per theme and satisfies the
// raw-colour-literal guard.
const fallbackIcon = computed<IconName>(() => assignedFallbackIcon(props.name || 'skill'))
</script>

<template>
  <component
    :is="variant === 'installed' ? 'button' : 'div'"
    :type="variant === 'installed' ? 'button' : undefined"
    class="sk-tile"
    :class="{ 'sk-tile--interactive': variant === 'installed', 'sk-tile--meta': meta }"
    :title="name + (desc ? ': ' + desc : '')"
    @click="variant === 'installed' ? emit('open') : undefined"
  >
    <span class="sk-tile__icon" aria-hidden="true">
      <span class="sk-tile__avatar" :data-icon="fallbackIcon"><Icon :name="fallbackIcon" :size="24" /></span>
    </span>

    <span class="sk-tile__body">
      <span class="sk-tile__name" :title="name">{{ name }}</span>
      <span class="sk-tile__desc">{{ desc }}</span>
      <span
        v-if="variant === 'registry' && (source || trustLevel || lifecycleLabel)"
        class="sk-tile__registry-meta"
      >
        <span v-if="source" class="sk-tile__source">{{ source }}</span>
        <span
          v-if="trustLevel"
          class="sk-tile__trust"
          :class="{ 'is-trusted': trustLevel === 'trusted' || trustLevel === 'builtin' }"
        >{{ trustLevel }}</span>
        <span
          v-if="lifecycleLabel"
          class="sk-tile__lifecycle"
          :data-tone="lifecycleTone || 'neutral'"
        >{{ lifecycleLabel }}</span>
      </span>
      <span
        v-if="variant === 'installed' && lifecycleLabel"
        class="sk-tile__lifecycle sk-tile__lifecycle--installed"
        :data-tone="lifecycleTone || 'neutral'"
      >{{ lifecycleLabel }}</span>
    </span>

    <span class="sk-tile__action">
      <template v-if="variant === 'installed'">
        <span
          v-if="statusDotClass"
          class="sk-tile__dot"
          :class="statusDotClass"
          :title="statusDotTitle"
        />
      </template>
      <template v-else>
        <button
          v-if="installed"
          type="button"
          class="sk-tile__add sk-tile__add--done"
          disabled
        >{{ t('cronSkills.tile.installed') }}</button>
        <button
          v-else
          type="button"
          class="sk-tile__add"
          :disabled="busy"
          :title="t('cronSkills.tile.add')"
          :aria-label="t('cronSkills.tile.add')"
          @click.stop="emit('install')"
        >
          <span v-if="busy" class="sk-spinner sk-tile__spinner" />
          <Icon v-else name="plus" :size="16" />
        </button>
      </template>
    </span>
  </component>
</template>

<style scoped>
.sk-tile {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: 0 1px 2px color-mix(in srgb, var(--text) 5%, transparent);
  display: grid;
  font: inherit;
  gap: var(--sp-3);
  grid-template-columns: auto 1fr auto;
  min-height: 92px;
  padding: 14px 15px;
  text-align: left;
  transition: background var(--dur-fast) var(--ease-standard),
    border-color var(--dur-fast) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
  width: 100%;
}
.sk-tile--interactive { cursor: pointer; }
.sk-tile--interactive:hover {
  background: var(--bg-elevated);
  border-color: var(--border-strong);
  box-shadow: 0 10px 26px color-mix(in srgb, var(--text) 9%, transparent);
  transform: translateY(-2px);
}
.sk-tile--interactive:focus-visible { box-shadow: var(--focus-ring); outline: 0; }

.sk-tile__icon {
  display: inline-flex;
  position: relative;
}
.sk-tile__emoji,
.sk-tile__avatar {
  align-items: center;
  border: 1px solid color-mix(in srgb, currentColor 9%, transparent);
  border-radius: var(--radius-md);
  display: inline-flex;
  font-size: 20px;
  height: 44px;
  justify-content: center;
  width: 44px;
}
.sk-tile__avatar {
  background: var(--bg-surface-2, var(--bg-elevated));
  color: var(--text);
  font-size: 17px;
  font-weight: 750;
}
/* Tinted letter-avatar palette — soft hue background + hue-dominant glyph,
   blended toward --text so contrast holds across every theme. */
.sk-tile__avatar--0 {
  background: color-mix(in srgb, var(--accent) 20%, var(--bg-surface));
  color: color-mix(in srgb, var(--accent) 85%, var(--text));
}
.sk-tile__avatar--1 {
  background: color-mix(in srgb, var(--ok) 20%, var(--bg-surface));
  color: color-mix(in srgb, var(--ok) 80%, var(--text));
}
.sk-tile__avatar--2 {
  background: color-mix(in srgb, var(--info) 20%, var(--bg-surface));
  color: color-mix(in srgb, var(--info) 85%, var(--text));
}
.sk-tile__avatar--3 {
  background: color-mix(in srgb, var(--warn) 22%, var(--bg-surface));
  color: color-mix(in srgb, var(--warn) 78%, var(--text));
}
.sk-tile__avatar--4 {
  background: color-mix(in srgb, var(--queued) 20%, var(--bg-surface));
  color: color-mix(in srgb, var(--queued) 85%, var(--text));
}

.sk-tile__body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.sk-tile__registry-meta {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.sk-tile__source,
.sk-tile__trust,
.sk-tile__lifecycle {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 18px;
  padding: 0 6px;
}
.sk-tile__lifecycle--installed {
  align-self: flex-start;
  margin-top: 2px;
  width: fit-content;
}
.sk-tile__lifecycle {
  --sk-lifecycle-tone: var(--text-dim);
  background: color-mix(in srgb, var(--sk-lifecycle-tone) 8%, transparent);
  border-color: color-mix(in srgb, var(--sk-lifecycle-tone) 38%, var(--border));
  color: var(--sk-lifecycle-tone);
}
.sk-tile__lifecycle[data-tone='success'] { --sk-lifecycle-tone: var(--ok); }
.sk-tile__lifecycle[data-tone='info'] { --sk-lifecycle-tone: var(--info); }
.sk-tile__lifecycle[data-tone='warning'] { --sk-lifecycle-tone: var(--warn); }
.sk-tile__lifecycle[data-tone='danger'] { --sk-lifecycle-tone: var(--danger); }
.sk-tile__trust {
  border-color: color-mix(in srgb, var(--warn) 40%, var(--border));
  color: var(--warn);
}
.sk-tile__trust.is-trusted {
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}
.sk-tile__name {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sk-tile__desc {
  color: var(--text-dim);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  font-size: 12px;
  line-height: 1.7;
  /* Reserve two lines so every tile is the same taller height, even when a
     description is short. */
  min-height: 3.4em;
  overflow: hidden;
}

.sk-tile__action { align-items: center; display: inline-flex; flex: none; gap: var(--sp-2); }
.sk-tile__dot { border-radius: 50%; height: 8px; width: 8px; }
.sk-tile__dot.is-ready { background: var(--ok); }
.sk-tile__dot.is-needs { background: var(--warn-fill); }
.sk-tile__dot.is-unverified { background: var(--text-dim); }

.sk-tile__add {
  align-items: center;
  background: var(--bg-surface-2, var(--bg-elevated));
  border: 1px solid var(--border);
  border-radius: 50%;
  color: var(--text);
  cursor: pointer;
  display: inline-flex;
  height: 30px;
  justify-content: center;
  width: 30px;
}
.sk-tile__add:hover:not(:disabled) { background: var(--accent, var(--bg-elevated)); border-color: var(--border-strong, var(--border)); }
.sk-tile__add:focus-visible { box-shadow: var(--focus-ring); outline: 0; }
.sk-tile__add:disabled { cursor: default; opacity: 0.6; }
.sk-tile__add--done { border-radius: var(--radius-sm); font-size: 11px; width: auto; padding: 0 var(--sp-2); }
.sk-tile__spinner { height: 14px; width: 14px; }

.sk-tile--meta {
  background:
    radial-gradient(circle at 0 0, color-mix(in srgb, var(--accent) 9%, transparent), transparent 44%),
    var(--bg-surface);
  border-color: color-mix(in srgb, var(--accent) 24%, var(--border));
}

.sk-tile--meta:hover {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  box-shadow: 0 12px 28px color-mix(in srgb, var(--accent) 11%, transparent);
}

.sk-tile__meta-mark {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  border-radius: 999px;
  bottom: -4px;
  color: var(--accent);
  display: inline-flex;
  font-size: 9px;
  height: 17px;
  justify-content: center;
  position: absolute;
  right: -4px;
  width: 17px;
}

.sk-tile__dot {
  box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 10%, transparent);
}

/* Quiet, recognition-first card treatment inspired by WorkBuddy. */
.sk-tile {
  background: color-mix(in srgb, var(--bg-elevated) 64%, var(--bg-surface));
  border-color: transparent;
  border-radius: var(--radius-lg);
  box-shadow: none;
  gap: 12px;
  min-height: 126px;
  padding: 20px;
}
.sk-tile--interactive:hover {
  background: var(--bg-elevated);
  border-color: color-mix(in srgb, var(--border) 70%, transparent);
  box-shadow: 0 8px 22px color-mix(in srgb, var(--text) 7%, transparent);
  transform: translateY(-1px);
}
.sk-tile__emoji,
.sk-tile__avatar {
  border: 0;
  border-radius: var(--radius-md);
  font-size: 15px;
  height: 36px;
  width: 36px;
}
.sk-tile__emoji { font-size: 18px; }
.sk-tile__body { gap: 9px; }
.sk-tile__name {
  font-size: 13px;
  font-weight: 650;
}
.sk-tile__desc {
  -webkit-line-clamp: 2;
  line-clamp: 2;
  line-height: 1.7;
  min-height: 3.4em;
  white-space: normal;
}
.sk-tile--meta {
  background: color-mix(in srgb, var(--accent) 4%, var(--bg-elevated));
  border-color: transparent;
}
.sk-tile--meta:hover {
  border-color: color-mix(in srgb, var(--accent) 22%, var(--border));
  box-shadow: 0 8px 22px color-mix(in srgb, var(--accent) 7%, transparent);
}
.sk-tile__meta-mark {
  bottom: -3px;
  height: 15px;
  right: -3px;
  width: 15px;
}

/* Meta and built-in skills share one card treatment; grouping carries meaning. */
.sk-tile--meta {
  background: color-mix(in srgb, var(--bg-elevated) 64%, var(--bg-surface));
  border-color: transparent;
}
.sk-tile--meta:hover {
  background: var(--bg-elevated);
  border-color: color-mix(in srgb, var(--border) 70%, transparent);
  box-shadow: 0 8px 22px color-mix(in srgb, var(--text) 7%, transparent);
}

/* One visual system, distinct glyphs: colour no longer varies by name hash. */
.sk-tile__avatar,
.sk-tile__avatar--0,
.sk-tile__avatar--1,
.sk-tile__avatar--2,
.sk-tile__avatar--3,
.sk-tile__avatar--4 {
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  color: color-mix(in srgb, var(--accent) 68%, var(--text));
}

/* Direct glyph treatment: no avatar tile, like WorkBuddy's skill list. */
.sk-tile__icon {
  align-items: center;
  justify-content: center;
  width: 28px;
}
.sk-tile__emoji,
.sk-tile__avatar,
.sk-tile__avatar--0,
.sk-tile__avatar--1,
.sk-tile__avatar--2,
.sk-tile__avatar--3,
.sk-tile__avatar--4 {
  background: transparent;
  border: 0;
  border-radius: 0;
  height: 28px;
  width: 28px;
}
.sk-tile__emoji { font-size: 20px; }
.sk-tile__avatar--0 { color: var(--accent); }
.sk-tile__avatar--1 { color: var(--ok); }
.sk-tile__avatar--2 { color: var(--info); }
.sk-tile__avatar--3 { color: var(--warn); }
.sk-tile__avatar--4 { color: var(--queued); }
.sk-tile__avatar svg {
  height: 20px;
  width: 20px;
}

/* Render glyphs at a larger native size to avoid soft scaling. */
.sk-tile__icon {
  height: 32px;
  width: 32px;
}
.sk-tile__emoji,
.sk-tile__avatar,
.sk-tile__avatar--0,
.sk-tile__avatar--1,
.sk-tile__avatar--2,
.sk-tile__avatar--3,
.sk-tile__avatar--4 {
  height: 32px;
  width: 32px;
}
.sk-tile__emoji {
  font-size: 24px;
  line-height: 1;
}
.sk-tile__avatar svg {
  height: 24px;
  shape-rendering: geometricPrecision;
  stroke-width: 1.8;
  width: 24px;
}

/* Fully unified skill glyphs: SVG only, one size, colour and stroke style. */
.sk-tile__avatar,
.sk-tile__avatar--0,
.sk-tile__avatar--1,
.sk-tile__avatar--2,
.sk-tile__avatar--3,
.sk-tile__avatar--4 {
  background: transparent;
  color: var(--accent);
}

/* Normalize every skill glyph onto the same optical canvas. Icon.vue renders
   an inline wrapper around the SVG; sizing both layers removes baseline drift
   and prevents individual glyphs from inheriting an uneven flex size. */
.sk-tile__icon {
  align-self: center;
  display: grid;
  flex: 0 0 32px;
  height: 32px;
  line-height: 0;
  place-items: center;
  width: 32px;
}
.sk-tile__avatar {
  display: grid;
  height: 32px;
  line-height: 0;
  place-items: center;
  width: 32px;
}
.sk-tile__avatar :deep(.icon) {
  display: grid;
  height: 24px;
  line-height: 0;
  place-items: center;
  width: 24px;
}
.sk-tile__avatar :deep(svg) {
  display: block;
  height: 24px !important;
  margin: 0;
  max-height: none;
  max-width: none;
  transform: none;
  width: 24px !important;
}

/* One text rhythm for every skill card. */
.sk-tile {
  align-items: center;
  font-family: var(--font-sans);
}
.sk-tile__body {
  gap: 8px;
  justify-content: center;
}
.sk-tile__name {
  font-family: inherit;
  font-size: 13px;
  font-weight: 650;
  line-height: 20px;
}
.sk-tile__desc {
  font-family: inherit;
  font-size: 13px;
  line-height: 26px;
  min-height: 52px;
}
.sk-tile__action {
  align-self: center;
  height: 24px;
  justify-content: center;
  line-height: 0;
  min-width: 12px;
}
.sk-tile__dot {
  flex: 0 0 8px;
}

</style>
