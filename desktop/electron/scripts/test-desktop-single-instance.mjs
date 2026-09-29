import assert from 'node:assert/strict'
import fs from 'node:fs'
import { closeSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, openSync, readFileSync,
  realpathSync, rmSync, symlinkSync, unlinkSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { syncBuiltinESMExports } from 'node:module'
import { isAbsolute, join, relative } from 'node:path'
import {
  acknowledgeDesktopActivation, clearDesktopActivationAcknowledgements,
  createDesktopActivationRequest, disposeDesktopActivationRequest,
  hasDesktopActivationAcknowledgement,
} from '../dist/desktop-single-instance.js'

const temporaryRoot = realpathSync(tmpdir())
const root = realpathSync(mkdtempSync(join(temporaryRoot, 'opensquilla-single-instance-')))
const receiptPath = (home, request) => join(home, 'desktop-activation', `${request.nonce}.ack`)
const data = request => ({ desktopActivation: request })
let count = 0

function profile() {
  const home = join(root, String(++count))
  mkdirSync(home)
  return home
}

try {
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    assert.match(request.nonce, /^[a-f0-9]{32}$/)
    assert.notEqual(request.nonce, createDesktopActivationRequest().nonce)
    assert.equal(existsSync(join(home, 'desktop-activation')), false, 'cold launches create no receipt directory')
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false)
    assert.equal(acknowledgeDesktopActivation(home, data(request)), true)
    assert.equal(hasDesktopActivationAcknowledgement(home, request), true)
    assert.equal(lstatSync(receiptPath(home, request)).size, 0, 'receipts contain no profile or activation payload')
    assert.equal(acknowledgeDesktopActivation(home, data(request)), false, 'repeated delivery cannot overwrite a receipt')
    disposeDesktopActivationRequest(home, request)
    assert.equal(existsSync(receiptPath(home, request)), false)
    clearDesktopActivationAcknowledgements()
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    for (const malformed of [
      null, {}, { desktopActivation: null },
      data({ ...request, version: 2 }), data({ ...request, nonce: '../outside' }),
      data({ ...request, nonce: 'A'.repeat(32) }), data({ ...request, nonce: 7 }),
      data({ ...request, expiresAt: Date.now() - 1 }),
      data({ ...request, expiresAt: Date.now() + 60_000 }),
      data({ ...request, expiresAt: Number.POSITIVE_INFINITY }),
    ]) assert.equal(acknowledgeDesktopActivation(home, malformed), false)
    assert.equal(existsSync(join(home, 'desktop-activation')), false, 'invalid metadata has no filesystem effects')
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    mkdirSync(join(home, 'desktop-activation'))
    const path = receiptPath(home, request)
    writeFileSync(path, 'another file')
    assert.equal(acknowledgeDesktopActivation(home, data(request)), false)
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false)
    disposeDesktopActivationRequest(home, request)
    assert.equal(readFileSync(path, 'utf8'), 'another file', 'neither acknowledgement nor cleanup replaces existing content')
    unlinkSync(path)
    closeSync(openSync(path, 'wx'))
    utimesSync(path, new Date(0), new Date(0))
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false, 'old receipts cannot acknowledge a new request')
    disposeDesktopActivationRequest(home, request)
    assert.equal(existsSync(path), true, 'old receipts are not owned by this request')
  }
  {
    const home = profile()
    const external = profile()
    const request = createDesktopActivationRequest()
    symlinkSync(external, join(home, 'desktop-activation'), process.platform === 'win32' ? 'junction' : 'dir')
    assert.equal(acknowledgeDesktopActivation(home, data(request)), false, 'redirected directories are rejected')
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false)
    disposeDesktopActivationRequest(home, request)
    assert.equal(existsSync(join(external, `${request.nonce}.ack`)), false)
    unlinkSync(join(home, 'desktop-activation'))
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    mkdirSync(join(home, 'desktop-activation'))
    const target = profile()
    symlinkSync(target, receiptPath(home, request), process.platform === 'win32' ? 'junction' : 'dir')
    assert.equal(acknowledgeDesktopActivation(home, data(request)), false, 'exclusive creation never follows an existing link')
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false)
    disposeDesktopActivationRequest(home, request)
    assert.equal(lstatSync(receiptPath(home, request)).isSymbolicLink(), true, 'cleanup never follows or removes an unowned link')
    unlinkSync(receiptPath(home, request))
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    mkdirSync(join(home, 'desktop-activation'))
    const outside = join(home, 'existing-empty')
    closeSync(openSync(outside, 'wx'))
    linkSync(outside, receiptPath(home, request))
    assert.equal(acknowledgeDesktopActivation(home, data(request)), false)
    assert.equal(hasDesktopActivationAcknowledgement(home, request), false, 'hard-linked files are not acknowledgements')
    disposeDesktopActivationRequest(home, request)
    assert.equal(existsSync(receiptPath(home, request)), true)
    assert.equal(existsSync(outside), true)
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    assert.equal(acknowledgeDesktopActivation(home, data(request)), true)
    const path = receiptPath(home, request)
    unlinkSync(path)
    writeFileSync(path, 'replacement')
    clearDesktopActivationAcknowledgements()
    assert.equal(readFileSync(path, 'utf8'), 'replacement', 'primary cleanup preserves a replaced file')
  }
  {
    const home = profile()
    const request = createDesktopActivationRequest()
    const path = receiptPath(home, request)
    const replacement = join(home, 'replacement')
    closeSync(openSync(replacement, 'wx'))
    const replacementIdentity = lstatSync(replacement)
    const originalClose = fs.closeSync
    try {
      fs.closeSync = descriptor => {
        originalClose(descriptor)
        fs.renameSync(path, join(home, 'original-created-receipt'))
        fs.renameSync(replacement, path)
      }
      syncBuiltinESMExports()
      assert.equal(acknowledgeDesktopActivation(home, data(request)), false,
        'a path replaced immediately after creation cannot become an owned acknowledgement')
    } finally {
      fs.closeSync = originalClose
      syncBuiltinESMExports()
    }
    clearDesktopActivationAcknowledgements()
    assert.equal(lstatSync(path).ino, replacementIdentity.ino,
      'cleanup preserves the file that replaced the exclusive-created handle')
  }
  {
    const home = profile()
    const receipts = Array.from({ length: 33 }, () => createDesktopActivationRequest())
    receipts.forEach((request, index) => {
      assert.equal(acknowledgeDesktopActivation(home, data(request)), index < 32,
        'receipt ownership and cleanup timers remain bounded')
    })
    clearDesktopActivationAcknowledgements()
    for (const request of receipts) assert.equal(existsSync(receiptPath(home, request)), false)
  }
  {
    const home = profile()
    const request = { ...createDesktopActivationRequest(), expiresAt: Date.now() + 50 }
    assert.equal(acknowledgeDesktopActivation(home, data(request)), true)
    await new Promise(resolve => setTimeout(resolve, 1_150))
    assert.equal(existsSync(receiptPath(home, request)), false, 'the primary also expires abandoned receipts')
  }
  console.log('desktop single-instance acknowledgement checks passed (synthetic directories; no app or Gateway)')
} finally {
  clearDesktopActivationAcknowledgements()
  const relativeRoot = relative(temporaryRoot, realpathSync(root))
  assert.ok(relativeRoot && !relativeRoot.startsWith('..') && !isAbsolute(relativeRoot), 'cleanup stays in the created temporary workspace')
  rmSync(root, { recursive: true, force: true })
}
