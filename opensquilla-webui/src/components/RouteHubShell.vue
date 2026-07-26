<template>
  <div class="route-hub" :class="{ 'route-hub--mobile-equal': mobileEqualTabs }">
    <div class="route-hub__bar">
      <nav class="route-hub__tabs" :aria-label="t(ariaLabelKey)">
        <RouterLink
          v-for="tab in tabs"
          :key="tab.path"
          :to="tab.path"
          class="route-hub__tab"
          :class="{ 'is-active': isActive(tab.path) }"
          :aria-current="isActive(tab.path) ? 'page' : undefined"
        >
          <Icon :name="tab.icon" :size="14" aria-hidden="true" />
          <span>{{ t(tab.labelKey) }}</span>
        </RouterLink>
      </nav>
      <div v-if="$slots.actions" class="route-hub__actions">
        <slot name="actions" />
      </div>
    </div>

    <div class="route-hub__panel">
      <KeepAlive :max="keepAliveMax">
        <component
          :is="activeTab?.component"
          v-if="activeTab"
          :key="activeTab.path"
        />
      </KeepAlive>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { IconName } from '@/utils/icons'

interface RouteHubTab {
  path: string
  labelKey: string
  icon: IconName
  component: Component
}

const props = defineProps<{
  tabs: readonly RouteHubTab[]
  ariaLabelKey: string
  keepAliveMax: number
  mobileEqualTabs?: boolean
}>()

const route = useRoute()
const { t } = useI18n()

// A route update reaches a kept-alive hub just before it is deactivated. Keep
// the last child selected when the destination is outside this hub so leaving
// cannot briefly mount or activate the first tab as a fallback.
const initialTab = props.tabs.find(tab => tab.path === route.path) ?? props.tabs[0]
const activePath = ref(initialTab?.path ?? '')

watch(
  () => route.path,
  (path) => {
    if (props.tabs.some(tab => tab.path === path)) activePath.value = path
  },
  { flush: 'sync' },
)

const activeTab = computed(() => (
  props.tabs.find(tab => tab.path === activePath.value) ?? props.tabs[0]
))

function isActive(path: string): boolean {
  return route.path === path
}
</script>

<style scoped>
.route-hub {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  position: relative;
}

.route-hub__bar {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  min-width: 0;
}

.route-hub__tabs {
  display: flex;
  gap: 2px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  padding: 3px;
  background: var(--bg-surface-2);
  border-radius: var(--radius-control);
  overflow-x: auto;
  scrollbar-width: none;
}

.route-hub__tabs::-webkit-scrollbar {
  display: none;
}

.route-hub__tab {
  align-items: center;
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  font-size: var(--fs-sm);
  font-weight: 600;
  gap: 7px;
  padding: 6px 14px;
  text-decoration: none;
  white-space: nowrap;
}

.route-hub__tab:hover {
  color: var(--text);
}

.route-hub__tab.is-active {
  background: var(--bg-surface);
  box-shadow: var(--elev-1);
  color: var(--text);
}

.route-hub__tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.route-hub__actions {
  flex: 0 0 auto;
}

/* The hub owns page identity, so hosted views keep their actions without
   repeating an H1 directly below the route tabs. */
.route-hub :deep(.control-stage__title-block) {
  display: none;
}

.route-hub :deep(.control-stage__header) {
  justify-content: flex-end;
}

@media (min-width: 769px) {
  .route-hub {
    padding-top: calc(36px + var(--sp-2));
  }

  .route-hub :deep(.control-stage__header) {
    padding-top: var(--sp-2);
  }

  /* A hosted view can opt its compact action-only header into the hub bar.
     Removing it from the child stage flow avoids a blank second toolbar row,
     while the hub's desktop top padding continues to clear the app topbar. */
  .route-hub :deep(.control-stage--hub-actions) {
    position: static;
  }

  .route-hub :deep(.control-stage__header--hub-actions) {
    align-items: center;
    height: 39px;
    padding-top: 0;
    position: absolute;
    right: 0;
    top: calc(36px + var(--sp-2));
  }
}

@media (max-width: 768px) {
  .route-hub__bar {
    align-items: center;
  }

  .route-hub__tab {
    min-height: 44px;
  }

  .route-hub--mobile-equal .route-hub__tabs {
    width: 100%;
  }

  .route-hub--mobile-equal .route-hub__tab {
    flex: 1 1 0;
    justify-content: center;
    min-width: 0;
  }
}
</style>
