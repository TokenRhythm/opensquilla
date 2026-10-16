<script setup lang="ts">
import { computed } from 'vue'
import Icon from '@/components/Icon.vue'
import vscode from '@/assets/workspace-apps/vscode.svg'
import sublime from '@/assets/workspace-apps/sublime.svg'

const props = defineProps<{ appId: string }>()
// Only bundled, attributed artwork can become an image or mask URL. An app ID
// from the Gateway never selects an arbitrary path or remote image resource.
const brands: Record<string, { src?: string; monochrome?: boolean }> = {
  vscode: { src: vscode },
  cursor: { monochrome: true },
  windsurf: { monochrome: true },
  sublime: { src: sublime },
  zed: { monochrome: true },
}
const brand = computed(() => Object.prototype.hasOwnProperty.call(brands, props.appId) ? brands[props.appId] : undefined)
</script>

<template>
  <span class="workspace-application-icon" :data-application-icon="brand ? appId : appId === 'reveal' ? 'folder' : 'externalLink'" aria-hidden="true">
    <span v-if="brand?.monochrome" class="workspace-application-icon__monochrome" :class="`workspace-application-icon__monochrome--${appId}`" />
    <img v-else-if="brand" class="workspace-application-icon__image" :src="brand.src" alt="" draggable="false" />
    <Icon v-else :name="appId === 'reveal' ? 'folder' : 'externalLink'" :size="18" />
  </span>
</template>

<style scoped>
.workspace-application-icon { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; flex: 0 0 20px; color: var(--text); line-height: 1; vertical-align: middle; }
.workspace-application-icon__image, .workspace-application-icon__monochrome { display: block; width: 18px; height: 18px; flex: 0 0 18px; }
.workspace-application-icon__image { object-fit: contain; }
.workspace-application-icon__monochrome { background-color: currentColor; mask-repeat: no-repeat; mask-position: center; mask-size: contain; -webkit-mask-repeat: no-repeat; -webkit-mask-position: center; -webkit-mask-size: contain; }
/* Let Vite resolve static mask URLs for both browser implementations.
   Separate assets avoid SVG data-URL quoting in dynamic style declarations. */
.workspace-application-icon__monochrome--cursor { -webkit-mask-image: url('../../assets/workspace-apps/cursor.svg?no-inline'); mask-image: url('../../assets/workspace-apps/cursor.svg?no-inline'); }
.workspace-application-icon__monochrome--windsurf { -webkit-mask-image: url('../../assets/workspace-apps/windsurf.svg?no-inline'); mask-image: url('../../assets/workspace-apps/windsurf.svg?no-inline'); }
.workspace-application-icon__monochrome--zed { -webkit-mask-image: url('../../assets/workspace-apps/zed.svg?no-inline'); mask-image: url('../../assets/workspace-apps/zed.svg?no-inline'); }
</style>
