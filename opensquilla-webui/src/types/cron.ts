export interface CronJob {
  id: string
  name?: string
  enabled?: boolean
  status?: string
  next_run?: string
  last_run?: string
  lastStatus?: string | null
  last_status?: string
  lastResult?: string | null
  error_count?: number
  expression?: string
  schedule?: string
  payloadKind?: string
  payload_kind?: string
  message?: string
  prompt?: string
  sessionTarget?: string
  session_target?: string
  scheduleKind?: string
  schedule_kind?: string
  scheduleRaw?: string
  schedule_raw?: string
  tz?: string
  wakeMode?: string
  wake_mode?: string
  agentId?: string
  workspaceId?: string
  workspaceName?: string
  workspaceUnavailable?: boolean
  templateId?: string
  delivery?: DeliveryConfig
  originSessionKey?: string
  origin_session_key?: string
  targetSessionKey?: string
  target_session_key?: string
  sessionKey?: string
  session_key?: string
  runMode?: 'safe' | 'full'
  elevated?: string
  executionTarget?: 'sandbox' | 'host'
  deduplicated?: boolean
}

export interface DeliveryConfig {
  mode?: string
  channelName?: string
  to?: string
  channelId?: string
  accountId?: string
  webhookUrl?: string
  webhookToken?: string
  bestEffort?: boolean
  failureDestination?: FailureDestination
}

export interface FailureDestination {
  mode?: string
  channelName?: string
  to?: string
  channelId?: string
  accountId?: string
  webhookUrl?: string
  webhookToken?: string
}

export interface CronRun {
  started_at?: string
  status?: string
  duration_ms?: number
  deliveryStatus?: Record<string, unknown> | string
  delivery_status?: Record<string, unknown> | string
  summary?: string
  sessionKey?: string
}

export interface CronPanelTemplate {
  id?: string
  name?: string
  expression?: string
  payloadKind?: string
  message?: string
  scheduleKind?: string
  schedule_kind?: string
  every_seconds?: number
  at?: string
  tz?: string
  wakeMode?: string
  sessionTarget?: string
  agentId?: string
  targetSessionKey?: string
  workspaceId?: string
  requiresWorkspace?: boolean
}

export interface CronDeliveryFormValues {
  deliveryMode: string
  deliveryChannel: string
  deliveryTo: string
  deliveryAccount: string
  deliveryWebhookUrl: string
  deliveryWebhookToken: string
  deliveryBestEffort: boolean
  fdMode: string
  fdChannel: string
  fdTo: string
  fdAccount: string
  fdWebhookUrl: string
  fdWebhookToken: string
}

export interface CronJobFormModel {
  templateId: string
  name: string
  type: string
  cron: string
  every: string
  at: string
  tz: string
  payloadKind: string
  agentId: string
  workspaceId: string
  workspaceRequired: boolean
  sessionTarget: string
  targetSessionKey: string
  message: string
  wakeMode: string
  deliveryMode: string
  deliveryChannel: string
  deliveryTo: string
  deliveryAccount: string
  deliveryWebhookUrl: string
  deliveryWebhookToken: string
  deliveryBestEffort: boolean
  fdMode: string
  fdChannel: string
  fdTo: string
  fdAccount: string
  fdWebhookUrl: string
  fdWebhookToken: string
  enabled: boolean
}
