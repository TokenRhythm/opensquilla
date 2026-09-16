// Release-tag parsing shared by desktop update-channel discovery and validation,
// split out of main.ts so it can be unit-tested without pulling in Electron.
//
// OpenSquilla ships two spellings of the same release: PEP440 for the Git tag /
// Python wheel (e.g. v0.5.0rc2) and npm semver for the Electron app metadata
// (0.5.0-rc2). The parser normalizes both spellings so mirrored manifests and
// the GitHub release-inventory fallback use the same update-channel path.

export const GITHUB_UPDATE_OWNER = 'TokenRhythm'
export const GITHUB_UPDATE_REPO = 'opensquilla'

export interface ParsedReleaseTag {
  base: string
  rc: number | null
}

// Accept the PEP440 rc tag (v0.5.0rc2), the semver rc spelling (v0.5.0-rc2 /
// v0.5.0-rc.2), and a plain stable tag (v0.5.0). Returns null for anything else
// (doc/website releases, monorepo tags, malformed tags).
export function parseOpenSquillaReleaseTag(tag: string): ParsedReleaseTag | null {
  const match = /^v?(\d+)\.(\d+)\.(\d+)(?:-?rc\.?(\d+))?$/i.exec(String(tag ?? '').trim())
  if (!match) return null
  const [, major, minor, patch, rc] = match
  return {
    base: `${Number(major)}.${Number(minor)}.${Number(patch)}`,
    rc: rc === undefined ? null : Number(rc),
  }
}
