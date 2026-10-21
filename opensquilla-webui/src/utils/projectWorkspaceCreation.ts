export type ProjectWorkspaceMode = 'create' | 'open'

/** A display path on the execution host; the Gateway validates the real path. */
export function projectWorkspaceTarget(parentPath: string, name: string, platform?: 'windows' | 'posix'): string {
  const parent = parentPath.trim()
  const leaf = name.trim()
  if (!parent || !leaf) return ''
  const separator = platform === 'windows' || (!platform && /^[A-Za-z]:[\\/]|^\\\\/.test(parent)) ? '\\' : '/'
  return `${parent.replace(/[\\/]+$/, '')}${separator}${leaf}`
}

export function validProjectDirectoryName(name: string): boolean {
  const leaf = name.trim()
  return Boolean(leaf) && leaf.length <= 120 && leaf !== '.' && leaf !== '..'
    && !/[\\/\x00-\x1f\x7f]/.test(leaf)
}
