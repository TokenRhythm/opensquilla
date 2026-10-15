<template>
  <section class="terminal-panel" data-testid="terminal-panel" @click="focusInput">
    <header class="terminal-panel__status" role="status" aria-live="polite">
      <span class="terminal-panel__dot" :class="`is-${state}`" aria-hidden="true" />
      <span>{{ stateLabel }}</span>
      <span v-if="lastSeq > 0" class="terminal-panel__seq">#{{ lastSeq }}</span>
      <button v-if="state === 'exited' || state === 'error'" type="button" class="btn btn--ghost" @click.stop="restart">
        {{ t('workbench.terminal.restart') }}
      </button>
    </header>
    <div ref="surface" class="terminal-panel__surface" :aria-label="t('workbench.terminal.input')" />
    <p v-if="errorMessage" class="terminal-panel__error" role="alert">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { TERMINAL_ACCESS_KEY, type TerminalAccess } from '@/modules/terminal'

const props = defineProps<{ sessionKey: string }>()
const { t } = useI18n()
const terminal = inject<TerminalAccess | null>(TERMINAL_ACCESS_KEY, null)
const surface = ref<HTMLElement | null>(null)
const state = ref<'starting' | 'running' | 'exited' | 'error'>('starting')
const errorMessage = ref('')
const lastSeq = ref(0)
const terminalId = ref('')
let stopOutput: (() => void) | null = null
let stopExit: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null
let mounted = true
let writeQueue = Promise.resolve()
let xterm: XTerm | null = null
let fitAddon: FitAddon | null = null
let stopData: (() => void) | null = null
let fitting = false
let lastResize = ''

const stateLabel = computed(() => t(`workbench.terminal.state.${state.value}`))

function append(text: string) { xterm?.write(text) }

function focusInput() { xterm?.focus() }

function cssColor(name: 'backgroundColor' | 'color', fallback: string) {
  const node = surface.value
  const style = node ? getComputedStyle(name === 'backgroundColor' ? (node.parentElement ?? node) : node) : null
  const value = style?.[name].trim() || ''
  return value || fallback
}

function write(data: string) {
  const id = terminalId.value
  if (!id || !terminal || state.value !== 'running' || !data) return
  writeQueue = writeQueue.then(() => terminal.write(id, data).then(result => {
    if (!result.accepted) throw new Error('terminal write was rejected')
  })).catch(error => {
    if (!mounted) return
    state.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : String(error)
  })
}

function scheduleResize() {
  if (resizeTimer !== null) return
  resizeTimer = setTimeout(() => {
    resizeTimer = null
    const id = terminalId.value
    const node = surface.value
    if (!id || !node || !terminal || state.value !== 'running') return
    fitting = true
    try { fitAddon?.fit() } finally { fitting = false }
    const cols = Math.max(1, Math.min(400, xterm?.cols || 100))
    const rows = Math.max(1, Math.min(200, xterm?.rows || 30))
    const dimensions = `${cols}x${rows}`
    if (dimensions === lastResize) return
    lastResize = dimensions
    void terminal.resize(id, cols, rows).catch(() => {})
  }, 80)
}

async function start() {
  if (!terminal || !props.sessionKey) { state.value = 'error'; errorMessage.value = t('workbench.terminal.unavailable'); return }
  state.value = 'starting'; errorMessage.value = ''; lastSeq.value = 0; lastResize = ''
  xterm?.dispose(); xterm = null; fitAddon = null; stopData?.(); stopData = null
  if (surface.value) {
    xterm = new XTerm({
      cursorBlink: true,
      scrollback: 5_000,
      convertEol: true,
      theme: {
        background: cssColor('backgroundColor', 'transparent'),
        foreground: cssColor('color', 'currentColor'),
      },
    })
    fitAddon = new FitAddon()
    xterm.loadAddon(fitAddon)
    xterm.open(surface.value)
    fitAddon.fit()
    const dataSubscription = xterm.onData(write)
    xterm.onResize(() => { if (!fitting) scheduleResize() })
    stopData = () => dataSubscription.dispose()
  }
  stopOutput?.(); stopExit?.()
  stopOutput = terminal.onOutput(event => {
    if (event.sessionKey && event.sessionKey !== props.sessionKey) return
    // The PTY reader starts immediately after create commits ownership. Keep
    // a same-session first frame while the create response is in flight so an
    // initial shell prompt cannot be lost on a busy remote connection.
    if (!terminalId.value && state.value === 'starting' && event.sessionKey === props.sessionKey) {
      terminalId.value = event.terminalId
    }
    if (event.terminalId !== terminalId.value || event.seq <= lastSeq.value) return
    if (lastSeq.value && event.seq > lastSeq.value + 1) append(`\r\n[${t('workbench.terminal.outputGap')}]\r\n`)
    lastSeq.value = event.seq
    append(event.data)
  })
  stopExit = terminal.onExit(event => {
    if (event.sessionKey && event.sessionKey !== props.sessionKey) return
    if (!terminalId.value && state.value === 'starting' && event.sessionKey === props.sessionKey) {
      terminalId.value = event.terminalId
    }
    if (event.terminalId !== terminalId.value || event.seq < lastSeq.value) return
    lastSeq.value = Math.max(lastSeq.value, event.seq)
    state.value = 'exited'
    append(`\r\n[${t('workbench.terminal.exited')}${event.exitCode === null ? '' : `: ${event.exitCode}`} ]\r\n`)
  })
  try {
    const handle = await terminal.create(props.sessionKey, { cols: 100, rows: 30 })
    if (!mounted) { await terminal.close(handle.terminalId, 'component-unmount').catch(() => {}); return }
    if (terminalId.value && terminalId.value !== handle.terminalId) {
      await terminal.close(handle.terminalId, 'duplicate-create').catch(() => {})
      return
    }
    terminalId.value = handle.terminalId
    if (state.value === 'starting') state.value = 'running'
    await nextTick(focusInput)
    scheduleResize()
  } catch (error) {
    state.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : String(error)
  }
}

async function restart() {
  const old = terminalId.value
  terminalId.value = ''
  if (old && terminal) await terminal.close(old, 'restart').catch(() => {})
  await start()
}

onMounted(() => {
  mounted = true
  if (typeof ResizeObserver !== 'undefined') { resizeObserver = new ResizeObserver(scheduleResize); if (surface.value) resizeObserver.observe(surface.value) }
  void start()
})
onBeforeUnmount(() => {
  mounted = false
  if (resizeTimer !== null) clearTimeout(resizeTimer)
  resizeObserver?.disconnect()
  stopOutput?.(); stopExit?.()
  stopData?.(); xterm?.dispose(); xterm = null
  if (terminalId.value && terminal) void terminal.close(terminalId.value, 'component-unmount').catch(() => {})
})
</script>

<style scoped>
.terminal-panel { display:flex; min-height:0; flex:1; flex-direction:column; background:var(--bg); color:var(--text); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.terminal-panel__status { display:flex; align-items:center; gap:8px; min-height:34px; padding:0 12px; border-bottom:1px solid var(--border); color:var(--text-dim); font:var(--fs-sm) var(--font-sans); }
.terminal-panel__status .btn { margin-left:auto; }
.terminal-panel__dot { width:8px; height:8px; border-radius:50%; background:var(--text-muted); }
.terminal-panel__dot.is-running { background:var(--ok); }.terminal-panel__dot.is-error { background:var(--danger); }.terminal-panel__dot.is-exited { background:var(--warn); }
.terminal-panel__seq { margin-left:auto; font-size:11px; opacity:.7; }
.terminal-panel__surface { min-height:0; flex:1; overflow:hidden; padding:12px; }
.terminal-panel__surface :deep(.xterm) { height:100%; }
.terminal-panel__surface :deep(.xterm-viewport) { overflow-y:auto !important; }
.terminal-panel__error { margin:0; padding:8px 12px; color:var(--danger); border-top:1px solid var(--danger); font:var(--fs-sm) var(--font-sans); }
</style>
