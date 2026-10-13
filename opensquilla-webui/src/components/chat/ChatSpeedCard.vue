<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, unref, watch, type MaybeRef } from 'vue'
import type { ChatSpeedSnapshot } from '@/composables/chat/useChatSpeedTracker'

interface Props {
  /**
   * Live speed snapshot. The parent passes the tracker's `snapshot` computed
   * (a `MaybeRef`); every read below goes through `unref` so the same code
   * works for either a ref or a plain object.
   */
  snapshot: MaybeRef<ChatSpeedSnapshot>
  /** Hide the card entirely while no round has started. */
  hasStarted?: boolean
  /** Optional model label to show in the header. */
  model?: string
}

const props = defineProps<Props>()
/** Normalized, reactive view of the snapshot whether or not it was passed as a ref. */
const snap = () => unref(props.snapshot)
// Collapsed by default so the card stays out of the way of the transcript and
// only expands when the reader wants the detail. Persists across rounds.
const collapsed = ref(true)

// Wave history of the last ~N live-rate samples, pushed on each snapshot read.
const waveHistory = ref<number[]>([])
let sampleTimer: ReturnType<typeof setInterval> | null = null
const SAMPLE_MS = 250

onMounted(() => {
  sampleTimer = setInterval(() => {
    const s = snap()
    waveHistory.value = [...waveHistory.value, s.live].slice(-120)
  }, SAMPLE_MS)
})
onBeforeUnmount(() => {
  if (sampleTimer) clearInterval(sampleTimer)
})

watch(
  snap,
  (s) => {
    if (s.running && waveHistory.value.length === 0) {
      waveHistory.value = [s.live]
    }
  },
  { immediate: true },
)

const running = computed(() => snap().running)

const liveRate = computed(() => fmtRate(snap().live))
const avgRate = computed(() => fmtRate(snap().avg))
const peakRate = computed(() => fmtRate(snap().peak))
const totalTokens = computed(() => fmtTotal(snap().total))
const ttftText = computed(() => fmtMs(snap().ttftMs))
const roundText = computed(() => fmtMs(snap().roundMs || Date.now()))
const statusText = computed(() => {
  const s = snap()
  if (!s.running && s.total === 0) return 'idle'
  if (s.running) return `streaming · ${fmtMs(s.roundMs || 0)}`
  return `done · ${fmtMs(s.roundMs)}`
})

const wavePoints = computed(() => {
  const data = waveHistory.value
  if (data.length < 2) return ''
  const W = 320
  const H = 56
  const max = Math.max(...data, 1)
  const step = W / (data.length - 1)
  return data
    .map((v, i) => `${(i * step).toFixed(2)},${(H - (v / max) * (H - 8) - 4).toFixed(2)}`)
    .join(' ')
})

const wavePath = computed(() => {
  const data = waveHistory.value
  if (data.length < 2) return ''
  const W = 320
  const H = 56
  const max = Math.max(...data, 1)
  const step = W / (data.length - 1)
  let d = ''
  for (let i = 0; i < data.length; i++) {
    const x = (i * step).toFixed(2)
    const y = (H - (data[i] / max) * (H - 8) - 4).toFixed(2)
    d += (i === 0 ? `M${x},${y}` : ` L${x},${y}`)
  }
  return `${d} L${((data.length - 1) * step).toFixed(2)},${H} L0,${H} Z`
})

function fmtRate(n: number): string {
  if (!isFinite(n) || n <= 0) return '--'
  return n >= 100 ? n.toFixed(0) : n.toFixed(1)
}
function fmtTotal(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
function fmtMs(ms: number | null): string {
  if (ms === null || ms === undefined || !isFinite(ms) || ms <= 0) return '--'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rs = Math.round(s % 60)
  return `${m}m ${rs}s`
}
</script>

<template>
  <div
    v-if="hasStarted === false && !running && totalTokens === '0'"
    class="chat-speed-card__empty"
    aria-hidden="true"
  />
  <section
    v-else
    class="chat-speed-card"
    :class="{ 'is-running': running, 'is-done': !running && totalTokens !== '0', 'is-collapsed': collapsed }"
    role="status"
    aria-live="polite"
  >
    <header class="chat-speed-card__head">
      <span class="chat-speed-card__label">最近速度</span>
      <button
        type="button"
        class="chat-speed-card__toggle"
        :aria-expanded="!collapsed"
        :title="collapsed ? '展开速度详情' : '收起为紧凑条'"
        @click="collapsed = !collapsed"
      >
        {{ collapsed ? '⊕' : '⊖' }}
      </button>
    </header>

    <div v-if="!collapsed" class="chat-speed-card__body">
      <div class="chat-speed-card__live">
        <span class="chat-speed-card__number">{{ liveRate }}</span>
        <span class="chat-speed-card__unit">tok/s</span>
      </div>

      <div class="chat-speed-card__wave">
        <svg viewBox="0 0 320 56" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="chat-speed-wave-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="currentColor" stop-opacity="0.35" />
              <stop offset="100%" stop-color="currentColor" stop-opacity="0" />
            </linearGradient>
          </defs>
          <path :d="wavePath" fill="url(#chat-speed-wave-fill)" />
          <polyline
            :points="wavePoints"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          />
        </svg>
      </div>

      <div class="chat-speed-card__stats">
        <div>
          <div class="chat-speed-card__stat-label">本轮平均</div>
          <div class="chat-speed-card__stat-value">{{ avgRate }} <small>tok/s</small></div>
        </div>
        <div>
          <div class="chat-speed-card__stat-label">累计输出</div>
          <div class="chat-speed-card__stat-value">{{ totalTokens }} <small>tok</small></div>
        </div>
        <div>
          <div class="chat-speed-card__stat-label">峰值</div>
          <div class="chat-speed-card__stat-value">{{ peakRate }} <small>tok/s</small></div>
        </div>
        <div>
          <div class="chat-speed-card__stat-label">首字延迟</div>
          <div class="chat-speed-card__stat-value">{{ ttftText }}</div>
        </div>
        <div>
          <div class="chat-speed-card__stat-label">单轮耗时</div>
          <div class="chat-speed-card__stat-value">{{ roundText }}</div>
        </div>
      </div>

      <footer class="chat-speed-card__foot">
        <span class="chat-speed-card__status">{{ statusText }}</span>
      </footer>
    </div>

    <div v-else class="chat-speed-card__compact">
      <span class="chat-speed-card__number chat-speed-card__number--compact">{{ liveRate }}</span>
      <span class="chat-speed-card__unit">tok/s</span>
      <span class="chat-speed-card__status">{{ statusText }}</span>
    </div>
  </section>
</template>

<style scoped>
.chat-speed-card {
  --sc-accent: var(--accent);
  --sc-mut: var(--text-muted);
  width: 200px;
  max-width: 100%;
  color: var(--text);
  font-family: inherit;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  box-shadow: 0 6px 18px color-mix(in srgb, var(--text-muted) 28%, transparent);
}
.chat-speed-card__empty {
  width: 1px;
  height: 1px;
  opacity: 0;
}
.chat-speed-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--sc-mut);
  font-size: 12px;
  margin-bottom: 6px;
}
.chat-speed-card__toggle {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--sc-accent);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
}
.chat-speed-card__toggle:hover {
  border-color: var(--sc-accent);
}
.chat-speed-card__body {
  display: flex;
  flex-direction: column;
}
.chat-speed-card__live {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 26px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.5px;
  margin-bottom: 6px;
}
.chat-speed-card__unit {
  font-size: 12px;
  font-weight: 400;
  color: var(--sc-mut);
}
.chat-speed-card__compact {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.chat-speed-card__number--compact {
  font-size: 18px;
  font-weight: 600;
  color: var(--sc-accent);
  font-variant-numeric: tabular-nums;
}
.chat-speed-card__status {
  font-size: 11px;
  color: var(--sc-mut);
}
.chat-speed-card__wave {
  width: 100%;
  height: 30px;
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  color: var(--sc-accent);
  margin-bottom: 10px;
}
.chat-speed-card__wave svg {
  width: 100%;
  height: 100%;
  display: block;
}
.chat-speed-card__stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.chat-speed-card__stats > div:nth-last-child(-n + 2) {
  grid-column: span 3;
}
.chat-speed-card__stat-label {
  color: var(--sc-mut);
  font-size: 11px;
}
.chat-speed-card__stat-value {
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.chat-speed-card__stat-value small {
  color: var(--sc-mut);
  font-size: 11px;
  font-weight: 400;
}
.chat-speed-card__foot {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  color: var(--sc-mut);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.chat-speed-card.is-running .chat-speed-card__live {
  animation: chat-speed-pulse 1.2s ease-in-out infinite;
}
@keyframes chat-speed-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.85; }
}
</style>
