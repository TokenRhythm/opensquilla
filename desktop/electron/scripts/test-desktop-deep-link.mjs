import { strict as assert } from 'node:assert'

import {
  desktopDeepLinkArguments,
  parseDesktopDeepLink,
  parseDesktopDeepLinkTarget,
} from '../dist/desktop-deep-link.js'

for (const url of [
  'opensquilla://open',
  'opensquilla://open/',
  'OPENSQUILLA://OPEN',
]) {
  assert.equal(parseDesktopDeepLink(url), 'open', url)
}

for (const url of [
  '',
  'not a URL',
  'https://open',
  'tokenrhythm://open',
  'opensquilla://unknown',
  'opensquilla://open/anything',
  'opensquilla://open?command=anything',
  'opensquilla://open#anything',
  'opensquilla://user@open',
  'opensquilla://open:1234',
  'opensquilla:open',
]) {
  assert.equal(parseDesktopDeepLink(url), null, url)
}

assert.equal(parseDesktopDeepLink(null), null)
assert.equal(parseDesktopDeepLink({}), null)

assert.deepEqual(
  parseDesktopDeepLinkTarget('opensquilla://open/session/agent%3Amain%3Awebchat%3Aabc'),
  { action: 'open', sessionKey: 'agent:main:webchat:abc' },
)
for (const url of [
  'opensquilla://open/session/a/b',
  'opensquilla://open/session/a%2Fb',
  'opensquilla://open/session/%2e%2e',
  'opensquilla://open/session/.%2e',
  'opensquilla://open/session/%2e.',
  'opensquilla://open/session/%2e%2E/../',
  'opensquilla://open/session/a?',
  'opensquilla://open/session/a#',
  'opensquilla://open/session/a\nb',
  'opensquilla://open/session/a\tb',
  'opensquilla://open/session/%00',
  'opensquilla://open/session/a%5Cb',
  'opensquilla://user:password@open/session/a',
  'opensquilla://open:1234/session/a',
  `opensquilla://open/session/${'a'.repeat(513)}`,
  `opensquilla://open/session/${'%41'.repeat(2049)}`,
  'opensquilla://open/session/a?query=1',
  'opensquilla://open/session/a#hash',
  'opensquilla://open/session/',
]) {
  assert.equal(parseDesktopDeepLinkTarget(url), null, url)
}

assert.deepEqual(
  desktopDeepLinkArguments([
    'OpenSquilla.exe',
    '--flag',
    'opensquilla://open',
    'https://example.com',
    'opensquilla://unknown',
  ]),
  ['opensquilla://open', 'opensquilla://unknown'],
)
assert.deepEqual(
  desktopDeepLinkArguments(['OpenSquilla.exe', '--flag']),
  [],
)

console.log('desktop deep-link checks passed')
