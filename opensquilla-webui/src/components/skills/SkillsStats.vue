<template>
  <section class="sk-stats control-stat-grid" style="--control-stat-min: 160px">
    <button
      v-for="tile in tiles"
      :key="tile.key"
      class="sk-stat control-stat control-stat--clickable"
      :class="[tile.mods, {
        'is-active': activeKey === tile.key,
        'control-stat--accent': tile.mods.includes('sk-stat--accent') || activeKey === tile.key,
        'control-stat--hero': activeKey === tile.key,
      }]"
      type="button"
      @click="emit('select', tile.key)"
    >
      <div class="sk-stat__label control-stat__label">{{ tile.label }}</div>
      <div class="sk-stat__value control-stat__value">
        <span v-if="tile.tone" :class="tile.tone">{{ tile.value }}</span>
        <template v-else>{{ tile.value }}</template>
      </div>
      <div class="sk-stat__hint control-stat__hint">{{ tile.hint }}</div>
    </button>
  </section>
</template>

<script setup lang="ts">
export interface SkillStatTile {
  key: string
  label: string
  value: string
  hint: string
  mods: string
  tone?: string
}

defineProps<{
  tiles: SkillStatTile[]
  activeKey: string
}>()

const emit = defineEmits<{
  select: [key: string]
}>()
</script>

<style scoped>
.sk-stat__ok {
  color: var(--ok);
}

.sk-stat__warn {
  color: var(--warn);
}

</style>
