<template>
  <section class="terminal-panel" data-testid="terminal-panel" @click="focusInput">
    <header class="terminal-panel__status" role="status" aria-live="polite">
      <span class="terminal-panel__dot" :class="`is-${state}`" aria-hidden="true" />
      <span>{{ stateLabel }}</span>
      <span v-if="cwdPath" class="terminal-panel__cwd" :title="cwdPath">{{ cwdLabel }}</span>
      <button v-if="state === 'exited' || state === 'error'" type="button" class="btn btn--ghost" @click.stop="restart">
        {{ t('workbench.terminal.restart') }}
      </button>
    </header>
    <p v-if="errorMessage" class="terminal-panel__error" role="alert">{{ errorMessage }}</p>
    <div ref="surface" class="terminal-panel__surface" :aria-label="t('workbench.terminal.input')" />
  </section>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { TERMINAL_ACCESS_KEY, normalizeTerminalTarget, terminalTargetKey, type TerminalAccess, type TerminalExit, type TerminalOutput, type TerminalTarget } from '@/modules/terminal'
import { readTerminalTheme } from './terminalTheme'

const props = defineProps<{ sessionKey?: string; target?: TerminalTarget }>()
const { t } = useI18n()
const terminal = inject<TerminalAccess | null>(TERMINAL_ACCESS_KEY, null)
const surface = ref<HTMLElement | null>(null)
const state = ref<'starting' | 'running' | 'exited' | 'error'>('starting')
const errorMessage = ref('')
const lastSeq = ref(0)
const terminalId = ref('')
const cwdPath = ref('')
const cwdLabel = computed(() => {
  const segments = cwdPath.value.split(/[\\/]/).filter(Boolean)
  return segments[segments.length - 1] || cwdPath.value
})
const target = computed(() => normalizeTerminalTarget(props.target ?? { kind: 'session', sessionKey: props.sessionKey }))
let stopOutput: (() => void) | null = null
let stopExit: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let themeObserver: MutationObserver | null = null
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

function refreshTheme() {
  // Updating options recolors the existing buffer without recreating the PTY.
  if (xterm && surface.value) xterm.options.theme = readTerminalTheme(surface.value)
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
  const requestedTarget = target.value
  if (!terminal || !requestedTarget) { state.value = 'error'; errorMessage.value = t('workbench.terminal.unavailable'); return }
  state.value = 'starting'; errorMessage.value = ''; lastSeq.value = 0; lastResize = ''; cwdPath.value = ''
  xterm?.dispose(); xterm = null; fitAddon = null; stopData?.(); stopData = null
  if (surface.value) {
    xterm = new XTerm({
      cursorBlink: true,
      cursorStyle: 'bar',
      cursorWidth: 2,
      fontFamily: getComputedStyle(surface.value).fontFamily,
      fontSize: 13,
      lineHeight: 1.35,
      minimumContrastRatio: 4.5,
      scrollback: 5_000,
      convertEol: true,
      theme: readTerminalTheme(surface.value),
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
  // Shell output can beat the create response. Buffer matching-target frames
  // until its terminalId is known: two panels may create the same target, so
  // adopting the first arriving terminalId would bind one panel to the other.
  const earlyFrames: Array<TerminalOutput | TerminalExit> = []
  const droppedTerminals = new Set<string>()
  let earlyChars = 0
  const matchesTarget = (event: TerminalOutput | TerminalExit) => {
    const eventTarget = event.target ?? (event.sessionKey ? { kind: 'session' as const, sessionKey: event.sessionKey } : null)
    return eventTarget ? terminalTargetKey(eventTarget) === terminalTargetKey(requestedTarget) : false
  }
  const queueEarly = (event: TerminalOutput | TerminalExit) => {
    if (!matchesTarget(event)) return
    earlyFrames.push(event)
    earlyChars += 'data' in event ? event.data.length : 0
    while (earlyFrames.length > 256 || earlyChars > 256 * 1024) {
      const removed = earlyFrames.shift()!
      earlyChars -= 'data' in removed ? removed.data.length : 0
      droppedTerminals.add(removed.terminalId)
    }
  }
  const applyOutput = (event: TerminalOutput) => {
    if ((event.target || event.sessionKey) && !matchesTarget(event)) return
    if (event.terminalId !== terminalId.value || event.seq <= lastSeq.value) return
    if (lastSeq.value && event.seq > lastSeq.value + 1) append(`\r\n[${t('workbench.terminal.outputGap')}]\r\n`)
    lastSeq.value = event.seq
    append(event.data)
  }
  const applyExit = (event: TerminalExit) => {
    if ((event.target || event.sessionKey) && !matchesTarget(event)) return
    if (event.terminalId !== terminalId.value || event.seq < lastSeq.value) return
    lastSeq.value = Math.max(lastSeq.value, event.seq)
    state.value = 'exited'
    append(`\r\n[${t('workbench.terminal.exited')}${event.exitCode === null ? '' : `: ${event.exitCode}`} ]\r\n`)
  }
  stopOutput = terminal.onOutput(event => {
    if (!terminalId.value && state.value === 'starting') queueEarly(event)
    else applyOutput(event)
  })
  stopExit = terminal.onExit(event => {
    if (!terminalId.value && state.value === 'starting') queueEarly(event)
    else applyExit(event)
  })
  try {
    const handle = await terminal.create(props.target ? requestedTarget : props.sessionKey!, { cols: 100, rows: 30 })
    if (!mounted) { await terminal.close(handle.terminalId, 'component-unmount').catch(() => {}); return }
    terminalId.value = handle.terminalId
    cwdPath.value = handle.cwdPath ?? ''
    if (droppedTerminals.has(handle.terminalId)) append(`\r\n[${t('workbench.terminal.outputGap')}]\r\n`)
    for (const event of earlyFrames) {
      if ('data' in event) applyOutput(event)
      else applyExit(event)
    }
    earlyFrames.length = 0
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
  themeObserver = new MutationObserver(refreshTheme)
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] })
  void start()
})
onBeforeUnmount(() => {
  mounted = false
  if (resizeTimer !== null) clearTimeout(resizeTimer)
  resizeObserver?.disconnect()
  themeObserver?.disconnect()
  stopOutput?.(); stopExit?.()
  stopData?.(); xterm?.dispose(); xterm = null
  if (terminalId.value && terminal) void terminal.close(terminalId.value, 'component-unmount').catch(() => {})
})
</script>

<style scoped>
.terminal-panel { display:flex; min-height:0; flex:1; flex-direction:column; background:var(--bg); color:var(--text); font-family:var(--font-mono); }
.terminal-panel__status { display:flex; flex-shrink:0; align-items:center; gap:7px; min-height:28px; padding:0 16px; border-bottom:1px solid var(--border); color:var(--text-muted); font:12px var(--font-sans); }
.terminal-panel__status .btn { margin-left:auto; }
.terminal-panel__cwd { margin-left:auto; max-width:60%; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; color:var(--text-dim); }
.terminal-panel__dot { width:6px; height:6px; border-radius:50%; background:var(--text-muted); }
.terminal-panel__dot.is-running { background:var(--ok); }.terminal-panel__dot.is-error { background:var(--danger); }.terminal-panel__dot.is-exited { background:var(--warn); }
.terminal-panel__surface { min-height:0; flex:1; overflow:hidden; padding:14px 16px; }
.terminal-panel__surface :deep(.xterm) { height:100%; }
.terminal-panel__surface :deep(.xterm-viewport) { overflow-y:auto !important; }
.terminal-panel__error { flex-shrink:0; max-height:8rem; overflow:auto; overflow-wrap:anywhere; white-space:pre-wrap; margin:0; padding:8px 12px; color:var(--danger); border-bottom:1px solid var(--danger); font:var(--fs-sm) var(--font-sans); }
</style>
