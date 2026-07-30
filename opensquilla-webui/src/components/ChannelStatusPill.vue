<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { statusPresentation } from '@/lib/channelStatus'

// The one component both channel surfaces render state through — a status
// word shown anywhere else is a vocabulary regression.
const props = defineProps<{
  status?: string | null
  enabled?: boolean | null
  connected?: boolean | null
  pendingRestart?: boolean
  errorClass?: string | null
  /** diagnostics recorded a startup failure (stopped → danger presentation). */
  startupFailed?: boolean
  /** Append the one-line last_error cause ("credentials rejected") when known. */
  showCause?: boolean
}>()

const { t } = useI18n()

const pres = computed(() => statusPresentation({
  status: props.status,
  enabled: props.enabled,
  connected: props.connected,
  pendingRestart: props.pendingRestart,
  errorClass: props.errorClass,
  startupFailed: props.startupFailed,
}))

const label = computed(() =>
  pres.value.key === 'unknown'
    ? t(pres.value.labelKey, { raw: pres.value.raw || '—' })
    : t(pres.value.labelKey),
)
const hint = computed(() => (pres.value.hintKey ? t(pres.value.hintKey) : ''))
const cause = computed(() =>
  props.showCause && pres.value.causeKey ? t(pres.value.causeKey) : '',
)
</script>

<template>
  <span class="chs" :class="`is-${pres.tone}`" :title="hint || undefined">
    <span class="chs__dot" aria-hidden="true"></span>
    <span>{{ label }}</span>
    <span v-if="cause" class="chs__cause">· {{ cause }}</span>
  </span>
</template>

<style scoped>
.chs { align-items: center; color: var(--text-muted); display: inline-flex; gap: 7px; white-space: nowrap; }
.chs__dot { background: currentColor; border-radius: 50%; flex: 0 0 auto; height: 8px; width: 8px; }
.chs.is-ok { color: var(--ok); }
.chs.is-info { color: var(--info); }
.chs.is-danger { color: var(--danger); }
.chs.is-muted { color: var(--text-dim); }
.chs__cause { color: var(--text-dim); font-size: 0.92em; white-space: normal; }
</style>
