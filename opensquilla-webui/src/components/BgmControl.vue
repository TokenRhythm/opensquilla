<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useBgm, BGM_LOCAL_TRACK_ID } from '@/composables/useBgm'

// Topbar background-music control: a split button next to the language/theme
// menus. The note button toggles play/pause of the current track; the caret
// opens a picker (reusing the global .theme-menu* classes so the three topbar
// popovers can never drift in look) with the playlist.json tracks, a volume
// slider, and a session-only "Choose local file…" escape hatch.

const { t } = useI18n()
const {
  tracks,
  playing,
  currentTrackId,
  currentTitle,
  volume,
  localTrackTitle,
  initBgm,
  toggle,
  selectTrack,
  setVolume,
  playLocalFile,
} = useBgm()

const menuOpen = ref(false)
const toggleRef = ref<HTMLButtonElement | null>(null)
const caretRef = ref<HTMLButtonElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
useDialogLayer(computed(() => menuOpen.value))

onMounted(() => { void initBgm() })

function onToggle() {
  void toggle()
}

function pickTrack(id: string) {
  void selectTrack(id)
  menuOpen.value = false
  toggleRef.value?.focus()
}

function onVolumeInput(e: Event) {
  const target = e.target as HTMLInputElement
  setVolume(Number(target.value))
}

function chooseLocalFile() {
  fileInputRef.value?.click()
}

function onLocalFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  // Clear so re-picking the same file fires change again.
  input.value = ''
  if (!file) return
  void playLocalFile(file)
  menuOpen.value = false
  toggleRef.value?.focus()
}

useDocumentEvent('click', (e) => {
  if (!menuOpen.value) return
  const wrap = caretRef.value?.closest('.bgm-menu-wrap')
  if (wrap && e.target instanceof Node && !wrap.contains(e.target)) {
    menuOpen.value = false
  }
})

useDocumentEvent('keydown', (e) => {
  if (e.key === 'Escape' && menuOpen.value) {
    menuOpen.value = false
    caretRef.value?.focus()
  }
})
</script>

<template>
  <div class="theme-menu-wrap bgm-menu-wrap">
    <button
      ref="toggleRef"
      type="button"
      class="btn btn--icon btn--ghost bgm-toggle"
      :class="{ 'is-playing': playing }"
      :title="playing ? t('chrome.bgm.pause') : t('chrome.bgm.play')"
      :aria-label="playing ? t('chrome.bgm.pause') : t('chrome.bgm.play')"
      :aria-pressed="playing"
      data-testid="bgm-toggle"
      @click="onToggle"
    >
      <Icon :name="playing ? 'pause' : 'music'" :size="16" />
    </button>
    <button
      ref="caretRef"
      type="button"
      class="btn btn--ghost bgm-caret"
      :title="t('chrome.bgm.label')"
      :aria-label="t('chrome.bgm.label')"
      aria-haspopup="menu"
      :aria-expanded="menuOpen"
      data-testid="bgm-menu-trigger"
      @click.stop="menuOpen = !menuOpen"
    >
      <Icon name="chevronDown" :size="12" />
    </button>
    <div v-if="menuOpen" class="theme-menu bgm-menu" role="menu" :aria-label="t('chrome.bgm.label')">
      <button
        v-for="track in tracks"
        :key="track.id"
        type="button"
        class="theme-menu__item"
        role="menuitemradio"
        :aria-checked="currentTrackId === track.id"
        :data-testid="`bgm-track-${track.id}`"
        @click="pickTrack(track.id)"
      >
        <Icon name="music" :size="14" />
        <span class="bgm-menu__title">{{ track.title }}</span>
        <Icon v-if="currentTrackId === track.id" class="theme-menu__check" name="check" :size="14" />
      </button>
      <!-- Session-only local pick: shown as a selectable row once a file has
           been chosen, so it can be toggled back to after a playlist track. -->
      <button
        v-if="localTrackTitle"
        type="button"
        class="theme-menu__item"
        role="menuitemradio"
        :aria-checked="currentTrackId === BGM_LOCAL_TRACK_ID"
        data-testid="bgm-track-local"
        @click="pickTrack(BGM_LOCAL_TRACK_ID)"
      >
        <Icon name="fileText" :size="14" />
        <span class="bgm-menu__title">{{ localTrackTitle }}</span>
        <Icon v-if="currentTrackId === BGM_LOCAL_TRACK_ID" class="theme-menu__check" name="check" :size="14" />
      </button>
      <div v-if="!tracks.length && !localTrackTitle" class="bgm-menu__empty">
        {{ t('chrome.bgm.noTracks') }}
      </div>
      <!-- Volume row: adjusting must not dismiss the menu. -->
      <div class="bgm-menu__volume" role="none" @click.stop>
        <Icon name="volume" :size="14" />
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          :value="volume"
          :aria-label="t('chrome.bgm.volume')"
          data-testid="bgm-volume"
          @input="onVolumeInput"
        />
      </div>
      <button
        type="button"
        class="theme-menu__item theme-menu__item--more"
        role="menuitem"
        data-testid="bgm-choose-local"
        @click="chooseLocalFile"
      >
        <Icon name="paperclip" :size="14" />
        <span>{{ t('chrome.bgm.chooseLocalFile') }}</span>
      </button>
      <input
        ref="fileInputRef"
        type="file"
        accept="audio/*"
        class="bgm-file-input"
        tabindex="-1"
        aria-hidden="true"
        @change="onLocalFilePicked"
      />
    </div>
    <!-- Screen-reader note of what is currently playing; visual users get the
         accent-lit note button instead. -->
    <span v-if="playing && currentTitle" class="bgm-sr-now-playing">{{ currentTitle }}</span>
  </div>
</template>

<style scoped>
.bgm-toggle {
  color: var(--text-muted);
}

.bgm-toggle.is-playing {
  color: var(--accent);
}

/* Narrow caret hugging the note button so the pair reads as one control. */
.bgm-caret {
  display: inline-flex;
  align-items: center;
  width: auto;
  padding: 0 2px;
  margin-left: -6px;
  color: var(--text-muted);
}

.bgm-caret:hover {
  color: var(--text);
}

.bgm-menu {
  min-width: 200px;
  max-width: 280px;
}

.bgm-menu__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bgm-menu__empty {
  padding: 7px 10px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.bgm-menu__volume {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 7px 10px;
  color: var(--text-muted);
}

.bgm-menu__volume input[type='range'] {
  flex: 1;
  min-width: 0;
  accent-color: var(--accent);
}

.bgm-file-input {
  display: none;
}

/* Off-screen but screen-reader-reachable "now playing" note. */
.bgm-sr-now-playing {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
</style>
