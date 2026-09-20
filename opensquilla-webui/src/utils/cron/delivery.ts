import i18n from '@/i18n'
import type { CronDeliveryFormValues, CronJob, DeliveryConfig, FailureDestination } from '@/types/cron'

export interface NormalizedCronDeliveryFields {
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

interface CronDeliveryValidationError {
  error: string
  errorField: keyof CronDeliveryFormValues
  delivery?: never
}

export type CronDeliveryBuildResult =
  | { delivery: DeliveryConfig | null; error?: never }
  | CronDeliveryValidationError

export function normalizeDeliveryFields(job: CronJob | null): NormalizedCronDeliveryFields {
  const d = job?.delivery || {}
  const mode = (d.mode || '').toLowerCase()
  const fd = d.failureDestination || {}
  const fdMode = (fd.mode || '').toLowerCase()
  return {
    deliveryMode: mode === 'webhook' ? 'webhook' : mode === 'announce' || mode === 'channel' ? 'announce' : mode === 'none' ? 'none' : '',
    deliveryChannel: d.channelName || '',
    deliveryTo: d.to || d.channelId || '',
    deliveryAccount: d.accountId || '',
    deliveryWebhookUrl: d.webhookUrl || '',
    deliveryWebhookToken: '',
    deliveryBestEffort: !!d.bestEffort,
    fdMode: fdMode === 'webhook' ? 'webhook' : fdMode === 'channel' || fdMode === 'announce' ? 'channel' : '',
    fdChannel: fd.channelName || '',
    fdTo: fd.to || fd.channelId || '',
    fdAccount: fd.accountId || '',
    fdWebhookUrl: fd.webhookUrl || '',
    fdWebhookToken: '',
  }
}

export function buildDeliveryFromValues(values: CronDeliveryFormValues): CronDeliveryBuildResult {
  const mode = values.deliveryMode
  const fdMode = values.fdMode
  if (!mode && !fdMode) return { delivery: null }

  const failure = buildFailureDestinationFromValues(values)
  if (failure.error !== undefined) return failure
  const fd = failure.delivery

  if (mode === 'none') {
    const out: DeliveryConfig = { mode: 'none' }
    if (fd) out.failureDestination = fd
    return { delivery: out }
  }
  if (mode === 'webhook') {
    const url = values.deliveryWebhookUrl.trim()
    if (!url) return {
      error: i18n.global.t('cronSkills.delivery.errWebhookUrlRequired'),
      errorField: 'deliveryWebhookUrl',
    }
    const out: DeliveryConfig = { mode: 'webhook', webhookUrl: url }
    const token = values.deliveryWebhookToken.trim()
    if (token) out.webhookToken = token
    if (values.deliveryBestEffort) out.bestEffort = true
    if (fd) out.failureDestination = fd
    return { delivery: out }
  }
  if (mode === 'announce') {
    const out: DeliveryConfig = { mode: 'announce' }
    const channel = values.deliveryChannel.trim()
    const to = values.deliveryTo.trim()
    const account = values.deliveryAccount.trim()
    if (channel) out.channelName = channel.toLowerCase()
    if (to) out.to = to
    if (account) out.accountId = account
    if (values.deliveryBestEffort) out.bestEffort = true
    if (fd) out.failureDestination = fd
    return { delivery: out }
  }
  return { delivery: fd ? { failureDestination: fd } : null }
}

function buildFailureDestinationFromValues(values: CronDeliveryFormValues):
  | { delivery: FailureDestination | null; error?: never }
  | CronDeliveryValidationError {
  if (!values.fdMode) return { delivery: null }
  if (values.fdMode === 'webhook') {
    const url = values.fdWebhookUrl.trim()
    if (!url) return {
      error: i18n.global.t('cronSkills.delivery.errFdWebhookUrlRequired'),
      errorField: 'fdWebhookUrl',
    }
    const out: FailureDestination = { mode: 'webhook', webhookUrl: url }
    const token = values.fdWebhookToken.trim()
    if (token) out.webhookToken = token
    return { delivery: out }
  }
  const channel = values.fdChannel.trim()
  const to = values.fdTo.trim()
  const account = values.fdAccount.trim()
  if (!channel && !to) return {
    error: i18n.global.t('cronSkills.delivery.errFdChannelNeedsTarget'),
    errorField: 'fdTo',
  }
  const out: FailureDestination = { mode: 'channel' }
  if (channel) out.channelName = channel.toLowerCase()
  if (to) out.to = to
  if (account) out.accountId = account
  return { delivery: out }
}
