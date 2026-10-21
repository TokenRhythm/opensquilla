import { computed, shallowRef, watch } from 'vue'

interface Options {
  /** Delivery scope, session and epoch; empty while no current identity is known. */
  identity: () => string
  /** A real task identity, never a pending/finished/stopped sentinel. */
  taskId: () => string
  canStop: () => boolean
  hydrating: () => boolean
  /** An explicit terminal fact, not the provisional idle state during recovery. */
  terminal: () => boolean
}

/** Preserve only a known task's Stop appearance while its authority is refreshed. */
export function useStableStopControl(options: Options) {
  const known = shallowRef<{ identity: string; taskId: string } | null>(null)

  // Retire obsolete evidence immediately, including a terminal/reset followed
  // by another read in the same Vue update batch. Only capture is deferred.
  watch([options.identity, options.taskId, options.terminal], ([identity, taskId, terminal]) => {
    if (terminal || (known.value && (identity !== known.value.identity
      || (taskId && taskId !== known.value.taskId)))) known.value = null
  }, { flush: 'sync' })

  // Batch synchronous ownership resets. Watching each field synchronously could
  // associate the outgoing task with the incoming session during navigation.
  watch(() => ({
    identity: options.identity(),
    taskId: options.taskId(),
    canStop: options.canStop(),
    hydrating: options.hydrating(),
    terminal: options.terminal(),
  }), state => {
    if (!state.identity || state.terminal) {
      known.value = null
      return
    }
    if (known.value && (known.value.identity !== state.identity
      || (state.taskId && known.value.taskId !== state.taskId))) known.value = null
    if (state.canStop && state.taskId && !state.hydrating) {
      known.value = { identity: state.identity, taskId: state.taskId }
    } else if (!state.hydrating) {
      known.value = null
    }
  }, { immediate: true })

  const showStop = computed(() => options.canStop() || Boolean(
    !options.terminal()
    && options.hydrating()
    && known.value
    && options.identity() === known.value.identity
    && (!options.taskId() || options.taskId() === known.value.taskId),
  ))

  return { showStop }
}
