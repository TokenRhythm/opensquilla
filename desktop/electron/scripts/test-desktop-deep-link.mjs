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

const previewScheme = 'opensquilla-preview'
for (const scheme of ['opensquilla', previewScheme]) {
  const otherScheme = scheme === previewScheme ? 'opensquilla' : previewScheme
  const sessionPath = '/session/agent%3Amain%3Awebchat%3Aabc'
  assert.equal(parseDesktopDeepLink(`${scheme}://open`, scheme), 'open')
  assert.equal(parseDesktopDeepLink(`${scheme.toUpperCase()}://OPEN`, scheme), 'open')
  assert.deepEqual(parseDesktopDeepLinkTarget(`${scheme}://open${sessionPath}`, scheme), {
    action: 'open', sessionKey: 'agent:main:webchat:abc',
  })
  assert.equal(parseDesktopDeepLink(`${otherScheme}://open`, scheme), null)
  assert.equal(parseDesktopDeepLinkTarget(`${otherScheme}://open${sessionPath}`, scheme), null)
  assert.deepEqual(desktopDeepLinkArguments([
    'OpenSquilla.exe', `${otherScheme}://open`, `${scheme}://open`,
    `${scheme.toUpperCase()}://OPEN`, `${scheme}-other://open`,
  ], scheme), [`${scheme}://open`, `${scheme.toUpperCase()}://OPEN`])
}
assert.equal(parseDesktopDeepLink(`${previewScheme}://open`), null)
assert.equal(parseDesktopDeepLinkTarget(`${previewScheme}://open/session/a`), null)
assert.deepEqual(desktopDeepLinkArguments([`${previewScheme}://open`]), [])
for (const suffix of [
  '/session/a/b', '/session/a%2Fb', '/session/%2e%2e', '/session/.%2e',
  '/session/a%5Cb', '/session/%00', '/session/a\nb', '/session/a\tb',
  '/session/', '/session/a?', '/session/a#', '?command=anything', '#anything',
  `/session/${'a'.repeat(513)}`, `/session/${'%41'.repeat(2049)}`,
]) {
  assert.equal(parseDesktopDeepLinkTarget(`${previewScheme}://open${suffix}`, previewScheme), null)
}
for (const url of [
  `${previewScheme}://user:password@open`, `${previewScheme}://open:1234`,
  `${previewScheme}://unknown`, `${previewScheme}:open`,
]) {
  assert.equal(parseDesktopDeepLinkTarget(url, previewScheme), null)
}

console.log('desktop deep-link checks passed')
