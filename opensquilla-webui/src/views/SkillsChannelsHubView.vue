<template>
  <RouteHubShell
    :tabs="TABS"
    ariaLabelKey="nav.skillsChannels"
    :keep-alive-max="3"
    mobile-equal-tabs
  />
</template>

<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
import RouteHubShell from '@/components/RouteHubShell.vue'

const SkillsView = defineAsyncComponent(() => import('@/views/SkillsView.vue'))
const ChannelsView = defineAsyncComponent(() => import('@/views/ChannelsView.vue'))
const SkillsMarketView = defineAsyncComponent(() => import('@/views/SkillsMarketView.vue'))

const TABS = [
  { path: '/skills', labelKey: 'nav.skills', icon: 'skills', component: SkillsView },
  { path: '/market', labelKey: 'nav.market', icon: 'store', component: SkillsMarketView },
  { path: '/channels', labelKey: 'nav.channels', icon: 'channels', component: ChannelsView },
] as const
</script>
