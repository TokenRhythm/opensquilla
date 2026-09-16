import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { basename, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { createBusinessDriver } from './live-html-journey-business.mjs'
import { nativeObservation } from './live-html-native-observation.mjs'
import { capturePageVisualEvidence } from './live-html-visual-evidence.mjs'

export async function createSupplementalClient(app, page, options) {
  const { report, directory, persist, readState, exportArtifacts, checkLedger, configuration } = options
  let selectedId = null
  let lastAnswerIds = []
  let pendingFiles = []
  const evaluatePage = (id, expression) => app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    if (!contents || contents.isDestroyed()) throw new Error('SUPPLEMENTAL_TARGET_LOST')
    return contents.executeJavaScript(request.expression, true)
  }, { id, expression })
  const business = id => createBusinessDriver(app, () => id ?? selectedId, async () => {})
  const wait = async (check, stage, milliseconds = 30000) => {
    const until = milliseconds === 0 ? Infinity : Date.now() + milliseconds
    while (Date.now() < until) {
      if (options.interrupted()) throw new Error('SUPPLEMENTAL_INTERRUPTED')
      const value = await check()
      if (value) return value
      await delay(200)
    }
    const error = new Error('SUPPLEMENTAL_WAIT_EXPIRED')
    error.diagnostic = { stage, waitMilliseconds: milliseconds }
    throw error
  }
  const installObservation = () => {
    if (window.__htmlSupplementalObservation) return
    const state = { turns: [], current: null, reviewRequests: [] }
    window.__htmlSupplementalObservation = state
    const original = WebSocket.prototype.send
    const observed = new WeakSet()
    WebSocket.prototype.send = function (data) {
      let message
      try { message = JSON.parse(data) } catch { return original.call(this, data) }
      if (!observed.has(this)) {
        observed.add(this)
        this.addEventListener('message', event => {
          let row
          try { row = JSON.parse(event.data) } catch { return }
          const payload = row.payload || row.result || {}
          const review = row.type === 'res' && state.reviewRequests.find(item => item.requestId === row.id)
          if (review) {
            review.respondedAt = Date.now()
            review.ok = row.ok !== false && !row.error
            if (review.method === 'artifacts.revisions.restore') {
              review.headRevisionId = payload.document?.headRevisionId ?? null
              review.generation = payload.document?.generation ?? null
              review.stateRevision = payload.document?.stateRevision ?? null
              review.revisionId = payload.revision?.id ?? null
              review.noOp = payload.changeSet?.validation?.no_op === true
              review.receipt = Object.fromEntries(['requestId', 'documentId', 'baseRevisionId', 'resultRevisionId', 'changeSetId', 'stateRevision', 'status']
                .filter(key => typeof payload.receipt?.[key] === 'string' || Number.isSafeInteger(payload.receipt?.[key]))
                .map(key => [key, payload.receipt[key]]))
            } else {
              review.changeSetIds = Array.isArray(payload.changeSets) ? payload.changeSets.map(item => item.id) : null
            }
          }
          const turn = state.current
          if (!turn) return
          if (row.type === 'res' && row.id === turn.requestId) {
            turn.acceptedAt = Date.now()
            turn.taskId = payload.task_id || payload.taskId || payload.turn_id || payload.turnId || null
            turn.userMessageId = payload.user_message_id || payload.userMessageId || null
            if (row.ok === false || row.error) {
              turn.rejected = true
              const code = row.error?.data?.code || row.error?.code
              turn.rejectionCode = typeof code === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(code) ? code : 'RPC_REJECTED'
            }
          }
          if (row.type === 'event' && String(row.event).startsWith('session.event.')) {
            const detail = payload.data || payload
            const eventSession = detail.sessionKey || detail.session_key || payload.sessionKey || payload.session_key || payload.key
            if (eventSession && turn.sessionKey && eventSession !== turn.sessionKey) return
            const eventTask = detail.task_id || detail.taskId
            if (eventTask && turn.taskId && eventTask !== turn.taskId) return
            turn.eventCounts[row.event] = (turn.eventCounts[row.event] || 0) + 1
            if (row.event === 'session.event.provider_activity') turn.providerEventAt ??= Date.now()
            if (['session.event.provider_activity', 'session.event.tool_use_start', 'session.event.tool_result', 'session.event.done', 'session.event.error'].includes(row.event)) {
              const safe = { event: row.event, at: Date.now(), taskId: eventTask || null }
              for (const key of ['model', 'tool_name', 'tool_use_id', 'code', 'is_error']) {
                const value = detail[key]
                if (typeof value === 'boolean' || typeof value === 'string' && /^[A-Za-z0-9_.:/-]{1,160}$/.test(value)) safe[key] = value
              }
              if (row.event === 'session.event.provider_activity' && ['requesting', 'reasoning', 'retry_wait', 'retrying', 'fallback'].includes(detail.phase)) safe.phase = detail.phase
              if (row.event === 'session.event.done' && Array.isArray(detail.execution_legs)) {
                safe.executionModels = detail.execution_legs.map(leg => typeof leg?.model === 'string' && /^[A-Za-z0-9_.:/-]{1,160}$/.test(leg.model) ? leg.model : null)
              }
              if (turn.events.length < 10000) turn.events.push(safe)
              else turn.evidenceIncomplete = true
            }
          }
        })
      }
      if (message.method === 'chat.send') {
        const turn = { sessionKey: message.params?.sessionKey || message.params?.session_key || message.params?.key, attachmentNames: (message.params?.attachments || []).map(item => item.name || item.filename || ''), requestId: message.id, clientRequestId: message.params?.clientRequestId || message.params?.client_request_id, submittedAt: Date.now(), annotationTexts: (message.params?.pageContext?.annotations || []).map(item => item.text), annotationCount: message.params?.pageContext?.annotations?.length || 0, attachmentCount: message.params?.attachments?.length || 0, eventCounts: {}, events: [] }
        state.turns.push(turn)
        state.current = turn
      }
      if (['artifacts.revisions.restore', 'artifacts.changes.list'].includes(message.method)) {
        state.reviewRequests.push({ method: message.method, requestId: message.id,
          clientRequestId: message.params?.clientRequestId ?? null,
          documentId: message.params?.documentId ?? null, targetRevisionId: message.params?.revisionId ?? null,
          submittedAt: Date.now() })
      }
      return original.call(this, data)
    }
  }
  await page.addInitScript(installObservation)
  await page.evaluate(installObservation)
  const observation = async () => {
    const turn = await page.evaluate(() => window.__htmlSupplementalObservation.current)
    if (!turn) return null
    turn.annotationTextsSha256 = createHash('sha256').update(JSON.stringify(turn.annotationTexts)).digest('hex')
    delete turn.annotationTexts
    if (turn.taskId) turn.events = turn.events.filter(event => !event.taskId || event.taskId === turn.taskId)
    return turn
  }
  const answers = () => page.locator('.msg-ai[data-message-id]').evaluateAll(elements => elements.flatMap(element => {
    const text = element.querySelector('.assistant-answer')?.textContent?.trim()
    return text ? [{ id: element.getAttribute('data-message-id'), text }] : []
  }))
  const currentPreview = async () => {
    const state = await nativeObservation(app)
    const matches = state.contents.filter(row => row.visible && row.path.split('/').at(-1) === 'supplemental.html')
    assert.ok(matches.length < 2, 'SUPPLEMENTAL_PREVIEW_AMBIGUOUS')
    if (matches[0]) selectedId = matches[0].id
    return matches[0]
  }
  const client = {
    fixtureUrl: options.fixtureUrl,
    record: event => { report.events.push(event) },
    check: result => { report.checks.push(result) },
    fillComposer: text => page.locator('.chat-textarea').fill(text),
    async send(name, prompt, turnOptions = {}) {
      await checkLedger()
      report.phase = name
      await persist()
      await page.locator('.chat-textarea').fill(prompt)
      const send = page.locator('.chat-send-btn.btn--primary')
      await wait(async () => await send.isVisible() && !await send.isDisabled(), 'composer-ready')
      const previous = (await observation())?.requestId
      lastAnswerIds = (await answers()).map(item => item.id)
      const beforeMaterial = turnOptions.cancelWhenFilesChange ? await client.material() : null
      let lastMaterialProbe = 0
      let filesChangedBeforeCancel = false
      let materialAtStop = null
      await send.click()
      await wait(async () => {
        const state = await observation()
        if (state?.requestId === previous) return false
        assert.ok(!state?.rejected || turnOptions.allowCapabilityFailure, 'SUPPLEMENTAL_CHAT_REJECTED')
        return state?.taskId || state?.rejected
      }, 'durable-acceptance', 60000)
      const accepted = await observation()
      if (accepted.rejected) {
        const turn = { name, ...accepted, terminalStatus: 'rejected', previousAnswerIds: lastAnswerIds, finishedAt: Date.now() }
        report.turns.push(turn)
        await persist()
        return turn
      }
      assert.ok(accepted.clientRequestId && accepted.userMessageId, 'SUPPLEMENTAL_ACCEPTANCE_IDENTITIES_MISSING')
      for (const file of pendingFiles) assert.ok(accepted.attachmentNames.includes(file.name), 'SUPPLEMENTAL_EXACT_ATTACHMENT_NOT_SENT')
      const receipt = await wait(async () => {
        const state = await readState()
        const receipts = (state.accepted_messages || []).filter(row => row.task_id === accepted.taskId && row.client_request_id === accepted.clientRequestId)
        assert.ok(receipts.length <= 1, 'SUPPLEMENTAL_DUPLICATE_ACCEPTANCE')
        const item = receipts[0]
        return item?.messageMatches === 1 && item.role === 'user' ? item : null
      }, 'persisted-user-message', 30000)
      assert.equal(receipt.message_id, accepted.userMessageId, 'SUPPLEMENTAL_MESSAGE_BINDING_MISMATCH')
      assert.equal(receipt.annotationCount, accepted.annotationCount, 'SUPPLEMENTAL_ANNOTATIONS_NOT_PERSISTED')
      assert.equal(receipt.annotationTextsSha256, accepted.annotationTextsSha256, 'SUPPLEMENTAL_ANNOTATION_CONTENT_MISMATCH')
      assert.equal(receipt.attachmentCount, accepted.attachmentCount, 'SUPPLEMENTAL_ATTACHMENTS_NOT_PERSISTED')
      let stopRequested = false
      let terminal
      const limit = configuration.runtimeTimeoutSeconds > 0 ? (configuration.runtimeTimeoutSeconds + configuration.webuiIdleGraceSeconds + 30) * 1000 : 0
      await wait(async () => {
        const observed = await observation()
        const state = await readState()
        const task = state.agent_tasks.find(row => row.task_id === observed.taskId)
        if (task && !['queued', 'running', 'waiting'].includes(task.status)) { terminal = task; return true }
        if (turnOptions.cancelWhenFilesChange && !stopRequested && Date.now() - lastMaterialProbe >= 2000) {
          lastMaterialProbe = Date.now()
          const current = await client.material()
          const bytes = snapshot => snapshot.working.flatMap(bundle => bundle.files.map(file => [bundle.documentId, file.path, file.sha256])).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
          filesChangedBeforeCancel = JSON.stringify(bytes(current)) !== JSON.stringify(bytes(beforeMaterial))
        }
        if (turnOptions.cancelWhenFilesChange && filesChangedBeforeCancel && !stopRequested) {
          const stop = page.locator('.chat-send-btn.btn--danger')
          if (await stop.isVisible() && !await stop.isDisabled()) { materialAtStop = await client.material(); await stop.click(); stopRequested = true }
        }
        report.activeTurn = { name, ...observed, stopRequested }
        await persist()
        return false
      }, `${name}-terminal`, limit)
      const turn = { name, ...await observation(), receipt, previousAnswerIds: lastAnswerIds, attachedFiles: pendingFiles, materialAtStop, terminalStatus: terminal.status, terminalReason: terminal.terminal_reason, errorClass: terminal.error_class, finishedAt: Date.now(), ...(turnOptions.cancelWhenFilesChange ? { cancellation: stopRequested ? 'stop-clicked-after-file-change' : 'completed-before-cancel', filesChangedBeforeCancel } : {}) }
      pendingFiles = []
      report.turns.push(turn)
      report.activeTurn = null
      if (turn.evidenceIncomplete) report.evidenceIncomplete = true
      if (terminal.status !== 'succeeded' && !(turnOptions.cancelWhenFilesChange && terminal.status === 'cancelled') && !turnOptions.allowCapabilityFailure) {
        const error = new Error('SUPPLEMENTAL_TURN_TERMINATED')
        error.diagnostic = { stage: name, terminalStatus: terminal.status, terminalReason: terminal.terminal_reason, errorClass: terminal.error_class }
        throw error
      }
      await wait(async () => await send.isVisible() && (turnOptions.allowCapabilityFailure || !await send.isDisabled()), 'composer-released', Math.max(30000, configuration.webuiIdleGraceSeconds * 1000))
      await persist()
      return turn
    },
    async openArtifact(filename) {
      await page.locator('.msg-artifact-chip').filter({ hasText: filename }).last().locator('.msg-artifact-body').click()
      const target = await wait(async () => {
        const state = await nativeObservation(app)
        const matches = state.contents.filter(row => row.visible && row.path.split('/').at(-1) === filename)
        assert.ok(matches.length < 2, 'SUPPLEMENTAL_PREVIEW_AMBIGUOUS')
        return matches[0]
      }, 'artifact-preview')
      selectedId = target.id
    },
    async annotate(targetName, text) {
      const toggle = page.getByRole('button', { name: /Annotate preview|批注预览|Stop annotating|停止批注/ })
      if (await toggle.getAttribute('aria-pressed') !== 'true') await toggle.click()
      const point = await business().point({ role: 'button', name: targetName, allowLink: true })
      const previous = await client.annotationCount()
      await app.evaluate(async ({ webContents }, request) => {
        const contents = webContents.fromId(request.id)
        if (!contents || contents.isDestroyed()) throw new Error('SUPPLEMENTAL_TARGET_LOST')
        const owner = contents.getOwnerBrowserWindow()
        if (!owner?.contentView.children.some(child => child.webContents?.id === contents.id && child.getVisible())) throw new Error('SUPPLEMENTAL_ANNOTATION_OWNER_CHANGED')
        contents.focus()
        const attached = !contents.debugger.isAttached()
        if (attached) contents.debugger.attach('1.3')
        try {
          for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type, x: request.point.x, y: request.point.y, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: 1 })
        } finally { if (attached) contents.debugger.detach() }
      }, { id: selectedId, point })
      const overlay = await wait(async () => app.evaluate(async ({ webContents }, request) => {
        const preview = webContents.fromId(request.previewId)
        if (!preview || preview.isDestroyed()) throw new Error('SUPPLEMENTAL_TARGET_LOST')
        const owner = preview.getOwnerBrowserWindow()
        const previewView = owner?.contentView.children.find(child => child.webContents?.id === preview.id)
        if (!owner || !previewView?.getVisible()) throw new Error('SUPPLEMENTAL_ANNOTATION_OWNER_CHANGED')
        const candidates = owner.contentView.children.filter(child => {
          const contents = child.webContents
          if (!contents || contents === preview || contents.isDestroyed() || !child.getVisible()) return false
          if (contents.getOwnerBrowserWindow()?.id !== owner.id
            || !contents.getURL().startsWith('data:text/html;charset=utf-8,')) return false
          // The pinned Electron runtime omits preload from getLastWebPreferences().
          // Its native getter retains the actual per-WebContents preload identity.
          if (typeof contents._getPreloadScript !== 'function') throw new Error('SUPPLEMENTAL_NATIVE_PRELOAD_INSPECTION_UNAVAILABLE')
          const script = contents._getPreloadScript()
          if (script == null) return false
          if (typeof script !== 'object' || typeof script.filePath !== 'string' || script.type !== 'frame') throw new Error('SUPPLEMENTAL_NATIVE_PRELOAD_METADATA_INVALID')
          return script.filePath.replaceAll('\\', '/').endsWith('/native-workbench-annotation-overlay-preload.cjs')
        })
        if (candidates.length > 1) throw new Error('SUPPLEMENTAL_ANNOTATION_OVERLAY_AMBIGUOUS')
        const contents = candidates[0]?.webContents
        if (!contents) return false
        const ready = await contents.executeJavaScript("document.activeElement?.id==='annotation-body' && Boolean(document.querySelector('#annotation-form[role=dialog] #annotation-submit')) && document.querySelector('#annotation-body')?.dataset.annotationBound==='true'", true)
        if (!ready) return false
        contents.focus(); await contents.insertText(request.body)
        if (await contents.executeJavaScript("document.getElementById('annotation-body').value") !== request.body) throw new Error('ANNOTATION_INPUT_MISMATCH')
        contents.sendInputEvent({ type: 'keyDown', keyCode: 'Enter' }); contents.sendInputEvent({ type: 'keyUp', keyCode: 'Enter' })
        return { ownerId: owner.id, previewId: preview.id, overlayId: contents.id,
          preloadIdentity: 'native-webcontents', ownerFocused: owner.isFocused(), overlayFocused: contents.isFocused() }
      }, { previewId: selectedId, body: text }), 'annotation-editor')
      client.record({ event: 'annotation-owner-bound', ...overlay })
      await wait(async () => await client.annotationCount() === previous + 1, 'annotation-draft')
    },
    annotationCount: () => page.locator('.chat-prompt-annotation-chip').count(),
    async hasButton(name) { try { await business().point({ name, role: 'button', allowLink: true }); return true } catch { return false } },
    visibleTextIncludes: async text => (await business().read()).renderedText.includes(text),
    async hasFinalAnswer(turn) {
      try {
        return Boolean(await wait(async () => (await answers()).some(item => !(turn?.previousAnswerIds || lastAnswerIds).includes(item.id)), 'current-turn-answer-rendered', 10000))
      } catch (error) {
        if (error.message === 'SUPPLEMENTAL_WAIT_EXPIRED') return false
        throw error
      }
    },
    async finalAnswer(turn) { return (await answers()).filter(item => !(turn?.previousAnswerIds || lastAnswerIds).includes(item.id)).map(item => item.text).join('\n') },
    async material() { const artifact = await exportArtifacts(`material-${report.materialSnapshots++}`); const state = await readState(); assert.ok(artifact.complete, 'SUPPLEMENTAL_FILE_EVIDENCE_INCOMPLETE'); return { ...artifact, documents: state.artifact_documents, revisions: state.artifact_revisions, audit: state.artifact_audit_events, publications: state.document_publications || [] } },
    async capture(stage, ids = selectedId ? [selectedId] : []) {
      assert.match(stage, /^[a-z][a-z0-9-]{0,63}$/)
      await page.locator('details.thinking-fold[open] > summary').evaluateAll(elements => elements.forEach(element => element.click()))
      await page.screenshot({ path: join(directory, `${stage}-control.png`), mask: [page.locator('.thinking-block__body,.thinking-fold__body,[data-testid="reasoning-timeline"]')], maskColor: '#e8e8e8' })
      const entry = { stage, native: await nativeObservation(app), artifacts: await exportArtifacts(stage), views: [] }
      for (const id of ids) {
        const views = await capturePageVisualEvidence(app, { pageId: id, outputDirectory: directory, stage: `${stage}-page-${id}` })
        entry.views.push(...views)
        if (views.some(view => !view.complete || !view.restorationMatches)) report.evidenceIncomplete = true
      }
      if (!entry.artifacts.complete || entry.native.incomplete) report.evidenceIncomplete = true
      report.checkpoints.push(entry)
      await persist()
    },
    async restoreOriginalVersion(snapshot) {
      const ids = snapshot.published.filter(bundle => bundle.entrypoint === 'supplemental.html').flatMap(bundle => bundle.headDocumentIds)
      const documents = snapshot.documents.filter(item => ids.includes(item.document_id))
      assert.equal(documents.length, 1, 'SUPPLEMENTAL_ORIGINAL_DOCUMENT_AMBIGUOUS')
      const document = documents[0]
      const revision = snapshot.revisions.find(item => item.revision_id === document.head_revision_id)
      assert.ok(revision, 'SUPPLEMENTAL_ORIGINAL_REVISION_MISSING')
      const before = await readState()
      const beforeDocument = before.artifact_documents.find(row => row.document_id === document.document_id)
      assert.ok(beforeDocument, 'SUPPLEMENTAL_RESTORE_DOCUMENT_MISSING')
      const revisionIds = state => state.artifact_revisions.filter(row => row.document_id === document.document_id).map(row => row.revision_id).sort()
      const originalIds = revisionIds(before)
      const apply = async (repeat) => {
        await page.getByRole('tab', { name: /Versions|版本/ }).click()
        const rows = page.locator('.artifact-document__versions > li')
        const label = page.locator('.artifact-document__list-main > small').filter({
          hasText: new RegExp(`^\\s*(?:Version|版本)\\s*${revision.generation}\\s*$`),
        })
        const original = rows.filter({ has: label })
        await wait(async () => {
          const count = await original.count()
          assert.ok(count < 2, 'SUPPLEMENTAL_ORIGINAL_VERSION_ROW_AMBIGUOUS')
          return count === 1 && await original.isVisible()
        }, 'original-version-row')
        if (repeat) assert.equal(await original.locator('.artifact-document__badge').count(), 1, 'SUPPLEMENTAL_RESTORED_VERSION_NOT_CURRENT')
        const offset = await page.evaluate(() => window.__htmlSupplementalObservation.reviewRequests.length)
        await original.locator('[data-artifact-action="restore-revision"]').click()
        const response = await wait(async () => {
          const requests = await page.evaluate(offset => window.__htmlSupplementalObservation.reviewRequests.slice(offset), offset)
          const restore = requests.find(item => item.method === 'artifacts.revisions.restore' && item.respondedAt)
          if (!restore) return false
          assert.ok(restore.ok, 'SUPPLEMENTAL_RESTORE_RPC_FAILED')
          assert.equal(restore.documentId, document.document_id, 'SUPPLEMENTAL_WRONG_RESTORE_DOCUMENT')
          assert.equal(restore.targetRevisionId, revision.revision_id, 'SUPPLEMENTAL_WRONG_RESTORE_TARGET')
          assert.equal(restore.headRevisionId, revision.revision_id, 'SUPPLEMENTAL_WRONG_REVISION_RESTORED')
          assert.equal(restore.receipt?.requestId, restore.clientRequestId, 'SUPPLEMENTAL_RESTORE_RECEIPT_UNCORRELATED')
          assert.equal(restore.receipt?.resultRevisionId, revision.revision_id, 'SUPPLEMENTAL_WRONG_RESTORE_RECEIPT')
          assert.equal(restore.receipt?.status, 'applied', 'SUPPLEMENTAL_RESTORE_NOT_APPLIED')
          const list = requests.find(item => item.method === 'artifacts.changes.list' && item.documentId === document.document_id && item.ok && item.submittedAt >= restore.respondedAt)
          if (!list) return false
          assert.ok(Array.isArray(list.changeSetIds), 'SUPPLEMENTAL_CHANGES_LIST_MISSING')
          return { ...restore, publiclyListed: list.changeSetIds.includes(restore.receipt.changeSetId) }
        }, repeat ? 'repeat-restore-receipt' : 'restore-receipt')
        const state = await readState()
        const current = state.artifact_documents.find(row => row.document_id === document.document_id)
        assert.equal(current?.head_revision_id, revision.revision_id, 'SUPPLEMENTAL_WRONG_DURABLE_RESTORE_HEAD')
        assert.deepEqual(revisionIds(state), originalIds, 'SUPPLEMENTAL_RESTORE_ADDED_OR_REMOVED_REVISIONS')
        assert.equal(current.generation, beforeDocument.generation, 'SUPPLEMENTAL_RESTORE_CHANGED_GENERATION_HIGHWATER')
        client.record({ event: repeat ? 'repeat-current-version-restored' : 'original-version-restored', response,
          documentId: document.document_id, headRevisionId: current.head_revision_id,
          revisionCount: originalIds.length, generation: current.generation, stateRevision: current.state_revision })
        await persist()
        await page.getByRole('tab', { name: /Preview|预览/ }).click()
        await wait(currentPreview, 'restored-native-page')
        await wait(async () => await client.hasButton('开始体验')
          && await client.visibleTextIncludes('海岬研究')
          && !await client.visibleTextIncludes('暮色研究'), 'restored-preview')
        return { response, stateRevision: current.state_revision }
      }
      const restored = await apply(false)
      const repeated = await apply(true)
      assert.notEqual(repeated.response.clientRequestId, restored.response.clientRequestId, 'SUPPLEMENTAL_REPEAT_REUSED_COMPLETED_REQUEST')
      assert.equal(repeated.response.noOp, true, 'SUPPLEMENTAL_REPEAT_RESTORE_NOT_NOOP')
      assert.equal(repeated.response.publiclyListed, false, 'SUPPLEMENTAL_NOOP_RESTORE_PUBLIC_CHANGE')
      assert.equal(repeated.stateRevision, restored.stateRevision, 'SUPPLEMENTAL_NOOP_RESTORE_CHANGED_STATE')
      return { documentId: document.document_id, revisionId: revision.revision_id,
        revisionCount: originalIds.length, generation: beforeDocument.generation,
        repeated: { noOp: true, publiclyListed: false, stateUnchanged: true, distinctRequest: true } }
    },
    async savePageAttachment() {
      const png = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'), selectedId)
      const path = join(directory, 'synthetic-page-attachment.png')
      await writeFile(path, Buffer.from(png, 'base64'), { mode: 0o600 })
      return path
    },
    async attachFile(path) {
      const name = basename(path)
      const bytes = await readFile(path)
      const previousCount = await page.locator('.attachment-chip').count()
      await page.locator('.chat-composer input[type=file]').setInputFiles(path)
      await wait(async () => {
        const chips = await page.locator('.attachment-chip').evaluateAll(elements => elements.map(element => ({ name: element.querySelector('.attachment-chip__name')?.textContent?.trim(), classes: element.className })))
        const matches = chips.filter(chip => chip.name === name)
        if (chips.length !== previousCount + 1 || matches.length !== 1) return false
        assert.ok(!matches[0].classes.includes('attachment-chip--failed'), 'SUPPLEMENTAL_ATTACHMENT_UPLOAD_FAILED')
        return !matches[0].classes.includes('attachment-chip--busy')
      }, 'attachment-upload')
      const file = { name, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }
      pendingFiles.push(file)
      client.record({ event: 'exact-attachment-uploaded', ...file })
    },
    async imageAdmission() {
      const status = page.locator('#chat-composer-send-status')
      const noticeVisible = await status.isVisible()
      const text = noticeVisible ? await status.innerText() : ''
      const send = page.locator('.chat-send-btn.btn--primary')
      return { blocked: noticeVisible && /图片输入|image input|不支持图片|does not support/i.test(text), noticeVisible, sendDisabled: await send.isDisabled() }
    },
    async selectReviewedNonvisionModel() {
      assert.ok(options.reviewedNonvisionModel, 'SUPPLEMENTAL_REVIEWED_NONVISION_MODEL_REQUIRED')
      return options.selectReviewedNonvisionModel(page, options.reviewedNonvisionModel)
    },
    async findPagesAtFixtureUrl() {
      return app.evaluate(({ webContents }, url) => webContents.getAllWebContents().filter(contents => !contents.isDestroyed() && contents.getURL() === url).map(contents => ({ id: contents.id })).sort((a, b) => a.id - b.id), options.fixtureUrl)
    },
    async pageInventory() {
      const state = await nativeObservation(app)
      assert.ok(!state.incomplete, 'SUPPLEMENTAL_NATIVE_INVENTORY_INCOMPLETE')
      const urls = await app.evaluate(({ webContents }) => webContents.getAllWebContents().filter(contents => !contents.isDestroyed()).map(contents => ({ id: contents.id, url: contents.getURL() })))
      const urlHashes = new Map(urls.map(row => [row.id, createHash('sha256').update(row.url).digest('hex')]))
      return {
        contents: state.contents.map(({ id, type, destroyed, ownerId, protocol, path }) => ({ id, type, destroyed, ownerId, protocol, path, urlSha256: urlHashes.get(id) })).sort((a, b) => a.id - b.id),
        destroyedIds: state.events.filter(event => event.event === 'destroyed').map(event => event.id).sort((a, b) => a - b),
      }
    },
    async activatePage(target) {
      const tabs = page.locator('.workbench-host__tabs [role=tab],.workbench-host__tab')
      const count = await tabs.count()
      for (let index = 0; index < count; index += 1) {
        const tab = tabs.nth(index)
        const wasSelected = await tab.getAttribute('aria-selected') === 'true'
        const before = await nativeObservation(app)
        const visibleIds = new Set(before.contents.filter(row => row.visible).map(row => row.id))
        await tab.click()
        const state = wasSelected ? await nativeObservation(app) : await wait(async () => {
          const current = await nativeObservation(app)
          return current.contents.some(row => row.visible && !visibleIds.has(row.id)) && current
        }, 'native-tab-activation')
        if (state.contents.some(row => row.id === target.id && row.visible)) { selectedId = target.id; return }
      }
      throw new Error('SUPPLEMENTAL_PAGE_TAB_UNAVAILABLE')
    },
    clickInPage: (id, name) => business(id).click(name),
    async preparePageState(target, note, scrollY) {
      await client.activatePage(target)
      await business(target.id).fill('页面备注', note)
      await app.evaluate(async ({ webContents }, request) => {
        const contents = webContents.fromId(request.id)
        contents.sendInputEvent({ type: 'mouseWheel', x: 50, y: 100, deltaX: 0, deltaY: -request.scrollY, canScroll: true })
      }, { id: target.id, scrollY })
      await wait(async () => (await client.pageMemory([target]))[0].scrollY > 0, 'independent-scroll-state')
    },
    async reenter() {
      selectedId = null
      await page.reload()
      await wait(async () => await page.locator('.chat-textarea').isVisible(), 'reentered-composer', 60000)
      assert.ok(await page.evaluate(() => Boolean(window.__htmlSupplementalObservation)), 'SUPPLEMENTAL_OBSERVER_NOT_REINSTALLED')
      await client.openArtifact('supplemental.html')
    },
    async verifyButtonInteraction(name, title) {
      const driver = business()
      assert.ok(!(await driver.read()).renderedText.includes(title), 'SUPPLEMENTAL_INFORMATION_ALREADY_VISIBLE')
      await driver.click(name, { allowLink: true })
      const opened = await driver.settle(state => state.renderedText.includes(title))
      assert.ok(opened.renderedText.includes(title), 'SUPPLEMENTAL_BUTTON_DID_NOT_OPEN_INFORMATION')
      await driver.click('关闭')
      const closed = await driver.settle(state => !state.renderedText.includes(title))
      assert.ok(!closed.renderedText.includes(title), 'SUPPLEMENTAL_INFORMATION_DID_NOT_CLOSE')
      return true
    },
    async pageMemory(pages) {
      return Promise.all(pages.map(async page => ({ id: page.id, ...await evaluatePage(page.id, '({count:Number(document.querySelector("[role=status]").textContent),instanceId:window.syntheticInstanceId,note:document.querySelector("input").value,scrollY})') })))
    },
  }
  return client
}
