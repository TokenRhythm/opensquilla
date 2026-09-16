type WireObject = Record<string, unknown>

export function chatHistoryPayload(
  messages: readonly WireObject[] = [],
  overrides: WireObject = {},
): WireObject {
  return {
    messages: [...messages],
    has_more: false,
    oldest_cursor: null,
    newest_cursor: null,
    history_scope: 'complete',
    loaded_count: messages.length,
    page_size: 100,
    canonical_available: true,
    canonical_complete: true,
    compaction_summaries: [],
    turn_outcomes: [],
    ...overrides,
  }
}

export function sessionMessagesMetadata(overrides: WireObject = {}): WireObject {
  return {
    workspaceId: null,
    projectWorkspace: null,
    projectWorkspaceDeferred: false,
    active_task_group_ids: [],
    run_mode_lock: { locked: false },
    pendingUserInputs: [],
    collaboration: null,
    routing: null,
    currentPlan: null,
    activePlanRun: null,
    goal: null,
    goalSnapshotStreamSeq: null,
    tasks: [],
    active_task: null,
    last_task: null,
    run_status: 'idle',
    hydration_complete: true,
    deferred_fields: [],
    ...overrides,
  }
}

export function sessionMessagesSubscribePayload(
  key: string,
  overrides: WireObject = {},
): WireObject {
  return {
    ...sessionMessagesMetadata(),
    subscribed: true,
    key,
    stream_generation: 'e2e-stream-generation',
    current_stream_seq: 0,
    replay_complete: true,
    replay_gap_reason: null,
    replayed_count: 0,
    ...overrides,
  }
}

export function sessionMessagesHydratePayload(
  key: string,
  overrides: WireObject = {},
): WireObject {
  return {
    ...sessionMessagesMetadata(),
    key,
    hydration_complete: true,
    ...overrides,
  }
}

export function sessionMessagesSnapshotPayload(
  key: string,
  overrides: WireObject = {},
): WireObject {
  return {
    key,
    task_id: null,
    stream_generation: 'e2e-stream-generation',
    current_stream_seq: 0,
    events: [],
    ...overrides,
  }
}
