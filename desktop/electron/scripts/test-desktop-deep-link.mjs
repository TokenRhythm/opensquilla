import { strict as assert } from 'node:assert'

import {
  desktopDeepLinkArguments,
  parseDesktopDeepLink,
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
