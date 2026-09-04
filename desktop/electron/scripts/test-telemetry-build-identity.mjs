import assert from 'node:assert/strict'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'

import {
  readSourceCommitId,
  sourceTelemetryVersion,
} from '../dist/telemetry/build-identity.js'

const COMMITS = {
  detached: '0123456789abcdef0123456789abcdef01234567',
  loose: '89abcdef0123456789abcdef0123456789abcdef',
  packed: 'fedcba9876543210fedcba9876543210fedcba98',
  worktree: '00112233445566778899aabbccddeeff00112233',
}

const root = mkdtempSync(join(tmpdir(), 'opensquilla-build-identity-'))

function checkout(name) {
  const checkoutRoot = join(root, name)
  mkdirSync(join(checkoutRoot, 'src', 'opensquilla'), { recursive: true })
  writeFileSync(join(checkoutRoot, 'pyproject.toml'), '[project]\nname = "opensquilla"\n')
  return checkoutRoot
}

function writeGitDirectory(checkoutRoot) {
  const gitRoot = join(checkoutRoot, '.git')
  mkdirSync(gitRoot, { recursive: true })
  return gitRoot
}

try {
  // Detached HEAD is the simplest source checkout and must not require refs.
  {
    const checkoutRoot = checkout('detached')
    const gitRoot = writeGitDirectory(checkoutRoot)
    writeFileSync(join(gitRoot, 'HEAD'), `${COMMITS.detached}\n`)
    assert.equal(readSourceCommitId(checkoutRoot), COMMITS.detached)
  }

  // A normal symbolic HEAD resolves through a bounded loose ref file.
  {
    const checkoutRoot = checkout('loose')
    const gitRoot = writeGitDirectory(checkoutRoot)
    mkdirSync(join(gitRoot, 'refs', 'heads'), { recursive: true })
    writeFileSync(join(gitRoot, 'HEAD'), 'ref: refs/heads/main\n')
    writeFileSync(join(gitRoot, 'refs', 'heads', 'main'), `${COMMITS.loose}\n`)
    assert.equal(readSourceCommitId(checkoutRoot), COMMITS.loose)
  }

  // Packed refs are used after Git's maintenance/prune operations.
  {
    const checkoutRoot = checkout('packed')
    const gitRoot = writeGitDirectory(checkoutRoot)
    writeFileSync(join(gitRoot, 'HEAD'), 'ref: refs/heads/main\n')
    writeFileSync(join(gitRoot, 'packed-refs'), `# pack-refs with: peeled fully-peeled\n${COMMITS.packed} refs/heads/main\n`)
    assert.equal(readSourceCommitId(checkoutRoot), COMMITS.packed)
  }

  // Git worktrees use a .git file and a shared commondir for refs.
  {
    const checkoutRoot = checkout('worktree')
    const gitMetadata = join(root, 'worktree-metadata')
    const commonMetadata = join(root, 'common-metadata')
    mkdirSync(gitMetadata, { recursive: true })
    mkdirSync(commonMetadata, { recursive: true })
    writeFileSync(join(checkoutRoot, '.git'), `gitdir: ${relative(checkoutRoot, gitMetadata)}\n`)
    writeFileSync(join(gitMetadata, 'commondir'), `${relative(gitMetadata, commonMetadata)}\n`)
    writeFileSync(join(gitMetadata, 'HEAD'), 'ref: refs/heads/work\n')
    writeFileSync(join(commonMetadata, 'packed-refs'), `${COMMITS.worktree} refs/heads/work\n`)
    assert.equal(readSourceCommitId(checkoutRoot), COMMITS.worktree)
  }

  // Invalid provenance must fail closed and never turn an arbitrary Git repo
  // into an OpenSquilla source identity.
  {
    const arbitrary = join(root, 'arbitrary')
    mkdirSync(join(arbitrary, '.git'), { recursive: true })
    writeFileSync(join(arbitrary, '.git', 'HEAD'), `${COMMITS.detached}\n`)
    assert.equal(readSourceCommitId(arbitrary), null)

    const malformed = checkout('malformed')
    mkdirSync(join(malformed, '.git'), { recursive: true })
    writeFileSync(join(malformed, '.git', 'HEAD'), 'not-a-commit\n')
    assert.equal(readSourceCommitId(malformed), null)

    const oversized = checkout('oversized')
    mkdirSync(join(oversized, '.git'), { recursive: true })
    writeFileSync(join(oversized, '.git', 'HEAD'), 'x'.repeat(4 * 1024 + 1))
    assert.equal(readSourceCommitId(oversized), null)
  }

  assert.equal(
    sourceTelemetryVersion('0.5.4', COMMITS.detached),
    `0.5.4+source.${COMMITS.detached}`,
  )
  assert.equal(sourceTelemetryVersion('0.5.4', null), '0.5.4')
  assert.equal(sourceTelemetryVersion('0.5.4', COMMITS.detached.toUpperCase()), '0.5.4')
  assert.equal(sourceTelemetryVersion('a'.repeat(17), COMMITS.detached), 'a'.repeat(17))
  assert.equal(sourceTelemetryVersion('bad version', COMMITS.detached), 'bad version')

  // Keep the test honest about the intended read-only implementation: the
  // module must not gain a child-process dependency that invokes Git at runtime.
  const source = readFileSync(new URL('../src/telemetry/build-identity.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /(?:spawn|exec)(?:Sync)?\s*\(/)
  assert.equal(existsSync(join(root, 'detached', '.git', 'HEAD')), true)
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('desktop telemetry build identity tests passed')
