import type { SkillCatalog, SkillInstallResult, SkillInstallStatus } from '@/modules/skillCatalog'

const STORAGE_KEY = 'opensquilla.skillInstall.pending.v1'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export interface PendingSkillInstall {
  scope: string
  operationId: string
  identifier: string
  source: string
  displayName: string
}

function readPending(): PendingSkillInstall[] {
  try {
    const value = JSON.parse(globalThis.sessionStorage?.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(value)) return []
    return value.slice(-100).filter((row): row is PendingSkillInstall =>
      row && typeof row.scope === 'string' && /^[a-f0-9]{64}$/.test(row.scope)
      && typeof row.operationId === 'string' && UUID.test(row.operationId)
      && typeof row.identifier === 'string' && row.identifier.length <= 4096
      && typeof row.source === 'string' && row.source.length <= 128
      && typeof row.displayName === 'string' && row.displayName.length <= 512)
  } catch { return [] }
}

function save(rows: PendingSkillInstall[]) {
  try { globalThis.sessionStorage?.setItem(STORAGE_KEY, JSON.stringify(rows.slice(-100))) } catch { /* Storage may be unavailable. */ }
}

function pause(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new Error('Install receipt wait ended')); return }
    const abort = () => { clearTimeout(timer); reject(new Error('Install receipt wait ended')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, delay)
    signal.addEventListener('abort', abort, { once: true })
  })
}

export function createSkillInstallReceipts(catalog: SkillCatalog, signal: AbortSignal) {
  const supported = () => Boolean(catalog.supportsInstallStatus?.() && catalog.installStatus)
  const scope = async () => {
    if (!supported()) return ''
    return (await catalog.installStatus!('00000000-0000-0000-0000-000000000000', { signal })).scope
  }
  const forget = (operationId: string) => save(readPending().filter(row => row.operationId !== operationId))
  return {
    supported,
    forget,
    async remember(row: Omit<PendingSkillInstall, 'scope'>) {
      if (!supported()) return
      const current = await scope()
      if (!current) return
      save([...readPending().filter(item => item.operationId !== row.operationId), { ...row, scope: current }])
    },
    async pending() {
      const current = await scope()
      return current ? readPending().filter(row => row.scope === current) : []
    },
    async wait(operationId: string, onStatus: (status: SkillInstallStatus) => void): Promise<SkillInstallResult> {
      const expected = readPending().find(row => row.operationId === operationId)?.scope
      let attempt = 0
      while (!signal.aborted && supported()) {
        await pause([2000, 5000, 10000][Math.min(attempt++, 2)]!, signal)
        let status: SkillInstallStatus
        try {
          status = await catalog.installStatus!(operationId, { signal })
        } catch (error) {
          if (signal.aborted) break
          const failure = error as { code?: unknown; data?: { code?: unknown } } | null
          const code = String(failure?.code ?? failure?.data?.code ?? '').toUpperCase()
          if (['METHOD_NOT_FOUND', 'UNSUPPORTED', 'UNAUTHORIZED', 'FORBIDDEN',
            'OWNER_REQUIRED', 'PERMISSION_DENIED', 'INVALID_PARAMS'].includes(code)) throw error
          continue
        }
        if (expected && status.scope !== expected) throw new Error('Install receipt belongs to another profile or caller')
        onStatus(status)
        if (status.terminal) {
          if (!status.result) throw new Error('Install result is unknown; installation was not repeated')
          if (status.state !== 'recovery_required') forget(operationId)
          return status.result
        }
      }
      throw new Error('Install receipt wait ended; installation was not repeated')
    },
  }
}
