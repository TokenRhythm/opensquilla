<template>
  <RouteHubShell
    :tabs="TABS"
    ariaLabelKey="nav.overview"
    :keep-alive-max="2"
  >
    <template #actions>
      <SupportDiagnosticsMenu />
    </template>
  </RouteHubShell>
</template>

<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
import RouteHubShell from '@/components/RouteHubShell.vue'
import SupportDiagnosticsMenu from '@/components/SupportDiagnosticsMenu.vue'

const OverviewView = defineAsyncComponent(() => import('@/views/OverviewView.vue'))
const UsageView = defineAsyncComponent(() => import('@/views/UsageView.vue'))

const TABS = [
  { path: '/overview', labelKey: 'nav.status', icon: 'home', component: OverviewView },
  { path: '/usage', labelKey: 'nav.usage', icon: 'usage', component: UsageView },
] as const
</script>
