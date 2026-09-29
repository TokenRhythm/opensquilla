<template>
  <div v-if="entries" class="trace-value__fields">
    <template v-for="[key, item] in entries.slice(0, visibleCount)" :key="key">
      <details v-if="isObject(item)" class="trace-value__branch" @toggle="setOpen(key, $event)">
        <summary><span>{{ entryLabel(key, item) }}</span><small>{{ t(`${ns}.items`, { count: Object.keys(item).length }) }}</small></summary>
        <TraceValue v-if="openKeys.has(key)" :value="item" />
      </details>
      <div v-else class="trace-value__field">
        <span class="trace-value__key">{{ key }}</span>
        <TraceValue :value="item" />
      </div>
    </template>
    <button v-if="entries.length > visibleCount" type="button" class="trace-value__more" @click="visibleCount += 30">{{ t(`${ns}.expand`) }} · {{ t(`${ns}.items`, { count: entries.length - visibleCount }) }}</button>
    <span v-if="!entries.length" class="trace-value__empty">{{ t(`${ns}.emptyValue`) }}</span>
  </div>
  <span v-else-if="value == null || value === ''" class="trace-value__empty">{{ t(`${ns}.emptyValue`) }}</span>
  <span v-else-if="typeof value === 'boolean'" class="trace-value__boolean">{{ String(value) }}</span>
  <pre v-else class="trace-value__text">{{ String(value) }}</pre>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ value: unknown }>()
const { t } = useI18n()
const ns = 'usageLogs.logs.traceInspectorView'
const visibleCount = ref(30)
const openKeys = ref(new Set<string>())
const entries = computed(() => isObject(props.value) ? Object.entries(props.value) : null)
function isObject(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' }
function setOpen(key: string, event: Event) {
  const next = new Set(openKeys.value)
  if ((event.target as HTMLDetailsElement).open) next.add(key)
  else next.delete(key)
  openKeys.value = next
}
function entryLabel(key: string, item: Record<string, unknown>): string {
  if (!Array.isArray(props.value)) return key
  const name = item.name ?? item.title ?? item.path ?? item.role
  return name == null ? String(Number(key) + 1) : `${Number(key) + 1} · ${String(name)}`
}
</script>

<style scoped>
.trace-value__fields { min-width: 0; }
.trace-value__field { display: grid; gap: 5px 12px; grid-template-columns: minmax(76px, .3fr) minmax(0, 1fr); padding: 8px 0; border-bottom: 1px solid var(--border); }
.trace-value__field:last-child { border-bottom: 0; }
.trace-value__key { color: var(--text-muted); font: 11px/1.6 var(--font-mono, monospace); overflow-wrap: anywhere; }
.trace-value__text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.65 var(--font-mono, monospace); color: var(--text); }
.trace-value__empty { color: var(--text-dim); font-size: 12px; }
.trace-value__boolean { color: var(--info); font: 12px/1.6 var(--font-mono, monospace); }
.trace-value__branch { border-bottom: 1px solid var(--border); padding: 8px 0; min-width: 0; }
.trace-value__branch > summary { cursor: pointer; font-size: 12px; color: var(--text); overflow-wrap: anywhere; }
.trace-value__branch > summary small { color: var(--text-dim); margin-left: 10px; font-size: 10px; }
.trace-value__branch > .trace-value__fields { padding: 3px 0 0 12px; }
.trace-value__more { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 12px; padding: 8px 0; }
@media (max-width: 520px) { .trace-value__field { grid-template-columns: minmax(0, 1fr); } }
</style>
