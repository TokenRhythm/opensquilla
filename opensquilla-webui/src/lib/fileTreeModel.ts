/**
 * File-tree model: pure functions that turn flat path lists or lazy
 * per-directory children into the flat row list a virtualized renderer
 * consumes.
 *
 * Ported (adapted to the OpenSquilla types and lint rules) from
 * anomalyco/opencode `packages/app/src/components/file-tree-v2-model.ts`
 * (MIT, Copyright (c) 2025 opencode). See THIRD_PARTY_NOTICES.md.
 */

export type FileNode = {
  name: string
  path: string
  type: 'file' | 'directory'
  size?: number
  mtime?: number
}

export type FileTreeRow = {
  node: FileNode
  level: number
}

export type FileTreeModel = {
  children: ReadonlyMap<string, readonly FileNode[]>
  total: number
}

export function normalizeFileTreePath(value: string): string {
  return value
    .replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '')
    .replace(/\/{2,}/g, '/')
}

/**
 * Build a tree model from a flat list of relative paths (POSIX form, or
 * Windows backslash paths which are normalized first). Parent directories
 * are synthesized. Siblings sort directories-first, then case-insensitively
 * by name.
 */
export function buildFileTreeModel(paths: readonly string[]): FileTreeModel {
  const nodes = new Map<string, FileNode>()

  paths.forEach((value) => {
    const file = normalizeFileTreePath(value)
    if (!file) return

    const parts = file.split('/')
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join('/')
      if (nodes.has(path)) return
      nodes.set(path, {
        name,
        path,
        type: index === parts.length - 1 ? 'file' : 'directory',
      })
    })
  })

  const children = new Map<string, FileNode[]>()
  nodes.forEach((node) => {
    const index = node.path.lastIndexOf('/')
    const parent = index === -1 ? '' : node.path.slice(0, index)
    const list = children.get(parent)
    if (list) list.push(node)
    else children.set(parent, [node])
  })
  children.forEach((siblings) =>
    siblings.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
    }),
  )

  return { children, total: nodes.size }
}

/**
 * Flatten an already-built model into render rows, following only the
 * directories for which `expanded(path)` is true.
 */
export function flattenFileTreeModel(model: FileTreeModel, expanded: (path: string) => boolean): FileTreeRow[] {
  const rows: FileTreeRow[] = []
  const stack = [...(model.children.get('') ?? [])].reverse().map((node) => ({ node, level: 0 }))

  while (stack.length > 0) {
    const row = stack.pop()!
    rows.push(row)
    if (row.node.type !== 'directory' || !expanded(row.node.path)) continue
    const kids = model.children.get(row.node.path) ?? []
    for (let index = kids.length - 1; index >= 0; index--) {
      stack.push({ node: kids[index]!, level: row.level + 1 })
    }
  }

  return rows
}

/**
 * Flatten a lazily-loaded tree: `children(path)` returns the immediate
 * children of one directory (root is ""), and only expanded directories are
 * followed. Matches the Pinia store's per-directory listing.
 */
export function flattenLiveFileTreeModel(
  children: (path: string) => readonly FileNode[],
  expanded: (path: string) => boolean,
): FileTreeRow[] {
  const rows: FileTreeRow[] = []
  const stack = [...children('')].reverse().map((node) => ({ node, level: 0 }))

  while (stack.length > 0) {
    const row = stack.pop()!
    rows.push(row)
    if (row.node.type !== 'directory' || !expanded(row.node.path)) continue
    const nested = children(row.node.path)
    for (let index = nested.length - 1; index >= 0; index--) {
      stack.push({ node: nested[index]!, level: row.level + 1 })
    }
  }

  return rows
}
