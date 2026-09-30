const MAX_MESSAGE_LENGTH = 100_000
const MAX_PATH_LENGTH = 32_768

/** Display metadata only: this never reads a file or grants access to one. */
export function copyLocalPathReferences(value: unknown, text?: string): string[] {
  if (!Array.isArray(value) || value.length > MAX_MESSAGE_LENGTH) return []
  if (!value.every(path => typeof path === 'string' && path.length > 0
    && path.length <= MAX_PATH_LENGTH && path.trim() === path
    && !/[\u0000-\u001f\u007f]/.test(path)
    && (/^[a-z]:[\\/]/i.test(path) || /^\\\\[^\\]+\\[^\\]+/.test(path) || path.startsWith('/')))) return []
  const paths = value as string[]
  const suffix = paths.join('\n')
  if (suffix.length > MAX_MESSAGE_LENGTH) return []
  if (text !== undefined && paths.length && (text.length > MAX_MESSAGE_LENGTH
    || (text !== suffix && !text.endsWith(`\n${suffix}`)))) return []
  return [...paths]
}

/** Strip only the exact, explicitly marked suffix; path-looking prose is untouched. */
export function localPathPresentation(text: string, references?: readonly string[]) {
  const paths = copyLocalPathReferences(references, text)
  if (!paths.length) return { text, paths }
  const suffix = paths.join('\n')
  return { text: text === suffix ? '' : text.slice(0, -suffix.length - 1), paths }
}

export function localPathName(path: string): string {
  return path.split(/[\\/]/).pop() || path
}
