<script setup lang="ts">
import { computed } from 'vue'

// Brand-colored monogram tiles, deliberately NOT vendor logos: several of
// these platforms actively police glyph redistribution (Slack and Microsoft
// had their marks removed from open icon sets on request), so shipping real
// logos in an open repository is the one option with genuine legal surface.
// A characteristic color plus a letter identifies the platform just as fast
// and reproduces nobody's registered mark.
const MARKS: Record<string, { label: string; bg: string; fg?: string }> = {
  dingtalk: { label: '钉', bg: 'var(--brand-dingtalk)' },
  discord: { label: 'D', bg: 'var(--brand-discord)' },
  feishu: { label: '飞', bg: 'var(--brand-feishu)' },
  matrix: { label: 'M', bg: 'var(--brand-matrix)' },
  msteams: { label: 'MT', bg: 'var(--brand-msteams)' },
  qq: { label: 'QQ', bg: 'var(--brand-qq)' },
  slack: { label: 'S', bg: 'var(--brand-slack)' },
  telegram: { label: 'T', bg: 'var(--brand-telegram)' },
  wecom: { label: '企', bg: 'var(--brand-wecom)' },
}

const props = defineProps<{ type: string; label?: string }>()

const mark = computed(() => {
  const known = MARKS[props.type]
  if (known) return known
  // Unknown/custom channel types get a neutral tile with their initial.
  const initial = (props.label || props.type || '?').trim().charAt(0).toUpperCase()
  return { label: initial || '?', bg: 'var(--bg-elevated)', fg: 'var(--text-muted)' }
})
</script>

<template>
  <span
    class="brand-mark"
    :style="{ background: mark.bg, color: mark.fg || 'var(--brand-mark-fg)' }"
    aria-hidden="true"
  >{{ mark.label }}</span>
</template>

<style scoped>
.brand-mark {
  align-items: center;
  border-radius: var(--radius-md);
  display: inline-flex;
  flex: none;
  font-size: 15px;
  font-weight: 700;
  height: 34px;
  justify-content: center;
  letter-spacing: -0.02em;
  line-height: 1;
  user-select: none;
  width: 34px;
}
</style>
