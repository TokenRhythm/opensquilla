import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

const PAGE_BRIEF = '为虚构的「海岬研究」制作完整中文产品页面，含主标题「海岬研究」、产品介绍、清晰分开的体验与团队价格区域。首屏按钮为「开始体验」，团队区域按钮为「了解团队版」，点击分别打开标题为「体验信息」与「团队信息」的本地信息框，每个信息框均有「关闭」按钮，关闭后对应标题从页面可见内容消失。页面要有吸引力、响应式、无外部提交。请实现并交付名为 supplemental.html 的可打开HTML入口产物。'

export const SUPPLEMENTAL_CASES = Object.freeze({
  'multiple-annotations': {
    generation: PAGE_BRIEF,
    annotations: [
      { target: '开始体验', text: '把这个首屏按钮文案改为「立即开始」，让主按钮更突出并保持点击信息框可用。' },
      { target: '了解团队版', text: '把这个团队按钮文案改为「查看团队方案」，让它与首屏主按钮形成清楚的视觉主次，保持点击信息框可用。' },
    ],
    followup: '请一次完成刚才两处批注，交付更新后的同一个页面。',
  },
  'same-url-memory': {
    generation: '请在右侧的两个独立标签中各打开一次这个页面：{url}。保留这两个页面实例，我接下来会分别操作。',
    followup: '这两个同地址的页面具有不同的内存计数状态。请只在当前显示计数为 2 的那个页面上点击一次「增加计数」。不要刷新、重新导航、另开页面或修改文件；另一个页面维持原状态。',
  },
  'attachment-capability': {
    generation: PAGE_BRIEF,
    annotation: '结合我附上的这张当前页面截图，说明这个按钮的视觉层级，并把按钮文案改为「开始探索」。保留页面其他内容和交互。',
    nonvisionQuestion: '请查看我附上的页面截图，用一句话描述主按钮；这次只回答，不修改页面。',
  },
  'ask-without-edit': {
    generation: PAGE_BRIEF,
    annotation: '只解释这个按钮当前的文字、位置与点击行为，不修改页面、不新增版本。',
    followup: '请回答刚才的页面批注，这次仅说明，不改变任何产物。',
  },
  'cancel-and-recover': {
    generation: PAGE_BRIEF,
    interruptible: '请把这个页面扩展成完整的研究团队展示站点，增加研究方向、项目案例、成员介绍和常见问题，保持现有产品信息与交互。请认真完成设计和实现。',
    recovery: '上一轮已被我停止。请基于当前实际页面继续，把首屏按钮文案设为「继续探索」，确保页面可打开、按钮可点击，并交付当前结果。',
  },
  'restore-and-edit': {
    generation: PAGE_BRIEF,
    change: '请把页面主标题改为「暮色研究」，把首屏按钮改为「进入暮色」，其他内容与交互保留，并交付更新后的页面。',
    annotation: '在我刚恢复的版本上，把这个按钮文案改为「加入名单」，保持恢复后的主标题「海岬研究」和其他内容。',
    followup: '请完成刚才的批注，继续编辑当前恢复后的版本并交付。',
  },
})

export function supplementalPromptHash(caseId, fixtureUrl = '') {
  assert.ok(Object.hasOwn(SUPPLEMENTAL_CASES, caseId), 'UNKNOWN_SUPPLEMENTAL_CASE')
  const prompts = JSON.parse(JSON.stringify(SUPPLEMENTAL_CASES[caseId]).replaceAll('{url}', fixtureUrl))
  return { prompts, sha256: createHash('sha256').update(JSON.stringify(prompts)).digest('hex') }
}

function material(snapshot) {
  const selected = snapshot.working?.length ? snapshot.working : snapshot.published.filter(item => item.headDocumentIds?.length)
  return selected.flatMap(bundle => bundle.files.map(file => [bundle.entrypoint, file.path, file.sha256])).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
}

export function supplementalHeads(snapshot) {
  return (snapshot.documents || []).map(row => [row.document_id, row.head_revision_id]).sort((a, b) => a[0].localeCompare(b[0]))
}

/**
 * The adapter owns ordinary client startup, composer submission, budget accounting,
 * screenshots, and cleanup. These journeys request outcomes and inspect actual UI
 * state; they do not enforce an LLM tool name or tool-call sequence.
 */
export async function runSupplementalScenario(caseId, client) {
  const { prompts, sha256 } = supplementalPromptHash(caseId, client.fixtureUrl)
  client.record({ event: 'scenario-prompts', caseId, prompts, sha256 })
  const check = (name, passed, detail = {}) => client.check({ name, passed: Boolean(passed), ...detail })

  if (caseId === 'same-url-memory') {
    await client.send('open-two-pages', prompts.generation)
    const pages = await client.findPagesAtFixtureUrl()
    check('two-distinct-page-identities', pages.length === 2 && pages[0].id !== pages[1].id)
    assert.equal(pages.length, 2, 'TWO_PAGE_INSTANCES_REQUIRED')
    await client.activatePage(pages[0])
    await client.clickInPage(pages[0].id, '增加计数')
    await client.activatePage(pages[1])
    await client.clickInPage(pages[1].id, '增加计数')
    await client.clickInPage(pages[1].id, '增加计数')
    await client.preparePageState(pages[0], 'first page note', 120)
    await client.preparePageState(pages[1], 'second page note', 240)
    const inventoryBefore = await client.pageInventory()
    const before = await client.pageMemory(pages)
    check('independent-memory-before-agent-action', before[0].count === 1 && before[1].count === 2)
    await client.capture('two-pages-before', pages.map(page => page.id))
    await client.send('operate-existing-page', prompts.followup)
    const inventoryAfter = await client.pageInventory()
    const remaining = await client.findPagesAtFixtureUrl()
    check('same-url-page-inventory-unchanged', JSON.stringify(remaining) === JSON.stringify(pages))
    check('whole-native-inventory-unchanged', JSON.stringify(inventoryAfter) === JSON.stringify(inventoryBefore), { before: inventoryBefore, after: inventoryAfter })
    const after = await client.pageMemory(pages)
    check('only-intended-page-changed', after[0].count === 1 && after[1].count === 3)
    check('same-url-identities-not-recreated', pages.every((page, index) => after[index].id === page.id && after[index].instanceId === before[index].instanceId))
    check('page-input-values-preserved', before[0].note === 'first page note' && before[1].note === 'second page note' && after.every((state, index) => state.note === before[index].note))
    check('untargeted-page-scroll-preserved', before[0].scrollY > 0 && after[0].scrollY === before[0].scrollY, { before: before[0].scrollY, after: after[0].scrollY })
    client.record({ event: 'targeted-page-scroll', before: before[1].scrollY, after: after[1].scrollY, reason: 'Locating and clicking the requested button may scroll only the target page.' })
    await client.capture('two-pages-after', pages.map(page => page.id))
    return
  }

  await client.send('generation', prompts.generation)
  await client.openArtifact('supplemental.html')
  check('generated-experience-interaction', await client.verifyButtonInteraction('开始体验', '体验信息'))
  check('generated-team-interaction', await client.verifyButtonInteraction('了解团队版', '团队信息'))
  const generation = await client.material()
  await client.capture('generation')

  if (caseId === 'multiple-annotations') {
    for (const annotation of prompts.annotations) await client.annotate(annotation.target, annotation.text)
    check('two-visible-composer-drafts', await client.annotationCount() === 2)
    await client.capture('two-drafts')
    const turn = await client.send('two-annotations', prompts.followup)
    check('two-annotations-in-accepted-message', turn.annotationCount === 2)
    check('both-results-visible', await client.hasButton('立即开始') && await client.hasButton('查看团队方案'))
    check('updated-experience-interaction', await client.verifyButtonInteraction('立即开始', '体验信息'))
    check('updated-team-interaction', await client.verifyButtonInteraction('查看团队方案', '团队信息'))
    await client.capture('two-annotations-completed')
    return
  }

  if (caseId === 'ask-without-edit') {
    const before = await client.material()
    await client.annotate('开始体验', prompts.annotation)
    const turn = await client.send('answer-only', prompts.followup)
    const after = await client.material()
    check('artifact-and-working-bytes-unchanged', JSON.stringify(material(before)) === JSON.stringify(material(after)))
    check('document-heads-unchanged', JSON.stringify(supplementalHeads(before)) === JSON.stringify(supplementalHeads(after)))
    check('answer-rendered-for-current-turn', await client.hasFinalAnswer(turn))
    await client.capture('answer-only')
    return
  }

  if (caseId === 'cancel-and-recover') {
    const before = await client.material()
    const cancelled = await client.send('interruptible-change', prompts.interruptible, { cancelWhenFilesChange: true })
    const exercised = cancelled.terminalStatus === 'cancelled' && cancelled.cancellation === 'stop-clicked-after-file-change' && cancelled.filesChangedBeforeCancel
    check('cancelled-after-real-file-change', exercised, { terminalStatus: cancelled.terminalStatus, cancellation: cancelled.cancellation })
    if (!exercised) {
      client.record({ event: 'branch-not-exercised', branch: 'cancellation-after-write', reason: 'no-running-write-interruption-confirmed' })
      return
    }
    const stopped = await client.material()
    check('cancelled-file-changes-remain-observable', JSON.stringify(material(stopped)) !== JSON.stringify(material(before)))
    const oldEvents = new Set((before.audit || []).map(row => row.event_id))
    const published = (stopped.audit || []).filter(row => !oldEvents.has(row.event_id) && (row.event_type === 'document.published' || row.event_id.startsWith('working-publish:')))
    const beforeHeads = new Map(supplementalHeads(before))
    check('cancelled-heads-match-published-facts', stopped.documents.every(row => {
      if (row.head_revision_id === beforeHeads.get(row.document_id)) return true
      return published.some(event => event.document_id === row.document_id && event.revision_id === row.head_revision_id)
    }), { publications: published, before: supplementalHeads(before), stopped: supplementalHeads(stopped) })
    client.record({ event: 'cancelled-material-facts', taskId: cancelled.taskId, files: material(stopped), heads: supplementalHeads(stopped), publications: stopped.publications || [], audit: published })
    await client.capture('after-cancel')
    await client.reenter()
    const reentered = await client.material()
    check('reentry-preserves-stopped-files', JSON.stringify(material(reentered)) === JSON.stringify(material(stopped)))
    check('reentry-preserves-stopped-heads', JSON.stringify(supplementalHeads(reentered)) === JSON.stringify(supplementalHeads(stopped)))
    await client.send('recovery', prompts.recovery)
    check('recovery-page-available', await client.hasButton('继续探索'))
    check('recovery-button-interaction', await client.verifyButtonInteraction('继续探索', '体验信息'))
    await client.capture('recovery')
    return
  }

  if (caseId === 'restore-and-edit') {
    const original = generation
    const documentIds = original.published.filter(bundle => bundle.entrypoint === 'supplemental.html').flatMap(bundle => bundle.headDocumentIds)
    const documents = original.documents.filter(row => documentIds.includes(row.document_id))
    assert.equal(documents.length, 1, 'SUPPLEMENTAL_ORIGINAL_DOCUMENT_AMBIGUOUS')
    const documentId = documents[0].document_id
    const target = documents[0].head_revision_id
    const document = snapshot => snapshot.documents.find(row => row.document_id === documentId)
    const revisions = snapshot => snapshot.revisions.filter(row => row.document_id === documentId)
    const revisionIds = snapshot => revisions(snapshot).map(row => row.revision_id).sort()
    await client.send('new-version', prompts.change)
    check('new-version-visible', await client.visibleTextIncludes('暮色研究'))
    const changed = await client.material()
    check('new-version-persisted', document(changed)?.head_revision_id !== target && revisions(changed).length > revisions(original).length)
    const restoredByUi = await client.restoreOriginalVersion(original)
    check('original-heading-restored', await client.visibleTextIncludes('海岬研究') && !await client.visibleTextIncludes('暮色研究'))
    const restored = await client.material()
    check('restored-head-is-original-revision', document(restored)?.head_revision_id === target && restoredByUi?.documentId === documentId && restoredByUi?.revisionId === target)
    check('restore-preserves-version-history', JSON.stringify(revisionIds(restored)) === JSON.stringify(revisionIds(changed)))
    check('restore-preserves-generation-highwater', document(restored)?.generation === document(changed)?.generation)
    check('repeat-current-version-is-private-noop', restoredByUi?.repeated?.noOp === true && restoredByUi.repeated.publiclyListed === false && restoredByUi.repeated.stateUnchanged === true && restoredByUi.repeated.distinctRequest === true)
    check('restored-original-bytes', JSON.stringify(material(original)) === JSON.stringify(material(restored)))
    await client.capture('restored')
    await client.annotate('开始体验', prompts.annotation)
    await client.send('edit-restored-version', prompts.followup)
    const edited = await client.material()
    const restoredIds = new Set(revisionIds(restored))
    const newRevisions = revisions(edited).filter(row => !restoredIds.has(row.revision_id)).sort((a, b) => a.generation - b.generation)
    const newParents = new Map(newRevisions.map(row => [row.revision_id, row.parent_revision_id]))
    let ancestor = document(edited)?.head_revision_id
    const visited = new Set()
    while (newParents.has(ancestor) && !visited.has(ancestor)) {
      visited.add(ancestor)
      ancestor = newParents.get(ancestor)
    }
    check('restored-edit-creates-version-from-restored-head', newRevisions.length > 0 && newRevisions[0].parent_revision_id === target && visited.size > 0 && ancestor === target && document(edited)?.generation > document(restored)?.generation && [...restoredIds].every(id => revisionIds(edited).includes(id)), { restoredRevisionId: target, newRevisions: newRevisions.map(({ revision_id, parent_revision_id, generation }) => ({ revision_id, parent_revision_id, generation })) })
    check('edit-applies-to-restored-version', await client.hasButton('加入名单') && await client.visibleTextIncludes('海岬研究') && !await client.visibleTextIncludes('暮色研究'))
    check('restored-edit-interaction', await client.verifyButtonInteraction('加入名单', '体验信息'))
    await client.capture('restored-then-edited')
    return
  }

  const screenshot = await client.savePageAttachment()
  await client.attachFile(screenshot)
  await client.annotate('开始体验', prompts.annotation)
  const turn = await client.send('attachment-and-annotation', '请处理我刚附上的截图和页面批注，完成按钮更新。')
  check('attachment-and-context-accepted-together', turn.attachmentCount > 0 && turn.annotationCount === 1)
  check('attachment-edit-visible', await client.hasButton('开始探索'))
  check('attachment-edit-interaction', await client.verifyButtonInteraction('开始探索', '体验信息'))
  await client.capture('attachment-completed')
  const selection = await client.selectReviewedNonvisionModel()
  client.record({ event: 'nonvision-model-ui-selection', ...selection, modelOverrideReason: 'required nonvision capability scenario' })
  check('reviewed-nonvision-model-selected', selection.selected === true && selection.supportsVision === false && Boolean(selection.model), selection)
  if (!selection.selected || selection.supportsVision !== false) {
    client.record({ event: 'branch-not-exercised', branch: 'nonvision-image-capability', reason: 'reviewed-model-not-selected' })
    return
  }
  await client.attachFile(screenshot)
  await client.fillComposer(prompts.nonvisionQuestion)
  const admission = await client.imageAdmission()
  if (admission.blocked) {
    check('unsupported-image-send-blocked', admission.noticeVisible && admission.sendDisabled, admission)
    await client.capture('nonvision-admission')
  } else {
    const turn = await client.send('nonvision-image-request', prompts.nonvisionQuestion, { allowCapabilityFailure: true })
    const hasAnswer = turn.terminalStatus === 'succeeded' && await client.hasFinalAnswer(turn)
    const answer = await client.finalAnswer(turn)
    const events = (turn.events || []).filter(event => turn.taskId && event.taskId === turn.taskId)
    const codes = [turn.rejectionCode, turn.errorClass, turn.terminalReason, ...events.map(event => event.code)].filter(Boolean)
    const capabilityCode = codes.some(code => /(?:image|vision).*(?:unsupported|not.supported|unavailable)|(?:unsupported|not.supported).*(?:image|vision)/i.test(code))
    const capabilityAnswer = [
      /(?:无法|不能|未能)(?:直接)?(?:查看|读取|识别|处理|分析)[^。！？；\n]{0,16}(?:图像|图片|截图)/,
      /(?:模型|我)[^。！？；\n]{0,12}(?:不支持|没有|不具备|缺少)(?:视觉(?:能力)?|(?:图像|图片|截图)(?:输入|识别|处理|分析)(?:能力)?)/,
      /(?:作为(?:一个|一款)?|(?:模型|我)(?:是|为|属于)(?:一个|一款)?)(?:纯文本|仅文本|只处理文本)(?:模型)?/,
      /\b(?:cannot|can['’]t|unable to)\s+(?:directly\s+)?(?:see|view|read|process|analy[sz]e|inspect)\b[^.!?;\n]{0,36}\b(?:images?|screenshots?|pictures?)\b/i,
      /\b(?:(?:this|the (?:current|selected)) model is|I(?: am|['’]m))\s+(?:an?\s+)?text[- ]only\b/i,
      /\b(?:this model|the (?:current|selected) model|I)\s+(?:(?:can\s+only|only)\s+process(?:es)?\s+text|(?:does not|doesn't|do not|don't)\s+(?:support|have)\s+(?:vision|visual capabilities|image input))\b/i,
    ].some(pattern => pattern.test(answer))
    const done = events.filter(event => event.event === 'session.event.done').at(-1)
    const executionModels = done?.executionModels || []
    const providerStarted = Boolean(turn.providerEventAt) || events.some(event => event.event === 'session.event.provider_activity' || event.event === 'session.event.tool_use_start')
    const rejectedBeforeCall = capabilityCode && !providerStarted && !executionModels.length && !done?.model && ['rejected', 'failed'].includes(turn.terminalStatus)
    const observedModel = typeof done?.model === 'string' ? done.model : null
    const selectedModelExecuted = observedModel === selection.model && executionModels.length > 0 && executionModels.every(model => model === selection.model)
    const modelEvidence = rejectedBeforeCall || selectedModelExecuted
    check('nonvision-selected-model-observed-or-rejected-before-call', modelEvidence, { selectedModel: selection.model, observedModel, executionModels, rejectedBeforeCall })
    if (!modelEvidence) client.record({ event: 'branch-not-exercised', branch: 'nonvision-image-capability', reason: observedModel && observedModel !== selection.model || executionModels.some(model => model && model !== selection.model) ? 'different-model-executed' : 'execution-model-evidence-missing' })
    check('nonvision-capability-limitation-observed', capabilityCode || (hasAnswer && capabilityAnswer), { terminalStatus: turn.terminalStatus, codes, answerSha256: createHash('sha256').update(answer).digest('hex') })
    await client.capture('nonvision-capability-result')
  }
}
