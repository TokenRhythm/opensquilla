<template>
  <span class="activity-motion" :class="{ 'activity-motion--active': active }">
    <slot />
  </span>
</template>

<script setup lang="ts">
defineProps<{ active?: boolean }>()
</script>

<style scoped>
.activity-motion {
  display: flex;
  align-items: center;
  gap: inherit;
  min-width: 0;
  flex: 1;
}

/* One mask moves through the glyphs of the entire row, without painting a bar. */
.activity-motion--active {
  color: var(--text);
  mask-image: linear-gradient(
    105deg,
    color-mix(in srgb, currentColor 72%, transparent) 40%,
    currentColor 50%,
    color-mix(in srgb, currentColor 72%, transparent) 60%
  );
  mask-size: 250% 100%;
  mask-repeat: no-repeat;
  animation: activity-row-shimmer 3s linear 250ms infinite;
}

:global([data-share-expanded="false"] [data-share-activity-body] .activity-motion--active) {
  animation: none;
  mask-image: none;
}

@keyframes activity-row-shimmer {
  from { mask-position: 100% 0; }
  65%, to { mask-position: 0 0; }
}

@media (prefers-reduced-motion: reduce) {
  .activity-motion--active {
    animation: none;
    mask-image: none;
  }
}
</style>
