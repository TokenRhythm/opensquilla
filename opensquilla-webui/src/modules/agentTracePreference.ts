import { readonly, ref } from 'vue'
import type { AppSettings } from '@/modules/appSettings'

const enabled = ref(false)
let revision = 0

const TRACE_CHANNEL_NAME = 'opensquilla.agent-trace-preference'
const TRACE_STORAGE_KEY = 'opensquilla.agent-trace-preference-change'
type PreferenceReader = {
  settings: Pick<AppSettings, 'read'>
  isAvailable: () => boolean
  onConfirmed?: (enabled: boolean) => void
}
const readers = new Set<PreferenceReader>()
type PreferenceChannel = {
  addEventListener: (type: 'message', listener: (event: { data: unknown }) => void) => void
  postMessage: (message: TracePreferenceMessage) => void
}
type PreferenceChannelConstructor = new (name: string) => PreferenceChannel

let channel: PreferenceChannel | undefined
let syncInstalled = false

type TracePreferenceMessage = {
  nonce: string
}

function runtimeWindow(): Window | undefined {
  return typeof window === 'undefined' ? undefined : window
}

function isPreferenceMessage(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  const message = value as Partial<TracePreferenceMessage>
  return typeof message.nonce === 'string' && Boolean(message.nonce)
}

function applyRemotePreference(value: unknown): void {
  if (!isPreferenceMessage(value)) return
  const reader = [...readers].reverse().find(item => item.isAvailable())
  if (reader) {
    void refreshAgentTraceEnabled(reader.settings, reader.isAvailable).then(confirmed => {
      if (confirmed === null || !readers.has(reader)) return
      for (const item of readers) {
        if (item.settings === reader.settings && item.isAvailable()) item.onConfirmed?.(confirmed)
      }
    })
  }
  else setAgentTraceEnabled(false)
}

function installSync(): void {
  if (syncInstalled) return
  const target = runtimeWindow()
  if (!target) return
  syncInstalled = true

  target.addEventListener('storage', event => {
    if (event.key !== TRACE_STORAGE_KEY || !event.newValue) return
    try {
      applyRemotePreference(JSON.parse(event.newValue))
    } catch {
      // Ignore storage events written by another version of the UI.
    }
  })

  const Channel = (target as unknown as { BroadcastChannel?: PreferenceChannelConstructor }).BroadcastChannel
  if (typeof Channel === 'function') {
    try {
      channel = new Channel(TRACE_CHANNEL_NAME)
      channel.addEventListener('message', event => applyRemotePreference(event.data))
    } catch {
      channel = undefined
    }
  }
}

function publishPreferenceChange(): void {
  installSync()
  const message: TracePreferenceMessage = {
    nonce: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
  }
  try {
    const current = channel
    if (current) current.postMessage(message)
  } catch {
    // BroadcastChannel can be closed by a browser during tab teardown.
  }
  try {
    runtimeWindow()?.localStorage.setItem(TRACE_STORAGE_KEY, JSON.stringify(message))
  } catch {
    // Private browsing and storage-disabled contexts still use the channel.
  }
}

export const agentTraceEnabled = readonly(enabled)

export function setAgentTraceEnabled(value: unknown): void {
  revision += 1
  enabled.value = value === true
}

export function registerAgentTracePreferenceReader(
  settings: Pick<AppSettings, 'read'>,
  isAvailable: () => boolean,
  onConfirmed?: (enabled: boolean) => void,
): () => void {
  installSync()
  const reader = { settings, isAvailable, onConfirmed }
  readers.add(reader)
  return () => { readers.delete(reader) }
}

export function notifyAgentTracePreferenceChanged(): void {
  publishPreferenceChange()
}

export async function refreshAgentTraceEnabled(
  settings: Pick<AppSettings, 'read'>,
  isAvailable: () => boolean = () => true,
): Promise<boolean | null> {
  installSync()
  const current = ++revision
  enabled.value = false
  if (!isAvailable()) return null
  try {
    const value = await settings.read('privacy.agent_trace_enabled')
    if (current !== revision || !isAvailable()) return null
    enabled.value = value === true
    return enabled.value
  } catch {
    if (current === revision) enabled.value = false
    return null
  }
}
