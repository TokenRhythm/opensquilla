<template>
  <RouteHubShell
    :tabs="TABS"
    ariaLabelKey="nav.viewUsage"
    :keep-alive-max="2"
  >
    <template #actions>
      <SupportDiagnosticsMenu v-if="route.path === '/overview'" />
    </template>
  </RouteHubShell>
</template>

<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
import { useRoute } from 'vue-router'
import RouteHubShell from '@/components/RouteHubShell.vue'
import SupportDiagnosticsMenu from '@/components/SupportDiagnosticsMenu.vue'

const OverviewView = defineAsyncComponent(() => import('@/views/OverviewView.vue'))
const UsageView = defineAsyncComponent(() => import('@/views/UsageView.vue'))
const route = useRoute()

const TABS = [
  { path: '/usage', labelKey: 'nav.usage', icon: 'usage', component: UsageView },
  { path: '/overview', labelKey: 'nav.overview', icon: 'home', component: OverviewView },
] as const
</script>
