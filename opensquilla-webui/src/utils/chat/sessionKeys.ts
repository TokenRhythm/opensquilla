export const WEBCHAT_SESSION_KEY = 'agent:main:webchat:default'

export function normalizeAgentId(agentId: string): string {
  const raw = String(agentId || '').trim().toLowerCase()
  if (!raw || raw === 'default') return 'main'
  const normalized = raw.replace(/[^a-z0-9_-]/g, '-').replace(/^-+|-+$/g, '')
  return normalized && normalized !== 'default' ? normalized : 'main'
}

export function agentIdFromSessionKey(key: string): string {
  if (!key.startsWith('agent:')) return 'main'
  return normalizeAgentId(key.split(':')[1] || 'main')
}

export function webchatSessionKey(agentId: string, suffix = 'default'): string {
  return 'agent:' + normalizeAgentId(agentId) + ':webchat:' + suffix
}

export function canonicalSessionKey(key: string): string {
  const value = (key || '').trim()
  if (!value || value === 'default' || value === 'webchat:default') return WEBCHAT_SESSION_KEY
  if (value.startsWith('agent:default:')) return 'agent:main:' + value.slice('agent:default:'.length)
  if (value.startsWith('sess-')) return 'agent:main:webchat:' + value.slice('sess-'.length)
  return value
}
