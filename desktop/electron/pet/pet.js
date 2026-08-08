'use strict';

// 这只宠只盯 OpenSquilla（单宠模式，无双宠分流）。
const AGENT = 'all';
const AGENT_LABEL = 'OpenSquilla';

const stage = document.getElementById('stage');
const pixel = document.getElementById('pixel');
const mascot = document.getElementById('mascot');
const mascotImg = document.getElementById('mascot-img');
const cat = document.getElementById('cat');

// 图标款按状态换眼神（每种状态一张只改眼睛的图）
const MASCOT_EYES = {
  working: 'mascot-work.png', // 干活：对着笔记本敲代码 + 咖啡（整幅工作场景）
  roam: 'mascot.png',        // 旅行：使用完整站姿，容器负责走路动画
  juggling: 'mascot-work.png', // 并行子任务：无独立图，回落到干活
  sweeping: 'mascot-work.png', // 清理上下文：无独立图，回落到干活
  loafing: 'mascot-sleep.png', // 间隙摸鱼：无独立图，回落到闭眼待机
  idle: 'mascot-sleep.png',   // 无任务：闭眼
  sleeping: 'mascot-sleep.png',
  thinking: 'mascot-think.png', // 思考：往上看
  happy: 'mascot-happy.png',  // 完成：^^ 笑眼
  greet: 'mascot-happy.png',
  talking: 'mascot-happy.png',
  waiting: 'mascot-wait.png', // 等你处理：瞪大
  needsinput: 'mascot-think.png', // 等你回复：往上看(期待)
  error: 'mascot-wait.png',
  // 情绪短暂态 → 就近回落（专属图未画）
  loved: 'mascot-happy.png',
  excited: 'mascot-happy.png',
  sad: 'mascot-wait.png',
  sorry: 'mascot-wait.png',
  puzzled: 'mascot-think.png',
};
function updateMascotEyes(s) {
  if (!mascotImg) return;
  const f = MASCOT_EYES[s] || 'mascot.png';
  if (!mascotImg.getAttribute('src').endsWith(f)) mascotImg.src = 'assets/' + f;
}

// 月薪喵（cat）：每个状态一张 meme GIF（原作者：抖音 @月薪喵）
const catImg = document.getElementById('cat-img');
const CAT_STATES = {
  idle: 'cat-idle.gif',           // 端坐待命/放空发呆
  roam: 'cat-roam.gif',           // 直立闲逛巡逻
  working: 'cat-working.gif',     // 头戴发带挥拳打气：干活
  thinking: 'cat-thinking.gif',   // 疯狂摇头想方案：思考
  lookout: 'cat-thinking-2.gif',  // 掠夺后朝远处看战果
  talking: 'cat-talking.gif',     // 捧着手机疯狂打字：回应中
  juggling: 'cat-juggling.gif',   // 双手抛球：并行子任务
  sweeping: 'cat-sweeping.gif',   // 花洒下淋浴冲洗：压缩/清理
  waiting: 'cat-waiting.gif',     // 紧张等待：等你授权
  needsinput: 'cat-needsinput.gif', // 头顶问号：等你回复
  happy: 'cat-happy.gif',         // 举手开心：完成庆祝
  greet: 'cat-greet.gif',         // 被闹钟惊醒弹起：新会话
  attention: 'cat-attention.gif', // 捂眼偷看：需要注意
  sleeping: 'cat-sleeping.gif',   // 盖被睡觉
  error: 'cat-error.gif',         // 倒地灵魂出窍：崩溃出错
  chou: 'cat-chou.gif',           // 捂住鼻子扇风：代码臭臭
  loafing: 'cat-loafing.gif',     // 从水里露头：摸鱼
  // 情绪短暂态 → 就近映射，别回落到摸鱼 idle 图（表情和文案会打架）
  loved: 'cat-happy-2.gif',       // 抱小橘猫：被夸/治愈
  excited: 'cat-happy-3.gif',     // 原地转圈跳舞：兴奋庆祝
  sad: 'cat-sad.gif',             // 喷泉大哭：惹你生气了
  crying: 'cat-crying.gif',       // 捧脸流泪：委屈/感动
  sorry: 'cat-sorry.gif',         // 双手合十鞠躬：道歉
  puzzled: 'cat-needsinput-2.gif', // 耸肩茫然：疑惑
  anxious: 'cat-anxious.gif',     // 抱紧自己摇晃：紧张焦虑
  angry: 'cat-angry.gif',         // 全身变红冒蒸汽：生气
  eating: 'cat-eating.gif',       // 火锅大快朵颐：加载中
  drinking: 'cat-drinking.gif',   // 吸管喝奶茶：休息
  sick: 'cat-sick.gif',           // 流鼻涕：过载/生病
};
// working/thinking 是停留最久的两个状态 → 多张姿态轮换：进入时换下一张，
// 持续期间每 60s 也换一张。大上下文会话推理一次要几分钟，单张静止图
// 播几分钟观感像卡死，轮换让「还活着」看得见。
const CAT_POOLS = {
  working: [
    'cat-working.gif',   // 挥拳打气
    'cat-working-2.gif', // 吐舌头兴奋敲键盘
    'cat-working-3.gif', // 仰卧起坐热身
    'cat-working-4.gif', // 叼着饭干活
    'cat-working-5.gif', // 挂着输液袋拼命
    'cat-working-6.gif', // 猛按「撤回」按钮
    'cat-working-7.gif', // 枕着鱼形抱枕撑着干
    'cat-working-8.gif', // 商务范正经上班
    'cat-working-9.gif', // 数位板画画创作
    'cat-working-10.gif', // 戴发带出拳冲刺
  ],
  thinking: [
    'cat-thinking.gif',   // 疯狂摇头想方案
    'cat-thinking-2.gif', // 躺着想：头顶「浮云」思考泡
  ],
  sleeping: [
    'cat-sleeping.gif',   // 盖被子睡
    'cat-sleeping-2.gif', // 裹毯子抱枕头
    'cat-sleeping-3.gif', // 从白天睡到月亮
    'cat-sleeping-4.gif', // 戴睡帽旁边有钟表
  ],
  loafing: [
    'cat-loafing.gif',   // 从水里露头
    'cat-loafing-2.gif', // 仰面四脚朝天
    'cat-loafing-3.gif', // 逐渐瘫软趴平
    'cat-loafing-4.gif', // 彻底贴地躺平
    'cat-loafing-5.gif', // 马桶上刷手机
    'cat-loafing-6.gif', // 塞进玻璃罐
    'cat-loafing-7.gif', // 抱着小橘子打滚
    'cat-loafing-8.gif', // 仰面翻滚四脚乱蹬
    'cat-loafing-9.gif', // 盘腿打掌机
  ],
  idle: [
    'cat-idle.gif',   // 端坐待命
    'cat-idle-2.gif', // 捧手机低头刷
    'cat-idle-3.gif', // 伸懒腰打哈欠
    'cat-idle-4.gif', // 目视前方待命
    'cat-idle-5.gif', // 伸脖子左右张望
    'cat-idle-6.gif', // 圆滚滚背影发呆
    'cat-idle-7.gif', // 睡前蓝绿光趴床
    'cat-idle-8.gif', // 凑近闻小花
  ],
  happy: [
    'cat-happy.gif',   // 举手开心
    'cat-happy-2.gif', // 抱小橘猫
    'cat-happy-3.gif', // 原地转圈跳舞
    'cat-happy-4.gif', // 食物从天而降
    'cat-happy-5.gif', // 摸脸头顶冒爱心
    'cat-happy-6.gif', // 张手舞足蹈欢呼
    'cat-happy-7.gif', // 双手胸前害羞开心
    'cat-happy-8.gif', // 撑脸周围闪星星
  ],
  error: [
    'cat-error.gif',   // 倒地灵魂出窍
    'cat-error-2.gif', // 头顶扣水桶
    'cat-error-3.gif', // 红底白叉停止手势
    'cat-error-4.gif', // 捂耳朵不想听
  ],
  attention: [
    'cat-attention.gif',   // 捂眼偷看
    'cat-attention-2.gif', // 望远镜+西瓜围观
    'cat-attention-3.gif', // 打响指提醒
    'cat-attention-4.gif', // 被闪电吓到
  ],
  needsinput: [
    'cat-needsinput.gif',   // 头顶问号
    'cat-needsinput-2.gif', // 耸肩茫然
  ],
  sweeping: [
    'cat-sweeping.gif',   // 花洒淋浴冲洗
    'cat-sweeping-2.gif', // 浴帽搓澡
  ],
  roam: [
    'cat-roam.gif',   // 直立闲逛
    'cat-roam-2.gif', // 四脚残影飞奔
  ],
  sad: [
    'cat-sad.gif',   // 喷泉大哭
    'cat-sad-2.gif', // 墙角抱膝
    'cat-sad-3.gif', // 垫子上眼泪成河
  ],
  eating: [
    'cat-eating.gif',   // 火锅大快朵颐
    'cat-eating-2.gif', // 吸溜面条
    'cat-eating-3.gif', // 汉堡+饮料
    'cat-eating-4.gif', // 饭盆暴风吸入
  ],
};
const POOL_ROTATE_MS = 10 * 1000;
let poolIdx = 0;
let poolRot = null;
let memeWorkReaction = null;
let memeWorkReactionTimer = null;
let lootActionVisual = null;
let lootActionDirection = 0;
let lootActionTimer = null;
function finishMemeWorkReaction(refresh = false) {
  memeWorkReaction = null;
  clearTimeout(memeWorkReactionTimer);
  memeWorkReactionTimer = null;
  // 高压工作姿态结束后，从当前真实状态的动画池随机接一张，避免每次
  // 都机械地回到同一个默认动作。睡眠唤醒或测试时钟跨越期限后，
  // activeMemeWorkVisual 的惰性到期路径也会走这里。
  const pool = CAT_POOLS[state];
  if (pool && pool.length) poolIdx = Math.floor(Math.random() * pool.length);
  if (refresh && skin === 'cat') updateCat(state);
}
function activeMemeWorkVisual(s) {
  if (!memeWorkReaction) return null;
  if (perfNow() >= memeWorkReaction.until) {
    finishMemeWorkReaction(false);
    return null;
  }
  return memeWorkReaction.activeStates.has(s) ? memeWorkReaction.visualState : null;
}
function updateCat(s) {
  if (!catImg) return;
  const workVisual = activeMemeWorkVisual(s);
  const pool = (workVisual || lootActionVisual) ? null : CAT_POOLS[s];
  const f = lootActionVisual
    ? (CAT_STATES[lootActionVisual] || CAT_STATES.attention)
    : workVisual
    ? (CAT_STATES[workVisual] || CAT_STATES.working)
    : (pool ? pool[poolIdx % pool.length] : (CAT_STATES[s] || CAT_STATES.idle));
  if (!catAssetMatches(f)) catImg.src = 'assets/cat/' + f;
  if (pool) {
    if (!poolRot) {
      poolRot = setInterval(() => {
        const cur = CAT_POOLS[state];
        if (!cur || skin !== 'cat') return;
        poolIdx++;
        catImg.src = 'assets/cat/' + cur[poolIdx % cur.length];
      }, POOL_ROTATE_MS);
    }
  } else if (poolRot) {
    clearInterval(poolRot);
    poolRot = null;
    poolIdx++; // 下次进入轮换态直接是下一张
  }
}
function catAssetMatches(filename) {
  if (!catImg) return false;
  try {
    return new URL(catImg.src, window.location.href).pathname.endsWith('/' + filename);
  } catch {
    return String(catImg.getAttribute('src') || '').split(/[?#]/, 1)[0].endsWith(filename);
  }
}
function lootVisualNeedsMirror(visualState, direction) {
  const targetDirection = Number(direction) === 1 ? 1 : -1;
  // 这里的“方向”是猫主体位于画布哪一侧，而不是素材里的显示器朝哪边。
  // attention 原图的猫在左、显示器在右；若把显示器方向当成猫的方向，
  // 向左掠夺时就会错误镜像成“显示器靠 Codex、猫退到很远的右侧”。
  // 三张掠夺动作的猫主体原生都在左侧，向右动作时才统一镜像。
  const nativeDirection = -1;
  return targetDirection !== nativeDirection;
}
function setLootActionVisual(visualState, direction) {
  lootActionVisual = visualState;
  lootActionDirection = Number(direction) === 1 ? 1 : -1;
  cat.classList.toggle('loot-action-mirrored',
    lootVisualNeedsMirror(visualState, lootActionDirection));
  if (skin === 'cat') updateCat(state);
}
function startLootCaptureVisual(direction) {
  clearTimeout(lootActionTimer);
  lootActionTimer = null;
  setLootActionVisual('attention', direction);
}
function startLootKick(direction) {
  clearTimeout(lootActionTimer);
  lootActionTimer = null;
  setLootActionVisual('roam', direction);
  // 同一张 GIF 连续触发时浏览器默认会沿用上次播放进度。加一次仅用于本轮
  // 演出的查询串，确保蓄力从第一帧开始，后端按真实 helper 时序同步出脚。
  if (skin === 'cat' && catImg) {
    catImg.src = 'assets/cat/' + CAT_STATES.roam + '?loot-kick=' + Date.now();
  }
}
function startLootLookout(direction, durationMs = 6000) {
  clearTimeout(lootActionTimer);
  setLootActionVisual('lookout', direction);
  lootActionTimer = setTimeout(() => {
    lootActionTimer = null;
    stopLootActionVisual();
  }, durationMs);
}
function stopLootActionVisual() {
  clearTimeout(lootActionTimer);
  lootActionTimer = null;
  lootActionVisual = null;
  lootActionDirection = 0;
  cat.classList.remove('loot-action-mirrored');
  if (skin === 'cat') updateCat(state);
}
function clearMemeWorkReaction(refresh = true) {
  const wasActive = !!memeWorkReaction;
  memeWorkReaction = null;
  clearTimeout(memeWorkReactionTimer);
  memeWorkReactionTimer = null;
  if (wasActive && refresh && skin === 'cat') updateCat(state);
}
function startMemeWorkReaction(work) {
  clearMemeWorkReaction(false);
  if (!work || !work.visualState || !Array.isArray(work.activeStates) || !work.activeStates.length) {
    if (skin === 'cat') updateCat(state);
    return false;
  }
  const durationMs = Math.max(1000, Math.min(120000, Number(work.durationMs) || 30000));
  memeWorkReaction = {
    visualState: work.visualState,
    activeStates: new Set(work.activeStates),
    until: perfNow() + durationMs,
  };
  if (skin === 'cat') updateCat(state);
  memeWorkReactionTimer = setTimeout(() => {
    if (!memeWorkReaction || perfNow() < memeWorkReaction.until) return;
    finishMemeWorkReaction(true);
  }, durationMs + 30);
  return true;
}
function applyDeliveredMemeWorkReaction(meme, result) {
  // inputSent means the native input path itself completed. submitted is the
  // stronger, eventually-consistent transcript confirmation. The reaction
  // should not disappear merely because transcript refresh lagged behind.
  if (!result || (!result.submitted && !result.inputSent)) {
    // playMeme starts the visual optimistically so it is continuous with the
    // short meme reaction. An explicit delivery failure must roll that back.
    clearMemeWorkReaction(true);
    return false;
  }
  // Usually playMeme already started it. Do not restart the 30-second clock
  // after transcript verification, otherwise slow confirmation lengthens the
  // configured reaction unpredictably.
  if (memeWorkReaction) return true;
  return startMemeWorkReaction(meme && meme.reaction && meme.reaction.work);
}
const bubble = document.getElementById('bubble');
const bubbleText = document.getElementById('bubble-text');
const chipCost = document.getElementById('chip-cost');
const chipWindow = document.getElementById('chip-window');
const chip = document.getElementById('chip');
const sessionsEl = document.getElementById('sessions');
const radial = document.getElementById('radial');
const thinkEl = document.getElementById('think');
const sleepEl = document.getElementById('sleep');
const propEl = document.getElementById('prop');
const sidekickEl = document.getElementById('sidekick');
const askEl = document.getElementById('ask');
const askScroll = document.getElementById('ask-scroll');
const askLabel = document.getElementById('ask-label');
const askSess = document.getElementById('ask-sess');
const askQhead = document.getElementById('ask-qhead');
const askQ = document.getElementById('ask-q');
const askHint = document.getElementById('ask-hint');
const askOpts = document.getElementById('ask-opts');
const askInputRow = document.getElementById('ask-input-row'); // .ask-other
const askText = document.getElementById('ask-text');
const askPage = document.getElementById('ask-page');
const askFoot = document.getElementById('ask-foot');
const askSubmit = document.getElementById('ask-submit');
const askBack = document.getElementById('ask-back');
const askTerm = document.getElementById('ask-term');
const notepad = document.getElementById('notepad');
const npBadge = document.getElementById('np-badge');
const todopop = document.getElementById('todopop');
const tpProg = document.getElementById('tp-prog');
const tpList = document.getElementById('tp-list');
const tpActs = document.getElementById('tp-acts');
const tpActSec = document.getElementById('tp-act-sec');
const tpTodoSec = document.getElementById('tp-todo-sec');
const sesslist = document.getElementById('sesslist');
const slRows = document.getElementById('sl-rows');
const slSub = document.getElementById('sl-sub');
const slTitle = document.getElementById('sl-title');
const slBack = document.getElementById('sl-back');
const slSessionView = document.getElementById('sl-session-view');
const slLoot = document.getElementById('sl-loot');
const slLootText = document.getElementById('sl-loot-text');
const slMemeView = document.getElementById('sl-meme-view');
const slMemeSession = document.getElementById('sl-meme-session');
const slMemeGrid = document.getElementById('sl-meme-grid');
const slMemeStatus = document.getElementById('sl-meme-status');
const slTravelView = document.getElementById('sl-travel-view');
const slTravelSession = document.getElementById('sl-travel-session');
const slTravelRankIcons = document.getElementById('sl-travel-rank-icons');
const slTravelRankMeta = document.getElementById('sl-travel-rank-meta');
const slMachineRankIcons = document.getElementById('sl-machine-rank-icons');
const slMachineRankMeta = document.getElementById('sl-machine-rank-meta');
const slTravelSetup = document.getElementById('sl-travel-setup');
const slTravelTemplates = document.getElementById('sl-travel-templates');
const slTravelMission = document.getElementById('sl-travel-mission');
const slTravelStart = document.getElementById('sl-travel-start');
const slTravelActive = document.getElementById('sl-travel-active');
const slTravelActiveStatus = document.getElementById('sl-travel-active-status');
const slTravelActiveMission = document.getElementById('sl-travel-active-mission');
const slTravelCancel = document.getElementById('sl-travel-cancel');
const slTravelPostcard = document.getElementById('sl-travel-postcard');
const slTravelPostcardMeta = document.getElementById('sl-travel-postcard-meta');
const slTravelStopTrack = document.getElementById('sl-travel-stop-track');
const slTravelStopPrev = document.getElementById('sl-travel-stop-prev');
const slTravelStopNext = document.getElementById('sl-travel-stop-next');
const slTravelStopPage = document.getElementById('sl-travel-stop-page');
const slTravelMailboxes = document.getElementById('sl-travel-mailboxes');
const slTravelHistory = document.getElementById('sl-travel-history');
const slTravelStatus = document.getElementById('sl-travel-status');
const slWander = document.getElementById('sl-wander');
const slTravelInbox = document.getElementById('sl-travel-inbox');
const slSearch = document.getElementById('sl-search');
const slFilters = document.getElementById('sl-filters');
const slArchivedToggle = document.getElementById('sl-archived-toggle');
const memePlayer = document.getElementById('meme-player');
const memeImage = document.getElementById('meme-image');
const memeCaption = document.getElementById('meme-caption');

let askActive = false;
let askQueue = []; // 当前所有待处理的选择/输入（每项含 project）
let askIdx = 0;
let lastAskSig = ''; // 当前面板内容签名，避免每 2s 重渲冲掉用户输入
const answered = new Set(); // 已答的 key，避免快照延迟导致重弹
let askHover = false; // 鼠标在选项面板上
let elic = null;      // elicitation 渲染态：{ key, questions, qIdx, answers, selected }
let sessionSearch = '';
let sessionFilter = 'all';
let showArchived = false;
let pinnedSessionIds = [];
let archivedSessionIds = [];
// 面板开着、且(鼠标在面板上 / 输入框聚焦/有草稿 / 已选了选项) = 交互中：
// 此时别重渲面板、别改小章鱼状态，免得打断你思考/选择。面板一关就自动解除。
const isInteracting = () => askActive && (askHover || document.activeElement === askText || !!(askText && askText.value) || (elic && elic.selected != null));

// 把 UI 决策写日志，便于自检；双宠模式给 tag 带上身份前缀（claude:state / codex:state）
const rlog = (tag, msg) => { try { window.pet.petLog((AGENT === 'all' ? '' : AGENT + ':') + tag, msg); } catch {} };
// i18n: shared/i18n.js is loaded as a <script> before this file.
const t = (key, vars) => window.OctoI18n.t(key, vars);
// A reason arrives as a stable key ('reply'|'plan'|'perm'); older payloads may
// still carry free text, so fall back to whatever came in.
const waitPhrase = (reason) => (reason ? t('wait.' + reason) : t('wait.default'));
const reasonWord = (reason) => (reason ? t('reason.' + reason) : t('reason.default'));
const esc = (s) => String(s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// 带上 sessionId：否则同一项目下两个并行会话若问了同样的问题，会共用一个 key，
// 答掉一个就把另一个也标记成 answered 吞掉。choice 各构造处都带 sessionId。
const choiceKey = (c) => (c && (c.sessionId || '') + '|' + (c.project || '') + '|' + (c.question || '')) || '';

// 动态定高：弹层贴 pet 上方(bottom:200)，把窗口高度调到刚好容纳内容，
// 避免固定大窗口留白 / 顶屏被下移。先扩到目标宽度再量高度：如果在基础
// 320px 窄窗里先测，长文本会被过度换行，错误地把弹层撑到整屏高。
const POPUP_W = 520;
const TRAVEL_POPUP_W = 760;
const POPUP_BOTTOM = 200;
const ASK_VIEWPORT_MAX_H = 520;
// 普通会话页永远只占三条 Session 的固定高度；更多内容只在列表内部滚动。
// 掠夺按秒流入时也复用同一个值，BrowserWindow 从打开到结束不再改变尺寸。
// 右侧基线分支在 3 条会话时的实测内容高度为 310px，对应 520 × 534
// 的 BrowserWindow。固定使用这份三行高度；更多会话只在 sl-scroll 内滚动。
const SESSION_PANEL_H = 310;
const MEME_WINDOW_W = 760;
const MEME_WINDOW_H = 340;
const MEME_MEDIA_W = 260;
const MEME_GAP = 14;
const MEME_EDGE_PAD = 10;
const BASE_PET_FRAME_H = 340;
const RESTING_FRAME_MAX_W = 360;
const RESTING_FRAME_MAX_H = 360;
let memeLayoutActive = false;
let fitPopupSeq = 0;
let edgeLayout = { vertical: 'above', horizontal: 'center' };

function browserWorkArea() {
  const s = window.screen || {};
  const width = Number.isFinite(s.availWidth) ? s.availWidth : (window.innerWidth || 320);
  const height = Number.isFinite(s.availHeight) ? s.availHeight : (window.innerHeight || 340);
  return {
    x: Number.isFinite(s.availLeft) ? s.availLeft : 0,
    y: Number.isFinite(s.availTop) ? s.availTop : 0,
    width,
    height,
  };
}

function petGeometrySnapshot() {
  const el = curSkinEl();
  if (!el || !Number.isFinite(window.screenX) || !Number.isFinite(window.screenY)) return null;
  const rect = el.getBoundingClientRect();
  const viewportW = Math.max(1, window.innerWidth || 320);
  const viewportH = Math.max(1, window.innerHeight || 340);
  return {
    workArea: browserWorkArea(),
    windowRect: { x: window.screenX, y: window.screenY, width: viewportW, height: viewportH },
    petRect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
  };
}

function setStageEdgeLayout(next) {
  const layout = next || edgeLayout;
  edgeLayout = {
    vertical: layout.vertical === 'below' ? 'below' : 'above',
    horizontal: ['left', 'right'].includes(layout.horizontal) ? layout.horizontal : 'center',
  };
  stage.classList.toggle('edge-below', edgeLayout.vertical === 'below');
  stage.classList.toggle('edge-left', edgeLayout.horizontal === 'left');
  stage.classList.toggle('edge-right', edgeLayout.horizontal === 'right');
}

// Changing the flex anchor moves the pet inside the transparent BrowserWindow.
// This payload lets the main process move/resize that window in the opposite
// direction, so the visible pet stays on exactly the same screen pixel.
function anchoredLayoutPayload(next) {
  const before = petGeometrySnapshot();
  if (!before) { setStageEdgeLayout(next); return null; }
  const oldPet = before.petRect;
  const wa = before.workArea;
  const waRight = wa.x + wa.width;
  const waBottom = wa.y + wa.height;
  const wr = before.windowRect;
  const compactHorizontalFrame = wr.width <= RESTING_FRAME_MAX_W;
  const compactVerticalFrame = wr.height <= RESTING_FRAME_MAX_H;
  let screenX = wr.x + oldPet.x;
  let screenY = wr.y + oldPet.y;

  // A frame at the work-area edge plus a large transparent inset means the OS
  // stopped the BrowserWindow before the user's visible pet reached the edge.
  // Treat that as an explicit edge drag and snap the *pet body*, not the frame.
  if (compactVerticalFrame && next.vertical === 'below' && wr.y <= wa.y + 3 && oldPet.y > 18) screenY = wa.y;
  if (compactVerticalFrame && next.vertical === 'above'
    && wr.y + wr.height >= waBottom - 3 && wr.height - oldPet.y - oldPet.height > 18) {
    screenY = waBottom - oldPet.height;
  }
  if (compactHorizontalFrame && next.horizontal === 'left' && wr.x <= wa.x + 3 && oldPet.x > 18) screenX = wa.x;
  if (compactHorizontalFrame && next.horizontal === 'right'
    && wr.x + wr.width >= waRight - 3 && wr.width - oldPet.x - oldPet.width > 18) {
    screenX = waRight - oldPet.width;
  }
  setStageEdgeLayout(next);
  const rect = curSkinEl().getBoundingClientRect();
  const viewportW = Math.max(1, window.innerWidth || 320);
  const viewportH = Math.max(1, window.innerHeight || 340);
  const xAlign = edgeLayout.horizontal;
  const yAlign = edgeLayout.vertical === 'below' ? 'top' : 'bottom';
  const xOffset = xAlign === 'left'
    ? rect.left
    : xAlign === 'right'
      ? viewportW - rect.right
      : rect.left + rect.width / 2 - viewportW / 2;
  const yOffset = yAlign === 'top' ? rect.top : viewportH - rect.bottom;
  return {
    screenX, screenY,
    width: rect.width, height: rect.height,
    xAlign, yAlign, xOffset, yOffset,
  };
}

function restingEdgeLayout() {
  const snapshot = petGeometrySnapshot();
  if (!snapshot || !window.PetGeometry) return edgeLayout;
  // In an expanded popup the bottom-anchored pet's local y grows by exactly
  // the extra window height. Remove that artificial offset before deciding
  // whether the visible pet itself is actually in the top-edge zone.
  const frameHeightExcess = Math.max(0, snapshot.windowRect.height - BASE_PET_FRAME_H);
  let topThreshold = snapshot.petRect.y - frameHeightExcess + 2;
  if (edgeLayout.vertical === 'below') {
    // Measure the real normal-layout inset for the current skin/status stack.
    // A fixed number is wrong as soon as a chip/bubble changes height and can
    // make pointerup flip the pet back too early.
    const previous = { ...edgeLayout };
    setStageEdgeLayout({ ...previous, vertical: 'above' });
    topThreshold = curSkinEl().getBoundingClientRect().top - frameHeightExcess + 2;
    setStageEdgeLayout(previous);
  }
  return window.PetGeometry.chooseRestingLayout({
    ...snapshot,
    current: edgeLayout,
    threshold: Math.max(24, topThreshold),
    inferVerticalFrameClamp: snapshot.windowRect.height <= RESTING_FRAME_MAX_H,
    inferHorizontalFrameClamp: snapshot.windowRect.width <= RESTING_FRAME_MAX_W,
  });
}

function popupEdgeLayout(height, popupHeight) {
  const snapshot = petGeometrySnapshot();
  if (!snapshot || !window.PetGeometry) return edgeLayout;
  return window.PetGeometry.choosePopupLayout({
    ...snapshot,
    current: edgeLayout,
    popupHeight: Math.max(80, Number(popupHeight) || (Number(height) || 340) - POPUP_BOTTOM),
    inferVerticalFrameClamp: snapshot.windowRect.height <= RESTING_FRAME_MAX_H,
    inferHorizontalFrameClamp: snapshot.windowRect.width <= RESTING_FRAME_MAX_W,
  });
}

function setRequestedPetSize(w, h, options = {}) {
  let width = Number(w) || 0;
  let height = Number(h) || 0;
  if (memeLayoutActive) {
    width = Math.max(width, MEME_WINDOW_W);
    height = Math.max(height, MEME_WINDOW_H);
  }
  const nextLayout = options.popup
    ? popupEdgeLayout(height, options.popupHeight)
    : restingEdgeLayout();
  const anchor = anchoredLayoutPayload(nextLayout);
  try { window.pet.setPetSize(width, height, anchor); } catch {}
}
function fitPopup(el) {
  if (!el) return;
  const seq = ++fitPopupSeq;
  requestAnimationFrame(() => {
    const fixedSessionPage = el === sesslist
      && slSessionView && !slSessionView.classList.contains('hidden');
    if (fixedSessionPage) {
      if (seq !== fitPopupSeq) return;
      // 固定页无需先扩宽再测量；一次完成宽高与上下翻转，避免中间帧错位。
      setRequestedPetSize(
        POPUP_W,
        Math.max(340, POPUP_BOTTOM + SESSION_PANEL_H + 24),
        { popup: true, popupHeight: SESSION_PANEL_H },
      );
      return;
    }
    const measure = () => {
      if (seq !== fitPopupSeq) return;
      const popupW = el === sesslist && slTravelView && !slTravelView.classList.contains('hidden')
        ? TRAVEL_POPUP_W
        : POPUP_W;
      // 关键：先临时去掉 max-height 再量，否则 scrollHeight 会被「当前小窗口算出的
      // max-height」钳住（鸡生蛋问题）→ 窗口永远只长一点点、列表只剩 1 行+滚动条。
      const prev = el.style.maxHeight;
      el.style.maxHeight = 'none';
      const contentH = el.scrollHeight;
      el.style.maxHeight = prev;
      const viewportH = el === askEl ? Math.min(contentH, ASK_VIEWPORT_MAX_H) : contentH;
      const winH = Math.max(340, POPUP_BOTTOM + viewportH + 24);
      setRequestedPetSize(popupW, winH, { popup: true, popupHeight: viewportH });
    };

    const targetW = el === sesslist && slTravelView && !slTravelView.classList.contains('hidden')
      ? TRAVEL_POPUP_W
      : POPUP_W;
    if (Math.abs((window.innerWidth || 0) - targetW) > 2) {
      // 第一拍只扩宽，第二拍在正确的横向排版下测真实高度。
      setRequestedPetSize(targetW, Math.max(340, window.innerHeight || 340), { popup: true });
      requestAnimationFrame(() => requestAnimationFrame(measure));
    } else {
      measure();
    }
  });
}
function resetPetSize() {
  fitPopupSeq++;
  if (memeLayoutActive) setRequestedPetSize(MEME_WINDOW_W, MEME_WINDOW_H);
  else setRequestedPetSize(0, 0);
}

function settleEdgeLayout() {
  // No screen coordinates in the headless renderer tests; the real Electron
  // window always has them. This also avoids inventing a desktop in Node.
  if (!petGeometrySnapshot()) return;
  setRequestedPetSize(memeLayoutActive ? MEME_WINDOW_W : 0, memeLayoutActive ? MEME_WINDOW_H : 0);
  requestAnimationFrame(reportPetVisualBounds);
}

// Switch the internal top/bottom anchor *during* a drag, just before the
// transparent BrowserWindow reaches the work-area boundary. The visible pet
// is kept on the same screen pixel and the gesture is rebased, so the next
// pointer frame continues from there instead of producing edge -> pause ->
// jump. Returning from the top probes the normal layout first and restores it
// as soon as the whole frame can fit on-screen again.
function movePetDuringDrag(gesture, e, targetX, targetY) {
  const el = curSkinEl();
  if (!el) {
    window.pet.setWinPos(targetX, targetY);
    return;
  }
  const before = el.getBoundingClientRect();
  const petScreenX = targetX + before.left;
  const petScreenY = targetY + before.top;
  const wa = browserWorkArea();
  let nextVertical = edgeLayout.vertical;

  if (edgeLayout.vertical === 'above') {
    nextVertical = window.PetGeometry
      ? window.PetGeometry.chooseDragVerticalLayout({
        current: 'above', workArea: wa, targetWindowY: targetY,
        petScreenY, abovePetOffset: before.top,
      })
      : (targetY <= wa.y + 2 ? 'below' : 'above');
  } else if (edgeLayout.vertical === 'below') {
    const candidate = { ...edgeLayout, vertical: 'above' };
    setStageEdgeLayout(candidate);
    const normalRect = el.getBoundingClientRect();
    const probed = window.PetGeometry
      ? window.PetGeometry.chooseDragVerticalLayout({
        current: 'below', workArea: wa, targetWindowY: targetY,
        petScreenY, abovePetOffset: normalRect.top,
      })
      : (petScreenY - normalRect.top >= wa.y + 2 ? 'above' : 'below');
    if (probed === 'above') {
      nextVertical = 'above';
    } else {
      setStageEdgeLayout({ ...edgeLayout, vertical: 'below' });
      nextVertical = 'below';
    }
  }

  if (nextVertical !== edgeLayout.vertical) {
    setStageEdgeLayout({ ...edgeLayout, vertical: nextVertical });
  }
  const after = el.getBoundingClientRect();
  const anchoredX = petScreenX - after.left;
  const anchoredY = petScreenY - after.top;

  if (Math.abs(anchoredX - targetX) > 0.5 || Math.abs(anchoredY - targetY) > 0.5) {
    gesture.win = [anchoredX, anchoredY];
    gesture.sx = e.screenX;
    gesture.sy = e.screenY;
  }
  window.pet.setWinPos(anchoredX, anchoredY);
}

// 从快照重建队列（多任务都在、且标明项目）
function refreshAsk(stats) {
  // 记事本行动中心开着时，事项在那里处理，别再另弹选项面板抢窗口
  if (todoPopOpen) { hideAsk(); return; }
  const items = (stats.sessions || [])
    .filter((x) => (x.state === 'waiting' || x.state === 'needsinput') && x.choice)
    .map((x) => x.choice)
    .filter((c) => (c.options && c.options.length) || c.allowInput);
  const present = new Set(items.map(choiceKey));
  for (const k of [...answered]) if (!present.has(k)) answered.delete(k); // 已消失=已答完，清理
  const fresh = items.filter((c) => !answered.has(choiceKey(c)));

  // 你正在答当前卡片、且它后端仍然有效 → 不重渲(保住勾选/输入)，但仍静默对账队列其余项，
  // 这样已解决的卡片不会残留、新卡片不会被你的“交互中”状态永久挡在外面。
  const cur = askActive ? askQueue[askIdx] : null;
  if (isInteracting() && cur && present.has(choiceKey(cur))) {
    askQueue = fresh;
    const i = fresh.findIndex((c) => choiceKey(c) === choiceKey(cur));
    askIdx = i >= 0 ? i : 0;
    return;
  }

  askQueue = fresh;
  if (!askQueue.length) { hideAsk(); return; }
  if (askIdx >= askQueue.length) askIdx = 0;
  const sig = askQueue.map(choiceKey).join(',');
  if (askActive && sig === lastAskSig) return; // 内容没变，别重渲（保住正在输入/勾选的）
  lastAskSig = sig;
  showAskPanel();
}

function enqueueChoice(c) {
  if (!c || (!(c.options && c.options.length) && !c.allowInput)) return;
  answered.delete(choiceKey(c));
  const i = askQueue.findIndex((x) => choiceKey(x) === choiceKey(c));
  if (i < 0) askQueue.push(c);
  // 记事本行动中心开着 → 新事项在那里显示，不另弹面板
  if (todoPopOpen) { renderTodoPop(); return; }
  // 你正在答当前面板时，新任务先进队列、不抢面板（等你答完再显示），避免打断
  if (isInteracting() && askActive) return;
  askIdx = askQueue.findIndex((x) => choiceKey(x) === choiceKey(c));
  showAskPanel();
}

function showAskPanel() {
  const c = askQueue[askIdx];
  if (!c) { hideAsk(); return; }
  if (sessListOpen) closeSessList(); // 卡片优先于会话列表
  askEl.classList.toggle('travel-letter', c.travel === true);

  const sess = c.sessionId ? ' · #' + String(c.sessionId).slice(-3) : '';
  const queue = askQueue.length > 1 ? `${askIdx + 1}/${askQueue.length} · ` : '';
  askSess.textContent = queue + (c.project || '?') + sess;

  if (c.kind === 'ask') {
    if (!elic || elic.key !== choiceKey(c)) {
      elic = { key: choiceKey(c), questions: Array.isArray(c.questions) ? c.questions : [], qIdx: 0, answers: {}, selected: null, selSet: [], multi: false, otherOn: false };
    }
    renderElicitation(c);
  } else {
    elic = null;
    if (c.kind === 'perm' && c.permId) renderPerm(c);
    else if (c.kind === 'plan' && c.permId) renderPlan(c);
    else renderContinue(c);
  }

  bubble.classList.add('hidden');
  askEl.classList.remove('hidden');
  lastAskSig = askQueue.map(choiceKey).join(',');
  askActive = true;
  rlog('ask', 'show ' + (c.kind || '') + ': ' + String(c.question || '').slice(0, 36));
  fitPopup(askEl); // 富卡片：固定头尾、中部滚动，动态定高 + 520 宽
}

function clearAskBody() {
  askScroll.scrollTop = 0;
  askOpts.innerHTML = '';
  askOpts.classList.remove('perm-row');
  askQhead.textContent = '';
  askHint.textContent = '';
  askPage.textContent = '';
  askInputRow.classList.add('hidden');
  askText.value = '';
  askTerm.textContent = t('ask.goTerminal');
}

// ① elicitation（AskUserQuestion）：多选项卡 + Other + 分页 + Submit/Back
function renderElicitation(c) {
  clearAskBody();
  askLabel.textContent = 'Needs Input';
  const qs = elic.questions;
  const q = qs[elic.qIdx] ||
    { question: c.question || t('ask.needAnswer'), options: (c.options || []).map((o) => ({ label: o.label, description: o.desc })) };
  askQhead.textContent = q.header || '';
  askQ.textContent = q.question || '';
  const multi = !!q.multiSelect;
  elic.multi = multi;
  askHint.textContent = multi ? t('ask.multiHint') : t('ask.singleHint');

  const prior = elic.answers[q.question];
  const opts = q.options || [];
  const known = (v) => opts.some((o) => o.label === v);
  if (multi) {
    const parts = prior ? String(prior).split(/,\s*/).filter(Boolean) : [];
    elic.selSet = parts.filter(known);
    const otherText = parts.find((p) => !known(p));
    elic.otherOn = !!otherText;
    elic.selected = null;
    if (otherText) askText.value = otherText;
  } else {
    elic.selSet = [];
    elic.otherOn = false;
    elic.selected = prior != null ? (known(prior) ? prior : '__other__') : null;
  }

  for (const o of opts) askOpts.appendChild(buildRadioCard(o.label, o.description, o.label, q));
  askOpts.appendChild(buildRadioCard('Other', '', '__other__', q));
  if (elic.selected === '__other__' || (multi && elic.otherOn)) {
    askInputRow.classList.remove('hidden');
    if (!multi && prior && !known(prior)) askText.value = prior;
  }

  askPage.textContent = `${elic.qIdx + 1} / ${qs.length || 1}`;
  askFoot.classList.remove('hidden');
  const last = elic.qIdx >= (qs.length || 1) - 1;
  askSubmit.textContent = last ? 'Submit Answer' : 'Next ›';
  askBack.classList.toggle('hidden', elic.qIdx === 0);
  askTerm.classList.remove('hidden');
  updateSubmitEnabled(q);
  fitPopup(askEl); // 题目切换后内容高度变了，重新定高
}

function buildRadioCard(label, desc, value, q) {
  const multi = elic.multi;
  const isSel = multi ? (value === '__other__' ? elic.otherOn : elic.selSet.includes(value)) : elic.selected === value;
  const card = document.createElement('button');
  card.className = 'ask-opt' + (multi ? ' multi' : '') + (isSel ? ' sel' : '');
  card.innerHTML =
    '<span class="ask-radio"></span><span class="ask-ot">' +
    `<span class="ask-ol">${esc(label)}</span>` + (desc ? `<span class="ask-od">${esc(desc)}</span>` : '') +
    '</span>';
  card.addEventListener('click', () => {
    if (multi) {
      if (value === '__other__') {
        elic.otherOn = !elic.otherOn;
        card.classList.toggle('sel', elic.otherOn);
        askInputRow.classList.toggle('hidden', !elic.otherOn);
        if (elic.otherOn) setTimeout(() => askText.focus(), 0);
      } else {
        const i = elic.selSet.indexOf(value);
        if (i >= 0) elic.selSet.splice(i, 1); else elic.selSet.push(value);
        card.classList.toggle('sel');
      }
    } else {
      elic.selected = value;
      askInputRow.classList.toggle('hidden', value !== '__other__');
      if (value === '__other__') setTimeout(() => askText.focus(), 0);
      [...askOpts.children].forEach((el) => el.classList.remove('sel'));
      card.classList.add('sel');
    }
    updateSubmitEnabled(q);
  });
  return card;
}

function updateSubmitEnabled() {
  let ok;
  if (elic && elic.multi) ok = elic.selSet.length > 0 || (elic.otherOn && (askText.value || '').trim());
  else ok = elic && elic.selected && (elic.selected !== '__other__' || (askText.value || '').trim());
  askSubmit.classList.toggle('disabled', !ok);
}

// 自定义输入为空时按回车：不发送，抖一下 + 提示别忘了填（2.6s 后复原 placeholder）
let emptyWarnTimer = null;
function warnEmptyInput() {
  askText.focus();
  askText.classList.add('warn');
  if (!askText.dataset.ph) askText.dataset.ph = askText.placeholder || t('ask.placeholder');
  askText.placeholder = t('ask.emptyWarn');
  clearTimeout(emptyWarnTimer);
  emptyWarnTimer = setTimeout(() => {
    askText.classList.remove('warn');
    if (askText.dataset.ph) { askText.placeholder = askText.dataset.ph; delete askText.dataset.ph; }
  }, 2600);
}

function elicNextOrSubmit(c) {
  const qs = elic.questions;
  const q = qs[elic.qIdx];
  let val;
  if (elic.multi) {
    const parts = [...elic.selSet];
    if (elic.otherOn && (askText.value || '').trim()) parts.push((askText.value).trim());
    val = parts.join(', ');
  } else {
    val = elic.selected === '__other__' ? (askText.value || '').trim() : elic.selected;
  }
  if (!val) return; // 必须先选/填
  if (q && q.question) elic.answers[q.question] = val;
  else elic.answers[c.question || '_'] = val;
  if (elic.qIdx < (qs.length || 1) - 1) { elic.qIdx++; renderElicitation(c); return; }
  window.pet.decidePermission(c.permId, { type: 'elicitation-submit', answers: { ...elic.answers } });
  rlog('ask', 'elicitation submit ' + Object.keys(elic.answers).length);
  finishChoice(c, t('ask.submitted'));
}

function elicBack(c) {
  if (elic && elic.qIdx > 0) { elic.qIdx--; renderElicitation(c); }
}

// ② 授权：允许(绿)/拒绝(红) + 可选「始终允许」建议按钮(中性)
function renderPerm(c) {
  clearAskBody();
  askLabel.textContent = c.travel ? t('travel.letterLabel') : t('ask.needPerm');
  askQhead.textContent = c.header || '';
  askQ.textContent = c.question || t('ask.needPermQ');
  const opts = c.options || [];
  if (opts.length === 2) askOpts.classList.add('perm-row'); // 仅允许/拒绝时并排
  opts.forEach((opt) => {
    const kind = opt.key === 'allow' ? 'allow' : opt.key === 'deny' ? 'deny' : 'sugg';
    const card = document.createElement('button');
    card.className = 'ask-opt act ' + kind;
    card.innerHTML = `<span class="ask-ot"><span class="ask-ol">${esc(opt.label)}</span></span>`;
    card.addEventListener('click', () => submitPerm(opt.key, c, opt.label));
    askOpts.appendChild(card);
  });
  askFoot.classList.add('hidden');
  if (c.travel) askTerm.textContent = t('travel.openTerminal');
  askTerm.classList.remove('hidden');
}

// ③ 纯回复（无选项）：只读问题 + Go to Terminal
function renderContinue(c) {
  clearAskBody();
  askLabel.textContent = 'Needs Input';
  askQ.textContent = c.question || t('ask.waitingReply');
  askFoot.classList.add('hidden');
  askTerm.classList.remove('hidden');
}

// ④ ExitPlanMode 方案评审：展示方案 + 批准 / 打回并反馈
function renderPlan(c) {
  clearAskBody();
  askLabel.textContent = t('ask.planLabel');
  askQhead.textContent = c.project ? '📂 ' + c.project : '';
  askQ.textContent = c.question || t('ask.planQ');
  const approve = document.createElement('button');
  approve.className = 'ask-opt act allow';
  approve.innerHTML = '<span class="ask-ot"><span class="ask-ol">' + esc(t('ask.approve')) + '</span></span>';
  approve.addEventListener('click', () => submitPerm('allow', c, t('ask.approved')));
  askOpts.appendChild(approve);
  const reject = document.createElement('button');
  reject.className = 'ask-opt act deny';
  reject.innerHTML = '<span class="ask-ot"><span class="ask-ol">' + esc(t('ask.reject')) + '</span></span>';
  reject.addEventListener('click', () => {
    window.pet.decidePermission(c.permId, { type: 'plan-feedback', feedback: (askText.value || '').trim() });
    finishChoice(c, t('ask.rejected'));
  });
  askOpts.appendChild(reject);
  askInputRow.classList.remove('hidden');
  askText.placeholder = t('ask.rejectPlaceholder');
  askFoot.classList.add('hidden');
  askTerm.classList.remove('hidden');
}

function finishChoice(choice, bubbleMsg) {
  answered.add(choiceKey(choice));
  elic = null;
  askQueue = askQueue.filter((c) => choiceKey(c) !== choiceKey(choice));
  if (askQueue.length) {
    // 还有下一题：直接展示，不弹确认气泡盖住选项面板
    askIdx = 0; showAskPanel();
  } else {
    // 先关面板（置 askActive=false），确认气泡才不会被 showBubble 的 askActive 早退拦掉
    hideAsk();
    showBubble(bubbleMsg, 2600);
  }
}
function submitPerm(key, choice, label) {
  window.pet.decidePermission(choice.permId, key);
  const msg = key === 'allow' ? t('ask.allowed') : key === 'deny' ? t('ask.denied') : t('ask.remembered');
  finishChoice(choice, msg);
}
// Go to Terminal：去会话终端自己答（授权/elicitation 都回 deny，让 CC 在终端重问）
function gotoSession(choice) {
  if (choice.permId) window.pet.decidePermission(choice.permId, 'deny');
  window.pet.focusSession(choice.sessionId || '');
  finishChoice(choice, t('ask.toTerminal'));
}

function hideAsk() {
  if (askActive) rlog('ask', 'hide');
  lastAskSig = '';
  elic = null;
  askEl.classList.add('hidden');
  askHover = false;
  if (askText) askText.value = ''; // 清掉草稿，避免关闭后仍被判为「交互中」冻住状态
  if (askActive) { askActive = false; resetPetSize(); window.pet.blurPet(); }
}

// ---------- 记事本 / 行动清单 ----------
let curTodos = [];
let curTodosProj = '';
let curSessions = [];
let todoPopOpen = false;
const TODO_ICON = { completed: '✅', in_progress: '▶️', pending: '⬜️' };

// 当前需要你处理的事项：有 choice、还没答过的 waiting/needsinput 会话
function actionableItems() {
  return curSessions
    .filter((x) => (x.state === 'waiting' || x.state === 'needsinput') && x.choice && !answered.has(choiceKey(x.choice)))
    .map((x) => x.choice)
    .filter((c) => (c.options && c.options.length) || c.allowInput);
}

let notepadShown = false;
function updateNotepad(s) {
  curTodos = Array.isArray(s.todos) ? s.todos : [];
  curTodosProj = s.todosProject || '';
  curSessions = s.sessions || [];
  const acts = actionableItems();
  if (!curTodos.length && !acts.length) {
    notepad.classList.add('hidden');
    if (notepadShown) { rlog('notepad', 'hide'); notepadShown = false; }
    if (todoPopOpen) closeTodoPop();
    return;
  }
  notepad.classList.remove('hidden');
  if (!notepadShown) { rlog('notepad', `show acts=${acts.length} todos=${curTodos.length}`); notepadShown = true; }
  if (acts.length) {
    npBadge.textContent = acts.length; // 优先显示「需处理」数
    npBadge.classList.add('urgent');
  } else {
    const done = curTodos.filter((t) => t.status === 'completed').length;
    npBadge.textContent = `${done}/${curTodos.length}`;
    npBadge.classList.remove('urgent');
  }
  // 弹层开着、且用户没在弹层里打字 → 同步刷新内容
  if (todoPopOpen && !todopop.contains(document.activeElement)) { renderTodoPop(); fitPopup(todopop); }
}

function renderTodoPop() {
  const acts = actionableItems();
  const done = curTodos.filter((t) => t.status === 'completed').length;
  tpProg.textContent = curTodos.length ? t('todo.progress', { done, total: curTodos.length }) : '';
  // 需要你处理
  if (acts.length) {
    tpActSec.classList.remove('hidden');
    tpActs.innerHTML = '';
    acts.forEach((c) => tpActs.appendChild(buildActCard(c)));
  } else {
    tpActSec.classList.add('hidden');
    tpActs.innerHTML = '';
  }
  // 待办
  if (curTodos.length) {
    tpTodoSec.classList.remove('hidden');
    tpList.innerHTML = curTodos
      .map((t) => {
        const cls = t.status === 'completed' ? 'tp-row done' : t.status === 'in_progress' ? 'tp-row doing' : 'tp-row';
        return `<div class="${cls}"><span class="ic">${TODO_ICON[t.status] || '⬜️'}</span><span class="tx">${esc(t.content)}</span></div>`;
      })
      .join('');
  } else {
    tpTodoSec.classList.add('hidden');
    tpList.innerHTML = '';
  }
}

// 一张「需要你处理」卡片：问题 + 选项按钮(可点即答) + 自定义输入
function buildActCard(c) {
  const card = document.createElement('div');
  card.className = 'tp-act' + (c.travel ? ' travel-letter' : '');
  const kindTag = c.travel ? t('travel.letterLabel')
    : c.kind === 'perm' ? t('ask.kindPerm')
      : c.kind === 'continue' ? t('ask.kindContinue')
        : c.kind === 'plan' ? t('ask.kindPlan') : t('ask.kindChoice');
  const head = document.createElement('div');
  head.className = 'tp-act-proj';
  head.textContent = `${c.travel ? '✉️' : '📂'} ${c.project || '?'} · ${kindTag}`;
  card.appendChild(head);
  const q = document.createElement('div');
  q.className = 'tp-act-q';
  q.textContent = (c.header ? '【' + c.header + '】 ' : '') + (c.question || t('ask.needHandling'));
  card.appendChild(q);

  const opts = document.createElement('div');
  opts.className = 'tp-act-opts';
  if (c.kind === 'perm' && c.permId) {
    // 授权：允许/拒绝 → HTTP 原生通道回 CC
    (c.options || []).forEach((opt) => {
      const b = document.createElement('button');
      b.textContent = opt.label;
      if (opt.desc) b.title = opt.desc;
      b.addEventListener('click', (e) => { e.stopPropagation(); popPerm(c, opt.key); });
      opts.appendChild(b);
    });
  } else {
    // 对话类：选项只读展示 + 「去回复」按钮（桌宠不替你打字）
    (c.options || []).forEach((opt) => {
      const label = typeof opt === 'string' ? opt : opt.label;
      const desc = typeof opt === 'string' ? '' : opt.desc || '';
      const d = document.createElement('div');
      d.className = 'tp-act-ro';
      d.textContent = label;
      if (desc) d.title = desc;
      opts.appendChild(d);
    });
    const go = document.createElement('button');
    go.className = 'tp-act-go';
    go.textContent = t('ask.goReply');
    go.addEventListener('click', (e) => { e.stopPropagation(); popGoto(c); });
    opts.appendChild(go);
  }
  card.appendChild(opts);
  return card;
}

// 授权：回 CC 决策
function popPerm(choice, key) {
  window.pet.decidePermission(choice.permId, key);
  answered.add(choiceKey(choice));
  renderTodoPop();
  maybeCloseEmptyPop();
}
// 对话类：定位并唤起该会话窗口
function popGoto(choice) {
  window.pet.focusSession(choice.sessionId || '');
  answered.add(choiceKey(choice));
  renderTodoPop();
  maybeCloseEmptyPop();
}
function maybeCloseEmptyPop() {
  if (!actionableItems().length && !curTodos.length) closeTodoPop();
}

function openTodoPop() {
  if (askActive) hideAsk(); // 别和选项面板抢窗口
  if (sessListOpen) closeSessList();
  renderTodoPop();
  todopop.classList.remove('hidden');
  todoPopOpen = true;
  rlog('pop', `open acts=${actionableItems().length} todos=${curTodos.length}`);
  fitPopup(todopop);
}
function closeTodoPop() {
  todopop.classList.add('hidden');
  todoPopOpen = false;
  rlog('pop', 'close');
  window.pet.blurPet();
  resetPetSize();
}

// ---------- 会话列表 HUD（左键弹出）----------
let sessListOpen = false;
let memeCatalog = { schemaVersion: 2, items: [] };
let memeTarget = null;
let memeTimer = null;
let memeAudio = null;
let memeCatalogRefreshTimer = null;
let travelTarget = null;
let travelData = null;
let travelPostcards = [];
let selectedPostcardId = null;
let selectedPostcardStop = 0;
let renderedPostcardKey = '';
let travelTemplateId = null;
let travelMissionDirty = false;
let lootCapture = null;
let lootCaptureTimers = [];
let lootPerformanceActive = false;
let lootPerformanceTimer = null;
let lootTargetClosed = false;
let lootKeptSessions = [];
let lootKeptExpiryTimer = null;

function beginLootPerformance() {
  clearTimeout(lootPerformanceTimer);
  lootPerformanceActive = true;
}

function endLootPerformance(delay = 0) {
  clearTimeout(lootPerformanceTimer);
  lootPerformanceTimer = setTimeout(() => { lootPerformanceActive = false; }, delay);
}
// Agent 图标 —— 只盯 OpenSquilla，恒用一颗小 burst。
const AGENT_ICON =
  '<svg viewBox="0 0 24 24" fill="#d97757"><path d="M12 1l2.2 6.3L20.5 5l-4 5.4 6.5 1.6-6.5 1.6 4 5.4-6.3-2.3L12 23l-2.2-6.3L3.5 19l4-5.4L1 12l6.5-1.6-4-5.4 6.3 2.3z"/></svg>';
const agentIcon = () => AGENT_ICON;
// State → HUD label. Resolved per call (not a frozen table) so switching the
// language re-labels every row on the next render.
const SESS_META_ICON = {
  waiting: '✋ ', needsinput: '💬 ', working: '⚙️ ', juggling: '🤹 ',
  sweeping: '🧹 ', thinking: '💭 ', loafing: '🍦 ', error: '😵 ',
  idle: '', sleeping: '💤 ',
};
const SESS_META_KEY = {
  waiting: 'state.waiting', needsinput: 'state.needsinput', working: 'state.working',
  juggling: 'state.juggling', sweeping: 'state.sweeping', thinking: 'state.thinking',
  loafing: 'state.loafingLong', error: 'state.error', idle: 'state.idle',
  sleeping: 'state.sleeping',
};
function sessMeta(state) {
  const key = SESS_META_KEY[state];
  return key ? (SESS_META_ICON[state] || '') + t(key) : null;
}
const SESS_SORT = { waiting: 0, needsinput: 0, error: 1, working: 2, juggling: 2, sweeping: 2, thinking: 2, loafing: 3, idle: 4, sleeping: 5 };

// 对齐参考项目阈值：≥90% 红(hot)、≥75% 黄(warm)、其余灰
function ctxClass(p) { return p >= 90 ? 'high' : p >= 75 ? 'mid' : ''; }

const sessionKey = (s) => String((s && (s.sessionId || s.id)) || '');
function activeLootKeptSessions() {
  const now = Date.now();
  return lootKeptSessions.filter((session) => session && session.expiresAt > now);
}
function mergedOrdinarySessions() {
  const byId = new Map();
  for (const session of (curSessions || [])) {
    const key = sessionKey(session);
    if (key) byId.set(key, session);
  }
  for (const kept of activeLootKeptSessions()) {
    const key = sessionKey(kept);
    if (!key) continue;
    // 当前 watcher 数据优先；快照只负责在 watcher 暂时回收历史项时兜底，
    // 并用 lootCapturedUntil 标记这 30 分钟的普通列表保留期。
    byId.set(key, {
      ...kept,
      ...(byId.get(key) || {}),
      lootCapturedUntil: kept.expiresAt,
    });
  }
  return [...byId.values()];
}
function scheduleLootKeptExpiry() {
  clearTimeout(lootKeptExpiryTimer);
  const active = activeLootKeptSessions();
  const next = active.reduce((min, session) => Math.min(min, session.expiresAt), Infinity);
  if (!Number.isFinite(next)) return;
  lootKeptExpiryTimer = setTimeout(() => {
    lootKeptSessions = activeLootKeptSessions();
    if (sessListOpen && !lootCapture && !memeTarget) {
      renderSessList();
      fitPopup(sesslist);
    }
    scheduleLootKeptExpiry();
  }, Math.max(50, next - Date.now() + 20));
}
const isBaseVisibleSession = (s) => !!s && !s.headless && s.state !== 'sleeping';
const isArchivedSession = (s) => archivedSessionIds.includes(sessionKey(s));
// 头顶状态点永远不展示已归档项；HUD 可通过「归档」开关单独查看。
const isVisibleSession = (s) => isBaseVisibleSession(s) && !isArchivedSession(s);
// 单一配色：小点和 HUD 用同一套（完成→绿、中断→红，否则按状态）
function sessionDotClass(s) {
  if (s.state === 'idle' && s.badge === 'done') return 'done';
  if (s.state === 'idle' && s.badge === 'interrupted') return 'error';
  return s.state || 'idle';
}

function visibleSessions() {
  return mergedOrdinarySessions()
    // Dedicated travel sessions live in their own mailbox. They remain in the
    // stats/permission model (so a letter cannot flash away), but do not count
    // as ordinary project tasks.
    .filter((s) => !!s && s.sessionRole !== 'travel')
    .filter((s) => !s.headless && (s.lootCapturedUntil > Date.now()
      || s.state !== 'sleeping' || (showArchived && isArchivedSession(s))))
    .filter((s) => showArchived ? isArchivedSession(s) : !isArchivedSession(s))
    .filter((s) => {
      if (sessionFilter === 'attention') return ['waiting', 'needsinput', 'error'].includes(s.state);
      // Only OpenSquilla ('squilla') sessions exist in this build — no Claude / Codex filters.
      return true;
    })
    .filter((s) => {
      const q = sessionSearch.trim().toLocaleLowerCase();
      if (!q) return true;
      return [s.project, s.sessionId, s.cwd, s.op]
        .some((v) => String(v || '').toLocaleLowerCase().includes(q));
    })
    .sort((a, b) => {
      const lootA = a.lootCapturedUntil > Date.now() ? 0 : 1;
      const lootB = b.lootCapturedUntil > Date.now() ? 0 : 1;
      if (lootA !== lootB) return lootA - lootB;
      const pinA = pinnedSessionIds.includes(sessionKey(a)) ? 0 : 1;
      const pinB = pinnedSessionIds.includes(sessionKey(b)) ? 0 : 1;
      if (pinA !== pinB) return pinA - pinB;
      const pa = SESS_SORT[a.state] != null ? SESS_SORT[a.state] : 3;
      const pb = SESS_SORT[b.state] != null ? SESS_SORT[b.state] : 3;
      if (pa !== pb) return pa - pb;
      return (a.idleMs || 0) - (b.idleMs || 0); // most-recently-active first
    });
}

function sessionsForList() {
  if (!lootCapture) return visibleSessions();
  return lootCapture.sessions;
}

function clearLootTimers() {
  for (const timer of lootCaptureTimers) clearTimeout(timer);
  lootCaptureTimers = [];
}

function setLootBanner(key, vars) {
  if (!slLoot || !slLootText) return;
  slLoot.classList.remove('hidden');
  slLootText.textContent = t(key, vars);
}

function startLootCapture(available = 0) {
  clearLootTimers();
  lootCapture = {
    sessions: [],
    available: Math.max(0, Number(available) || 0),
    enteringSessionId: '',
    ready: false,
  };
  sessionSearch = '';
  sessionFilter = 'all';
  showArchived = false;
  if (slSearch) slSearch.value = '';
  slFilters.querySelectorAll('button[data-filter]').forEach((button) => {
    button.classList.toggle('active', button.dataset.filter === 'all');
  });
  openSessList();
  slTitle.textContent = t('loot.panelTitle');
  setLootBanner(lootCapture.available ? 'loot.capturing' : 'loot.noSessions', { n: lootCapture.available });
  renderSessList();
}

function appendLootSession(session) {
  if (!lootCapture || !session) return;
  const key = sessionKey(session);
  if (!key || lootCapture.sessions.some((item) => sessionKey(item) === key)) return;
  lootCapture.sessions.push(session);
  lootCapture.enteringSessionId = key;
  setLootBanner('loot.progress', { done: lootCapture.sessions.length });
  renderSessList();
  // 掠夺开始时已经一次性固定了会话框高度；这里只滚动内容，绝不能再次
  // resize BrowserWindow，否则可见桌宠会被每条 session 带着漂移。
  requestAnimationFrame(() => {
    if (lootCapture && slRows) slRows.scrollTop = slRows.scrollHeight;
  });
  SOUND.done();
  // 只记录这次显式入场。掠夺期间普通 stats/config 刷新不再重建列表，
  // 所以同一条动画不会被快照轮询反复从第一帧重启。
  lootCaptureTimers.push(setTimeout(() => {
    if (lootCapture && lootCapture.enteringSessionId === key) {
      lootCapture.enteringSessionId = '';
    }
  }, 620));
}

function markLootCaptureWaiting() {
  if (!lootCapture || lootCapture.ready) return;
  setLootBanner(lootCapture.sessions.length ? 'loot.preparing' : 'loot.noSessions', {
    done: lootCapture.sessions.length,
  });
}

function revealLootReady(count) {
  if (!lootCapture) return;
  lootCapture.ready = true;
  setLootBanner('loot.ready', { done: Number(count) || lootCapture.sessions.length });
}

function finishLootCapture(success) {
  if (!lootCapture) return;
  clearLootTimers();
  lootCapture.enteringSessionId = '';
  setLootBanner(success ? 'loot.captured' : 'loot.kept', { n: lootCapture.sessions.length });
  renderSessList();
  lootCaptureTimers.push(setTimeout(() => {
    lootCapture = null;
    if (slLoot) slLoot.classList.add('hidden');
    slTitle.textContent = t('sess.title');
    renderSessList();
    fitPopup(sesslist);
  }, success ? 1400 : 2200));
}

function renderSessList() {
  const list = sessionsForList();
  if (slTravelInbox) {
    const waitingLetters = (curSessions || []).filter((session) => (
      session &&
      session.sessionRole === 'travel' &&
      session.choice &&
      (session.state === 'waiting' || session.state === 'needsinput')
    )).length;
    slTravelInbox.textContent = waitingLetters
      ? `${t('travel.inboxEntry')} · ${waitingLetters}`
      : t('travel.inboxEntry');
    slTravelInbox.classList.toggle('has-letter', waitingLetters > 0);
  }
  slSub.textContent = lootCapture
    ? t('loot.countStreaming', { done: lootCapture.sessions.length })
    : (list.length ? t('sess.count', { n: list.length }) : '');
  slRows.innerHTML = '';
  if (!list.length) {
    const e = document.createElement('div');
    e.className = 'sl-empty';
    e.textContent = lootCapture ? t('loot.waiting') : t('sess.empty');
    slRows.appendChild(e);
    return;
  }
  for (const [index, s] of list.entries()) {
    const row = document.createElement('div');
    row.className = 'sl-row';
    if (lootCapture && sessionKey(s) === lootCapture.enteringSessionId) row.classList.add('loot-enter');
    const attn = s.state === 'waiting' || s.state === 'needsinput';
    // meta：等待类显示「等你…」；忙碌显示当前操作；其余只显示状态（不要把陈旧 op 显示成"处理中"）
    let meta;
    if (attn) meta = s.reason
      ? t(s.state === 'waiting' ? 'sess.waitFor' : 'sess.replyFor', { reason: reasonWord(s.reason) })
      : sessMeta(s.state);
    else if (s.state === 'working' || s.state === 'juggling' || s.state === 'sweeping' || s.state === 'thinking') meta = s.op || sessMeta(s.state);
    else if (s.badge === 'done') meta = t('sess.justDone');
    else if (s.badge === 'interrupted') meta = t('sess.interrupted');
    else meta = sessMeta(s.state) || s.state;
    const dotCls = sessionDotClass(s); // 与头顶小点同一套配色
    const ctx = typeof s.contextPercent === 'number'
      ? `<span class="sl-ctx ${ctxClass(s.contextPercent)}">${s.contextPercent}%</span>` : '';
    const key = sessionKey(s);
    const pinned = pinnedSessionIds.includes(key);
    const archived = archivedSessionIds.includes(key);
    row.innerHTML =
      `<span class="sl-dot ${dotCls}"></span>` +
      `<span class="sl-icon" title="OpenSquilla">${agentIcon()}</span>` +
      `<div class="sl-main"><div class="sl-name">${esc(s.project)}</div>` +
      `<div class="sl-meta ${attn ? 'attn' : ''}">${esc(meta)}</div></div>` +
      ctx +
      `<button class="sl-meme-entry" title="${esc(t('meme.entryTitle'))}">${esc(t('meme.entry'))}</button>` +
      `<button class="sl-travel-entry" title="${esc(t('travel.entryTitle'))}">🧳</button>` +
      `<span class="sl-actions">` +
      `<button class="sl-action pin ${pinned ? 'active' : ''}" title="${esc(t(pinned ? 'sess.unpin' : 'sess.pin'))}">★</button>` +
      `<button class="sl-action archive ${archived ? 'active' : ''}" title="${esc(t(archived ? 'sess.unarchive' : 'sess.archive'))}">▣</button>` +
      `</span>`;
    const memeBtn = row.querySelector('.sl-meme-entry');
    if (memeBtn) {
      memeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openMemePage(s);
      });
    }
    const travelBtn = row.querySelector('.sl-travel-entry');
    if (travelBtn) {
      travelBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openTravelPage(s);
      });
    }
    row.querySelector('.sl-action.pin').addEventListener('click', (e) => {
      e.stopPropagation();
      if (pinned) pinnedSessionIds = pinnedSessionIds.filter((id) => id !== key);
      else {
        pinnedSessionIds = [key, ...pinnedSessionIds.filter((id) => id !== key)];
        archivedSessionIds = archivedSessionIds.filter((id) => id !== key);
      }
      window.pet.setSessionPrefs(pinnedSessionIds, archivedSessionIds);
      renderSessList();
    });
    row.querySelector('.sl-action.archive').addEventListener('click', (e) => {
      e.stopPropagation();
      if (archived) archivedSessionIds = archivedSessionIds.filter((id) => id !== key);
      else {
        archivedSessionIds = [key, ...archivedSessionIds.filter((id) => id !== key)];
        pinnedSessionIds = pinnedSessionIds.filter((id) => id !== key);
      }
      window.pet.setSessionPrefs(pinnedSessionIds, archivedSessionIds);
      renderSessList();
    });
    row.addEventListener('click', () => {
      window.pet.focusSession(s.sessionId || '');
      rlog('sesslist', 'focus ' + (s.project || ''));
      closeSessList();
    });
    slRows.appendChild(row);
  }
}

async function loadMemeCatalog() {
  try {
    const next = await window.pet.getMemeCatalog();
    if (next && Array.isArray(next.items)) memeCatalog = next;
  } catch (err) {
    rlog('meme', 'catalog failed ' + (err && err.message ? err.message : err));
  }
  return memeCatalog;
}

function memeMediaUrl(meme, kind) {
  if (!meme || !meme.media || typeof meme.media[kind] !== 'string') return '';
  const base = `assets/memes/${meme.media[kind]}`;
  return meme.media.version ? `${base}?v=${encodeURIComponent(meme.media.version)}` : base;
}

function setMemeStatus(text, kind = '') {
  slMemeStatus.textContent = text || '';
  slMemeStatus.className = 'sl-meme-status' + (kind ? ' ' + kind : '');
}

async function openMemePage(session) {
  memeTarget = session;
  sesslist.classList.remove('session-list-mode');
  slSessionView.classList.add('hidden');
  slMemeView.classList.remove('hidden');
  slBack.classList.remove('hidden');
  slTitle.textContent = t('meme.pickTitle');
  slSub.textContent = '';
  slMemeSession.textContent = `OpenSquilla · ${session.project}`;
  slMemeGrid.innerHTML = '';
  setMemeStatus(t('meme.loading'));
  await loadMemeCatalog();
  if (!memeTarget || memeTarget.sessionId !== session.sessionId) return;
  slMemeGrid.innerHTML = '';
  for (const meme of memeCatalog.items) {
    const card = document.createElement('button');
    card.className = 'sl-meme-card';
    card.innerHTML =
      `<img class="sl-meme-thumb" src="${esc(memeMediaUrl(meme, 'gif'))}" alt="">` +
      `<span class="sl-meme-label">${esc(meme.label)}</span>` +
      `<span class="sl-meme-desc">${esc(meme.description)}</span>`;
    card.addEventListener('click', async (e) => {
      e.stopPropagation();
      card.disabled = true;
      setMemeStatus(t('meme.sending'));
      const target = memeTarget;
      closeSessList();
      let result;
      try {
        result = await window.pet.triggerMeme(target.sessionId, meme.id);
      } catch (err) {
        result = { ok: false, submitted: false, message: err && err.message ? err.message : t('meme.failed') };
      }
      const workReactionStarted = applyDeliveredMemeWorkReaction(meme, result);
      card.disabled = false;
      if (result && result.ok) {
        memeCaption.textContent = t(result.submitted ? 'meme.sent' : 'meme.copied', { label: meme.label });
      } else {
        memeCaption.textContent = (result && result.message) || t('meme.failed');
        if (memePlayer.classList.contains('hidden')) showBubble(memeCaption.textContent, 3600, true);
      }
      rlog(
        'meme',
        `${meme.id} target=${String(target.sessionId || '').slice(-6)} ` +
          `submitted=${!!(result && result.submitted)} inputSent=${!!(result && result.inputSent)} ` +
          `workReaction=${workReactionStarted}`,
      );
    });
    slMemeGrid.appendChild(card);
  }
  setMemeStatus(memeCatalog.items.length ? t('meme.hint') : t('meme.none'));
  fitPopup(sesslist);
}

function travelBelongsToThisPet(trip) {
  return !!trip && (AGENT === 'all' || AGENT === trip.agent);
}

function tokenRankText(rank, emptyKey = 'travel.rankEmpty') {
  if (!rank || !rank.units) return t(emptyKey);
  const parts = [];
  if (rank.crown) parts.push(rank.crown > 3 ? `👑×${rank.crown}` : '👑'.repeat(rank.crown));
  if (rank.sun) parts.push(rank.sun > 3 ? `☀️×${rank.sun}` : '☀️'.repeat(rank.sun));
  if (rank.moon) parts.push('🌙'.repeat(rank.moon));
  if (rank.star) parts.push('⭐'.repeat(rank.star));
  // Keep the persisted property name `leaf` for compatibility, but render an
  // amber paw instead of a green leaf.
  if (rank.leaf) parts.push('🐾'.repeat(rank.leaf));
  return parts.join(' ') || t(emptyKey);
}

function setTravelStatus(text, kind = '') {
  slTravelStatus.textContent = text || '';
  slTravelStatus.className = 'sl-travel-status' + (kind ? ' ' + kind : '');
}

function travelErrorText(code) {
  if (code === 'busy') return t('travel.busy');
  if (code === 'invalid-target' || code === 'foreign-target' || code === 'empty-mission') return t('travel.invalid');
  return t('travel.notReady');
}

function travelMailboxSessions() {
  return (curSessions || [])
    .filter((session) => (
      session &&
      session.sessionRole === 'travel' &&
      !session.headless &&
      (AGENT === 'all' || session.travelAgent === AGENT || session.agent === AGENT)
    ));
}

function renderTravelMailboxes() {
  if (!slTravelMailboxes) return;
  const sessions = travelMailboxSessions();
  const agents = ['squilla'];
  slTravelMailboxes.innerHTML = '';
  for (const agent of agents) {
    const session = sessions.find((item) => (item.travelAgent || item.agent) === agent) || null;
    const hasLetter = !!(
      session &&
      session.choice &&
      (session.state === 'waiting' || session.state === 'needsinput')
    );
    const active = travelData && travelData.active && travelData.active.agent === agent
      ? travelData.active
      : null;
    let meta = t('travel.mailboxDormant');
    if (hasLetter) meta = t('travel.mailboxWaiting');
    else if (active) meta = active.status === 'departing'
      ? t('travel.departing')
      : t('travel.traveling', {
        minutes: Math.max(0, Math.floor((Date.now() - Number(active.startedAt || Date.now())) / 60000)),
      });
    else if (session) {
      if (session.badge === 'done') meta = t('sess.justDone');
      else if (session.badge === 'interrupted') meta = t('sess.interrupted');
      else meta = sessMeta(session.state) || session.state || t('travel.mailboxReady');
    }

    const row = document.createElement('div');
    row.className = 'sl-travel-mailbox' + (hasLetter ? ' has-letter' : '') + (active ? ' active' : '');
    row.innerHTML =
      `<span class="sl-travel-mailbox-icon">🐾</span>` +
      `<div class="sl-travel-mailbox-main">` +
      `<strong>${esc(t('travel.sessionName', { who: 'OpenSquilla' }))}</strong>` +
      `<span>${esc(meta)}</span></div>` +
      (hasLetter
        ? `<button class="sl-travel-letter-open">${esc(t('travel.replyLetter'))}</button>`
        : session
          ? `<button class="sl-travel-terminal-open">${esc(t('travel.openMailbox'))}</button>`
          : `<span class="sl-travel-mailbox-new">${esc(t('travel.firstTripCreates'))}</span>`);
    const letter = row.querySelector('.sl-travel-letter-open');
    if (letter) {
      letter.addEventListener('click', (event) => {
        event.stopPropagation();
        const choice = session && session.choice;
        if (!choice) return;
        closeSessList();
        enqueueChoice(choice);
      });
    }
    const terminal = row.querySelector('.sl-travel-terminal-open');
    if (terminal) {
      terminal.addEventListener('click', (event) => {
        event.stopPropagation();
        window.pet.focusSession(session.sessionId || '');
        closeSessList();
      });
    }
    slTravelMailboxes.appendChild(row);
  }
}

const POSTCARD_ART = {
  mountain: [
    '                 /\\',
    '            /\\  /  \\   /\\',
    '       /\\  /  \\/ /\\ \\_/  \\',
    '      /  \\/    _/  \\_      \\',
    '  ___/^^^  \\___/^^^^^^\\__/^^^\\___',
    '     \\       /\\      /       /',
    '      \\_____/  \\____/\\______/',
    '          /\\      /\\      /\\',
    '         /__\\    /__\\    /__\\',
    '          ||      ||      ||',
    '             /\\_/\\',
    '            ( o.o )  *',
    '             > ^ <  /|\\',
  ],
  desert: [
    '         .       *       .',
    '             \\   |   /',
    '          ----  ( )  ----',
    '             /   |   \\',
    '       _..--\'\'       \'\'--.._',
    '   _.-\'                     `-._',
    '.-\'       _..---.._            `-.',
    '      _.-\'   (   ) `-._',
    '  _.-\'    (       )    `-._',
    '            /\\_/\\',
    '       _   ( o.o )   _',
    '    .-\'     > ^ <     `-.',
  ],
  coast: [
    '             |\\',
    '             | \\',
    '          ___|__\\___',
    '         /    []    \\',
    '        /_____[]_____\\',
    '           |  ||',
    '       ____|__||____       .',
    '  ~~~~/            \\~~~~~~~~',
    ' ~~~~~   /\\_/\\       ~~~~~~~~',
    '  ~~~   ( o.o )  __/\\__  ~~~~',
    '         > ^ <  /______\\',
    ' ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~',
  ],
  city: [
    '       |[]|       .-.',
    '   .---|[]|---.  |[]|   .----.',
    '   |[] |  | []|  |  |   | [] |',
    ' .-|[] |[]| []|--|[]|---| [] |-.',
    ' | |   |  |   |  |  |   |    | |',
    ' |_|___|__|___|__|__|___|____|_|',
    '        \\   |   /',
    '      ===\\==|==/===',
    '          /\\_/\\',
    '         ( o.o )',
    '          > ^ <',
  ],
  forest: [
    '       /\\        /\\       /\\',
    '      /**\\   /\\ /**\\     /**\\',
    '     /****\\ /**\\****\\ /\\****\\',
    '       ||  /****\\ ||  /**\\ ||',
    '   /\\  ||    ||   || /****\\||',
    '  /**\\ ||    ||   ||   ||  ||',
    ' /****\\||  .----. ||   ||  ||',
    '   ||  || /      \\||   ||  ||',
    '   ||    /\\_/\\    \\',
    '        ( o.o )    |',
    '         > ^ <    /',
    '   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~',
  ],
  museum: [
    '            /\\',
    '           /  \\',
    '      ____/____\\____',
    '     /______________\\',
    '       ||  ||  ||',
    '       ||  ||  ||',
    '       ||  ||  ||',
    '    ___||__||__||___',
    '   /________________\\',
    '       /\\_/\\   []',
    '      ( o.o ) /__\\',
    '       > ^ <',
  ],
  craft: [
    '      +-------------------+',
    '      |# # # # # # # # # #|',
    '      | # # # # # # # # # |',
    '      |# # # # # # # # # #|',
    '      +----+---------+----+',
    '           |         |',
    '        ___|_________|___',
    '       /      /\\_/\\      \\',
    '      |      ( o.o )      |',
    '      |       > ^ <       |',
    '       \\_________________/',
  ],
  generic: [
    '          .-------------.',
    '         /  .--.   *     \\',
    '        /  /    \\    .    \\',
    '       |  /  /\\  \\         |',
    '       | /__/  \\__\\  /\\_/\\ |',
    '       |             ( o.o )|',
    '       |     .----.   > ^ < |',
    '       |____/______\\_________|',
    '          /  /  \\  \\',
    '         /__/    \\__\\',
    '       ~~~~~~~~~~~~~~~~~',
  ],
};

const POSTCARD_ART_VARIANTS = {
  desert: [
    POSTCARD_ART.desert,
    [
      '          .--.       .--.',
      '       .-(    ).   .(    )-.',
      '      (___.__)__) (___.__)__)',
      '          \\\\ | /     \\\\ | /',
      '       _..-\\\\|/--..__\\\\|/--.._',
      '   _.-\'      o       o       `-._',
      '.-\'       o     o       o        `-.',
      '       _..---.._   _..---.._',
      '     .\'  /\\_/\\  `.\'         `.',
      '    /   ( o.o )   \\\\   o      \\\\',
      '   /     > ^ <     \\\\      o   \\\\',
      '  `-----------------------------\'',
    ],
    [
      '        _..---~~~~---.._',
      '   _.-\'      .   .      `-._',
      '.-\'_____( )_______( )_______`-.',
      '          \\\\       /',
      '           \\\\_.-._/',
      '         .-\'  o o `-.',
      '       _/  .-.___.-. \\\\_',
      '      /___/  /   \\\\  \\\\___\\\\',
      '         /__/     \\\\__\\\\',
      '       /\\_/\\   _/\\_',
      '      ( o.o ) /____\\\\',
      '       > ^ <   ||||',
    ],
    [
      '    Y   Y      Y       Y   Y',
      '   \\\\|/ \\\\|/  .------.  \\\\|/ \\\\|/',
      ' Y--*---*--/        \\\\--*---*--Y',
      '   /|\\\\ /|\\\\|   .-.   |/|\\\\ /|\\\\',
      '          |  (   )  |',
      '   Y   Y  \\\\   `-\'  /  Y   Y',
      '  \\\\|/ \\\\|/  `------\'  \\\\|/ \\\\|/',
      '   *---*      /\\_/\\      *---*',
      '  /|\\\\ /|\\\\    ( o.o )    /|\\\\ /|\\\\',
      '              > ^ <',
      '       _..--\'     `--.._',
      '  _.-\'___________________`-._',
    ],
  ],
};

function postcardArtKey(words = '') {
  const text = String(words || '').toLocaleLowerCase();
  if (/(珠穆朗玛|珠峰|喜马拉雅|everest|himalaya|mountain|山峰|雪山|登山)/i.test(text)) {
    return 'mountain';
  }
  if (/(纳米布|沙漠|沙丘|desert|dune|fairy circle|仙女圈|荒漠|白蚁|termite|linyji|皮尔巴拉|pilbara|spinifex|刺叶草)/i.test(text)) {
    return 'desert';
  }
  if (/(海|岛|港|灯塔|coast|ocean|sea|island|harbour|harbor|lighthouse)/i.test(text)) {
    return 'coast';
  }
  if (/(森林|树|生态|雨林|forest|woodland|jungle|tree)/i.test(text)) {
    return 'forest';
  }
  if (/(博物馆|建筑|神殿|museum|gallery|temple|palace|遗址)/i.test(text)) {
    return 'museum';
  }
  if (/(手艺|织|陶|工坊|craft|weav|potter|workshop|传统)/i.test(text)) {
    return 'craft';
  }
  if (/(城市|小城|街|市场|city|town|street|market|社区)/i.test(text)) {
    return 'city';
  }
  return '';
}

function mirrorPostcardArt(lines) {
  const swap = { '/': '\\', '\\': '/', '(': ')', ')': '(', '<': '>', '>': '<', '[': ']', ']': '[' };
  return lines.map((line) => [...line].reverse().map((char) => swap[char] || char).join(''));
}

function fallbackPostcardArt(trip, scene = '', index = 0) {
  // A stop's own place name must win over broader trip context. Otherwise one
  // Everest mention in a multi-stop trip turns every following card into a
  // mountain postcard.
  const tripWords = `${
    trip && trip.project || ''
  } ${
    trip && trip.mission || ''
  } ${
    String(trip && trip.result || '').slice(0, 1600)
  }`;
  const key = postcardArtKey(scene) || postcardArtKey(tripWords) || 'generic';
  const variantNumber = Math.abs(Number(index) || 0);
  const variants = POSTCARD_ART_VARIANTS[key];
  if (variants && variants.length) {
    const lines = [...variants[variantNumber % variants.length]];
    if (variantNumber >= variants.length && lines.length < 16) {
      lines.unshift(variantNumber % 2 ? '      ·       *        .' : '   *        .       ·');
    }
    return lines.join('\n');
  }
  const base = POSTCARD_ART[key] || POSTCARD_ART.generic;
  const lines = variantNumber % 2 ? mirrorPostcardArt(base) : [...base];
  if (variantNumber >= 2 && lines.length < 16) {
    lines.unshift(variantNumber % 3 === 2 ? '      .       *       .' : '   *       ·        .');
  }
  return lines.join('\n');
}

function usablePostcardArt(value) {
  const lines = String(value || '').trim().split('\n');
  if (lines.length < 8 || lines.length > 16) return false;
  if (lines.some((line) => [...line].length > 48)) return false;
  const ink = lines.join('').replace(/[\s\p{L}\p{N}]/gu, '');
  return new Set(ink).size >= 5;
}

function postcardExcerpt(value) {
  let source = String(value || '')
    .replace(/\[([^\]]+)\]\((?:https?:\/\/)?[^)]+\)/g, '$1')
    .replace(/^#{1,4}\s+[^\n]+\n*/gm, '')
    .replace(/\n(?:核实(?:于|日期)?|公开资料|参考资料|Sources?|References?|確認資料|出典)[:：]?[^\n]*[\s\S]*$/i, '')
    .replace(/^[*-]\s+/gm, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  const hanCount = (source.match(/[\u3040-\u30ff\u3400-\u9fff]/g) || []).length;
  const limit = hanCount > source.length * 0.18 ? 300 : 620;
  if (source.length <= limit) return source;
  const chunks = source.match(/[^。！？.!?\n]+[。！？.!?]?|\n+/g) || [source];
  let result = '';
  for (const chunk of chunks) {
    const candidate = result + chunk;
    if (candidate.length > limit - 2) break;
    result = candidate;
  }
  if (!result.trim()) result = source.slice(0, limit - 2);
  return `${result.trim()}…`;
}

function postcardStopFromBlock(block, trip, index, prefix = '') {
  const source = String(block || '').replace(/<!--\s*PET_STOP\s*-->/ig, '').trim();
  const heading = /^\s*#{1,4}\s+([^\n]+)/m.exec(source);
  const firstLine = String((source.split(/\n+/)[0] || '')).replace(/^#{1,4}\s*/, '').trim();
  const title = (heading && heading[1] || firstLine || t('travel.stopN', { n: index + 1 }))
    .replace(/\s+/g, ' ')
    .slice(0, 54);
  const fenced = /```(?:text|ascii(?:-art)?)?[ \t]*\n([\s\S]*?)```/i.exec(source);
  const candidateArt = fenced ? String(fenced[1] || '').trim() : '';
  let text = source;
  if (fenced) text = `${text.slice(0, fenced.index)}${text.slice(fenced.index + fenced[0].length)}`;
  if (heading) text = text.replace(heading[0], '');
  text = `${prefix ? `${prefix.trim()}\n\n` : ''}${text.trim()}`.trim();
  return {
    title,
    art: usablePostcardArt(candidateArt)
      ? candidateArt
      : fallbackPostcardArt(trip, `${title}\n${text}`, index),
    text: postcardExcerpt(text),
  };
}

function splitLegacyTravelStops(source) {
  // Older travel replies were prose, so a stop marker can sit inside a
  // sentence ("可第三站…", "去了第四站…") instead of at a paragraph start.
  // New replies use PET_STOP and never depend on this compatibility path.
  const marker = /第(?:[一二三四五六七八九十百]+|\d+)站|Stop\s+\d+|第\d+停留地/gim;
  const found = [];
  let match;
  while ((match = marker.exec(source))) {
    found.push(match.index);
  }
  if (found.length < 2) return null;
  return {
    prefix: source.slice(0, found[0]).trim(),
    blocks: found.map((start, index) => source.slice(start, found[index + 1] || source.length).trim()),
  };
}

function splitTravelPostcard(trip) {
  const source = String((trip && (trip.result || trip.error)) || '').trim();
  const marked = source.split(/<!--\s*PET_STOP\s*-->/i).slice(1).map((part) => part.trim()).filter(Boolean);
  let stops;
  if (marked.length) {
    stops = marked.map((block, index) => postcardStopFromBlock(block, trip, index));
  } else {
    const legacy = splitLegacyTravelStops(source);
    stops = legacy
      ? legacy.blocks.map((block, index) => (
      postcardStopFromBlock(block, trip, index, index === 0 ? legacy.prefix : '')
      ))
      : [postcardStopFromBlock(source, trip, 0)];
  }
  const seen = new Set();
  return stops.map((stop, index) => {
    let art = stop.art;
    let signature = String(art || '').replace(/\s+/g, '');
    if (!signature || seen.has(signature)) {
      art = fallbackPostcardArt(trip, `${stop.title}\n${stop.text}`, index);
      signature = String(art || '').replace(/\s+/g, '');
    }
    let alternate = index + 1;
    while (seen.has(signature) && alternate < index + 7) {
      art = fallbackPostcardArt(trip, `${stop.title}\n${stop.text}`, alternate++);
      signature = String(art || '').replace(/\s+/g, '');
    }
    seen.add(signature);
    return { ...stop, art };
  });
}

function updateTravelStopNav(stops) {
  const count = Array.isArray(stops) ? stops.length : 0;
  selectedPostcardStop = Math.max(0, Math.min(selectedPostcardStop, Math.max(0, count - 1)));
  slTravelStopPage.textContent = count
    ? t('travel.stopPage', { current: selectedPostcardStop + 1, total: count })
    : '';
  slTravelStopPrev.disabled = selectedPostcardStop <= 0;
  slTravelStopNext.disabled = selectedPostcardStop >= count - 1;
}

function goTravelStop(index) {
  const cards = slTravelStopTrack ? [...slTravelStopTrack.children] : [];
  if (!cards.length) return;
  selectedPostcardStop = Math.max(0, Math.min(Number(index) || 0, cards.length - 1));
  cards.forEach((card, cardIndex) => {
    card.classList.toggle('active', cardIndex === selectedPostcardStop);
    card.setAttribute('aria-hidden', cardIndex === selectedPostcardStop ? 'false' : 'true');
  });
  updateTravelStopNav(cards);
}

function renderTravelPostcard(trip) {
  const source = String((trip && (trip.result || trip.error)) || '');
  const key = `${trip && trip.id || ''}|${source.length}|${source.slice(0, 48)}`;
  if (key === renderedPostcardKey && slTravelStopTrack.children.length) {
    updateTravelStopNav([...slTravelStopTrack.children]);
    return;
  }
  renderedPostcardKey = key;
  const stops = splitTravelPostcard(trip);
  slTravelStopTrack.innerHTML = '';
  for (let index = 0; index < stops.length; index++) {
    const stop = stops[index];
    const card = document.createElement('article');
    card.className = 'sl-travel-stop-card';
    card.dataset.stop = String(index);
    card.innerHTML =
      `<div class="sl-travel-stop-title">${esc(stop.title)}</div>` +
      `<div class="sl-travel-postcard-body">` +
      `<pre class="sl-travel-postcard-art">${esc(stop.art)}</pre>` +
      `<div class="sl-travel-postcard-text">${esc(stop.text)}</div>` +
      `</div>`;
    slTravelStopTrack.appendChild(card);
  }
  selectedPostcardStop = 0;
  updateTravelStopNav(stops);
  goTravelStop(0);
}

function selectedTravelPostcard() {
  const all = Array.isArray(travelPostcards) ? travelPostcards : [];
  if (!all.length) {
    const latest = travelData && travelData.latest;
    return latest && latest.status === 'completed' && latest.result ? latest : null;
  }
  return all.find((trip) => trip.id === selectedPostcardId) || all[0];
}

function renderTravelHistory() {
  if (!slTravelHistory) return;
  const all = Array.isArray(travelPostcards) ? travelPostcards : [];
  slTravelHistory.innerHTML = '';
  if (!all.length) {
    const empty = document.createElement('div');
    empty.className = 'sl-travel-history-empty';
    empty.textContent = t('travel.noPostcard');
    slTravelHistory.appendChild(empty);
    return;
  }
  if (!selectedPostcardId || !all.some((trip) => trip.id === selectedPostcardId)) {
    selectedPostcardId = all[0].id;
  }
  for (const trip of all) {
    const card = document.createElement('button');
    const statusKey = trip.status === 'completed'
      ? 'travel.completed'
      : trip.status === 'cancelled' ? 'travel.cancelled' : 'travel.failed';
    card.className = 'sl-travel-history-card' + (trip.id === selectedPostcardId ? ' active' : '');
    card.innerHTML =
      `<span>🐾</span>` +
      `<div><strong>${esc(trip.project || t('travel.postcard'))}</strong>` +
      `<small>${esc(t(statusKey))} · ${esc(compactTokens(trip.usage && trip.usage.tokens || 0))} token</small></div>`;
    card.addEventListener('click', (event) => {
      event.stopPropagation();
      selectedPostcardId = trip.id;
      selectedPostcardStop = 0;
      renderedPostcardKey = '';
      renderTravelPage();
    });
    slTravelHistory.appendChild(card);
  }
}

function renderTravelTemplates() {
  const available = (travelData && travelData.templates) || [];
  if (!available.length) return;
  if (!travelTemplateId || !available.some((item) => item.id === travelTemplateId)) {
    travelTemplateId = available[0].id;
  }
  slTravelTemplates.innerHTML = '';
  for (const item of available) {
    const card = document.createElement('button');
    card.className = 'sl-travel-template' + (item.id === travelTemplateId ? ' active' : '');
    card.innerHTML = `<strong>${esc(item.label)}</strong><span>${esc(item.description)}</span>`;
    card.addEventListener('click', (e) => {
      e.stopPropagation();
      travelTemplateId = item.id;
      travelMissionDirty = false;
      slTravelMission.value = item.mission || '';
      renderTravelTemplates();
    });
    slTravelTemplates.appendChild(card);
  }
  if (!travelMissionDirty && !slTravelMission.value) {
    const selected = available.find((item) => item.id === travelTemplateId) || available[0];
    slTravelMission.value = selected.mission || '';
  }
}

function renderTravelPage() {
  if (!slTravelView || slTravelView.classList.contains('hidden')) return;
  const data = travelData || { growth: {}, templates: [] };
  const growth = data.growth || {};
  const rank = growth.rank || {};
  const active = data.active || null;
  const shownTarget = active || travelTarget;

  slTravelSession.textContent = shownTarget
    ? `OpenSquilla · ${shownTarget.project || ''}`
    : t('travel.inboxIntro');
  slTravelRankIcons.textContent = tokenRankText(rank);
  const remaining = Math.max(0, (Number(rank.nextTokens) || 10000) - (Number(rank.progressTokens) || 0));
  slTravelRankMeta.textContent =
    t('travel.rankTokens', { tokens: compactTokens(growth.totalTokens || 0), trips: growth.completed || 0 }) +
    '\n' + t('travel.nextRank', { tokens: compactTokens(remaining) });

  const machine = (lastStats && lastStats.machineGrowth) || {};
  const machineRank = machine.rank || {};
  const machineRemaining = Math.max(
    0,
    (Number(machineRank.nextTokens) || 10000000) - (Number(machineRank.progressTokens) || 0),
  );
  slMachineRankIcons.textContent = tokenRankText(machineRank, 'travel.machineRankEmpty');
  slMachineRankMeta.textContent =
    t('travel.machineRankTokens', {
      tokens: compactTokens(machine.totalTokens || 0),
    }) +
    '\n' + t('travel.nextMachineRank', { tokens: compactTokens(machineRemaining) });

  renderTravelMailboxes();
  if (slWander) {
    slWander.disabled = !!active;
    slWander.title = active ? t('travel.busy') : t('travel.wanderTitle');
  }

  if (active) {
    slTravelSetup.classList.add('hidden');
    slTravelActive.classList.remove('hidden');
    const minutes = Math.max(0, Math.floor((Date.now() - Number(active.startedAt || Date.now())) / 60000));
    slTravelActiveStatus.textContent = active.status === 'departing'
      ? t('travel.departing')
      : t('travel.traveling', { minutes });
    slTravelActiveMission.textContent = active.mission || '';
    slTravelCancel.classList.toggle('hidden', !travelBelongsToThisPet(active));
    setTravelStatus(travelBelongsToThisPet(active) ? '' : t('travel.busy'));
  } else if (travelTarget) {
    slTravelSetup.classList.remove('hidden');
    slTravelActive.classList.add('hidden');
    renderTravelTemplates();
  } else {
    slTravelSetup.classList.add('hidden');
    slTravelActive.classList.add('hidden');
  }

  renderTravelHistory();
  const latest = selectedTravelPostcard();
  if (latest && latest.status === 'completed' && latest.result) {
    slTravelPostcard.classList.remove('hidden');
    slTravelPostcardMeta.textContent =
      `${t('travel.completed')} · ${compactTokens(latest.usage && latest.usage.tokens || 0)} token`;
    renderTravelPostcard(latest);
  } else {
    slTravelPostcard.classList.add('hidden');
    renderedPostcardKey = '';
    if (slTravelStopTrack) slTravelStopTrack.innerHTML = '';
  }
  fitPopup(sesslist);
}

async function openTravelPage(session) {
  travelTarget = session || null;
  sesslist.classList.remove('session-list-mode');
  travelMissionDirty = false;
  travelTemplateId = null;
  slSessionView.classList.add('hidden');
  slMemeView.classList.add('hidden');
  slTravelView.classList.remove('hidden');
  slBack.classList.remove('hidden');
  slTitle.textContent = session ? t('travel.pickTitle') : t('travel.inboxTitle');
  slSub.textContent = '';
  slTravelMission.value = '';
  setTravelStatus('');
  try {
    const loaded = await Promise.all([
      window.pet.getTravel(),
      typeof window.pet.getTravelPostcards === 'function'
        ? window.pet.getTravelPostcards()
        : Promise.resolve([]),
    ]);
    travelData = loaded[0];
    travelPostcards = Array.isArray(loaded[1]) ? loaded[1] : [];
    if (
      !travelPostcards.length &&
      travelData &&
      travelData.latest &&
      travelData.latest.status === 'completed' &&
      travelData.latest.result
    ) {
      travelPostcards = [travelData.latest];
    }
    selectedPostcardId = travelPostcards[0] ? travelPostcards[0].id : null;
    selectedPostcardStop = 0;
    renderedPostcardKey = '';
  } catch {
    travelData = null;
    travelPostcards = [];
    setTravelStatus(t('travel.notReady'), 'error');
  }
  renderTravelPage();
}

function openTravelInbox() {
  return openTravelPage(null);
}

function showSessionPage() {
  memeTarget = null;
  travelTarget = null;
  sesslist.classList.add('session-list-mode');
  slMemeView.classList.add('hidden');
  slTravelView.classList.add('hidden');
  slSessionView.classList.remove('hidden');
  slBack.classList.add('hidden');
  slTitle.textContent = t('sess.title');
  renderSessList();
  fitPopup(sesslist);
}

function openSessList() {
  if (radialOpen) closeRadial();
  if (todoPopOpen) closeTodoPop();
  hideAsk();
  showSessionPage();
  sesslist.classList.remove('hidden');
  sessListOpen = true;
  rlog('sesslist', 'open ' + visibleSessions().length);
  fitPopup(sesslist); // 动态定高 + 440 宽，会话名不截断
}
function closeSessList() {
  if (!sessListOpen) return;
  sesslist.classList.add('hidden');
  sessListOpen = false;
  memeTarget = null;
  travelTarget = null;
  rlog('sesslist', 'close');
  resetPetSize();
}

function alignMemePlayer() {
  if (!memeLayoutActive || memePlayer.classList.contains('hidden')) return;
  const petEl = curSkinEl();
  if (!petEl) return;
  const petRect = petEl.getBoundingClientRect();
  const docEl = document.documentElement;
  const viewportW = Math.max(1, window.innerWidth || (docEl && docEl.clientWidth) || MEME_WINDOW_W);
  const viewportH = Math.max(1, window.innerHeight || (docEl && docEl.clientHeight) || MEME_WINDOW_H);
  const naturalW = Number(memeImage.naturalWidth) || 16;
  const naturalH = Number(memeImage.naturalHeight) || 9;
  const availableRight = viewportW - petRect.right - MEME_GAP - MEME_EDGE_PAD;
  const availableLeft = petRect.left - MEME_GAP - MEME_EDGE_PAD;
  const preferred = currentMemePlacement === 'pet-left' ? 'left' : 'right';
  let side = preferred;
  if (side === 'right' && availableRight < 120 && availableLeft > availableRight) side = 'left';
  if (side === 'left' && availableLeft < 120 && availableRight > availableLeft) side = 'right';
  const available = Math.max(120, side === 'right' ? availableRight : availableLeft);
  const mediaW = Math.min(MEME_MEDIA_W, available);
  const mediaH = Math.min(180, mediaW * naturalH / naturalW);
  let left = side === 'right'
    ? petRect.right + MEME_GAP
    : petRect.left - MEME_GAP - mediaW;
  let top = petRect.top + (petRect.height - mediaH) / 2;
  left = Math.max(MEME_EDGE_PAD, Math.min(left, viewportW - mediaW - MEME_EDGE_PAD));
  // Caption sits below the image; reserve a small footer so it cannot be cut.
  top = Math.max(MEME_EDGE_PAD, Math.min(top, viewportH - mediaH - 34));
  memePlayer.style.left = `${Math.round(left)}px`;
  memePlayer.style.top = `${Math.round(top)}px`;
  memePlayer.style.width = `${Math.round(mediaW)}px`;
  memePlayer.dataset.side = side;
}

function restoreSizeAfterMeme() {
  if (askActive) fitPopup(askEl);
  else if (sessListOpen) fitPopup(sesslist);
  else if (todoPopOpen) fitPopup(todopop);
  else if (!bubble.classList.contains('hidden')) fitPopup(bubble);
  else resetPetSize();
}

let currentMemePlacement = 'pet-right';
function playMeme(meme) {
  if (!meme || !meme.media) return;
  clearTimeout(memeTimer);
  if (memeAudio) {
    try { memeAudio.pause(); } catch {}
    memeAudio = null;
  }
  memeLayoutActive = true;
  currentMemePlacement = meme.media.placement === 'pet-left' ? 'pet-left' : 'pet-right';
  memeImage.src = memeMediaUrl(meme, 'gif');
  memeImage.alt = meme.label || t('meme.fallbackLabel');
  memeCaption.textContent = `${meme.label || t('meme.fallbackLabel')} · ${meme.project || ''}`;
  memePlayer.classList.remove('hidden');
  setRequestedPetSize(MEME_WINDOW_W, MEME_WINDOW_H);
  if (meme.reaction && meme.reaction.state) {
    transient(meme.reaction.state, Number(meme.reaction.durationMs) || Number(meme.media.durationMs) || 3000);
  }
  // Start the longer work visual at the same instant as the meme response.
  // The dispatch result later confirms it (submitted/inputSent) or cancels it
  // on a real delivery failure, so there is no transcript-lag gap.
  startMemeWorkReaction(meme.reaction && meme.reaction.work);
  requestAnimationFrame(() => requestAnimationFrame(alignMemePlayer));
  if (!muted && typeof window.Audio === 'function') {
    try {
      memeAudio = new window.Audio(memeMediaUrl(meme, 'audio'));
      memeAudio.volume = 0.9;
      const p = memeAudio.play();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch {}
  }
  memeTimer = setTimeout(() => {
    memePlayer.classList.add('hidden');
    memeImage.removeAttribute('src');
    if (memeAudio) { try { memeAudio.pause(); } catch {} }
    memeAudio = null;
    memeLayoutActive = false;
    restoreSizeAfterMeme();
  }, Number(meme.media.durationMs) || 3000);
}

memeImage.addEventListener('load', alignMemePlayer);
window.pet.onMeme(playMeme);
if (typeof window.pet.onMemeCatalogChanged === 'function') {
  window.pet.onMemeCatalogChanged(() => {
    clearTimeout(memeCatalogRefreshTimer);
    memeCatalogRefreshTimer = setTimeout(async () => {
      await loadMemeCatalog();
      if (sessListOpen && memeTarget) await openMemePage(memeTarget);
    }, 80);
  });
}
function toggleSessList() { sessListOpen ? closeSessList() : openSessList(); }

// 工具 -> 干活动作；道具 emoji 的运动变体
const TOOL_ACT = {
  Edit: 'type', MultiEdit: 'type', Write: 'type', NotebookEdit: 'type',
  Read: 'read',
  Bash: 'crank',
  Grep: 'search', Glob: 'search',
  WebSearch: 'web', WebFetch: 'web',
  Task: 'summon', Agent: 'summon',
  TodoWrite: 'check',
};
const ACT_CLASSES = ['act-type', 'act-read', 'act-search', 'act-crank', 'act-web', 'act-summon', 'act-check', 'act-work'];
const PROP_MOTION = { crank: 'spin', web: 'spin', search: 'hunt', type: 'jit' };
let actTimer = null;

let state = 'idle';
let bubbleTimer = null;
let blinkTimer = null;
let transientUntil = 0;   // 短暂状态（happy/error）持续到的时间
let transientState = null;
let muted = false;
let skin = 'cat';
let lastWaiting = 0;
let lastBgZombie = 0; // 后台疑似僵尸数
let radialOpen = false;

const IDLE_SLEEP_MS = 6 * 60 * 1000;
const stateEls = [pixel, mascot, cat].filter(Boolean);
const DEBUG_STATE = null; // 调试用：强制某状态（如 'sleeping'）；正常运行设为 null
const DEBUG_CONFETTI = false; // 临时：定时放彩带验证；验证完改回 false

// ---------- 像素小怪兽 ----------
const PIXEL_MAP = [
  '..##############..',
  '..##############..',
  '..##############..',
  '#####OO####OO#####',
  '#####OO####OO#####',
  '..##############..',
  '..##############..',
  '..##############..',
  '..##############..',
  '...##.##..##.##...',
  '...##.##..##.##...',
];
function buildPixel() {
  if (!pixel) return;
  const sprite = pixel.querySelector('.pixel-sprite');
  const rows = PIXEL_MAP.length;
  const cols = PIXEL_MAP[0].length;
  const cell = 9;
  const W = cols * cell;
  const H = rows * cell;
  let rects = '';
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const c = PIXEL_MAP[y][x];
      if (c === '.') continue;
      const fill = c === 'O' ? '#2a1b2e' : '#c2694a';
      rects += `<rect x="${x * cell}" y="${y * cell}" width="${cell}" height="${cell}" fill="${fill}"/>`;
    }
  }
  sprite.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${rects}</svg>`;
}
buildPixel();

// ---------- 状态机（作用于两种形象，仅当前皮肤可见） ----------
// 前端会 setState 的全部状态词（聚合态 + 短暂态 + 情绪态）——统一取自
// shared/states.js（pet.html 以 <script> 在 pet.js 之前加载它）。classList.remove
// 必须覆盖此全集，漏一个就会 class 残留在皮肤元素上。
const STATE_WORDS = (window.OctoStates && window.OctoStates.RENDER_STATE_WORDS) || [];
function setState(s) {
  if (state === s) {
    // 语义状态没变，限时视觉层仍可能刚刚到期；同状态快照也要让猫
    // 重新选图，否则 30s 的高压工作姿态会一直拖到下一次状态切换。
    if (skin === 'cat') updateCat(s);
    return;
  }
  for (const el of stateEls) {
    el.classList.remove(...STATE_WORDS);
    el.classList.add(s);
  }
  state = s;
  rlog('state', s);
  thinkEl.classList.toggle('on', s === 'thinking');
  sleepEl.classList.toggle('on', s === 'sleeping');
  if (s === 'thinking' || s === 'sleeping') bubble.classList.add('hidden');
  if (s === 'working') {
    // 进入干活态 → 立刻挂上「持续忙碌」基线动作，不等具体 tool 事件，
    // 任何时刻都显得在忙（具体 tool 动作会在它之上叠加，结束后回落到这里）。
    for (const el of stateEls) el.classList.add('act-work');
  } else {
    clearAction(); // 离开干活态才清掉动作
  }
  // 注意：不要在这里 hideAsk()！面板显隐只由 refreshAsk(按是否有待答事项) 管。
  // 之前「s!=='waiting' 就 hideAsk」会在聚合态变 working/thinking 时把 needsinput 的面板闪掉。
  if (skin === 'mascot') updateMascotEyes(s);
  if (skin === 'cat') updateCat(s);
}

// 按工具播放专属动作 + 头顶道具
function playAction(toolName, icon) {
  if (state === 'waiting' || state === 'sleeping') return;
  const act = TOOL_ACT[toolName] || 'work';
  for (const el of stateEls) {
    el.classList.remove(...ACT_CLASSES);
    el.classList.add('act-' + act); // 通用 work 也有身体动作（不再只闪图标）
  }
  if (icon) {
    propEl.textContent = icon;
    propEl.className = 'prop';
    void propEl.offsetWidth; // 重启动画
    const pm = PROP_MOTION[act];
    propEl.className = 'prop on' + (pm ? ' ' + pm : '');
  }
  if (act === 'summon') {
    sidekickEl.classList.remove('on');
    void sidekickEl.offsetWidth;
    sidekickEl.classList.add('on');
  }
  clearTimeout(actTimer);
  actTimer = setTimeout(clearAction, 2200);
}
function clearAction() {
  for (const el of stateEls) el.classList.remove(...ACT_CLASSES);
  propEl.classList.remove('on');
  // 具体 tool 动作结束后，仍在干活 → 回落到「持续忙碌」基线，别安静下来
  if (state === 'working') for (const el of stateEls) el.classList.add('act-work');
}

// 短暂状态：happy/error/greet…，到点后由 applyStats 接管。
// 到期不再干等下一个快照（周期推送最坏 ~4s，短暂态会拖尾）——
// 定时用最近一次快照主动重算聚合态，到点即回落。
let transientTimer = null;
function transient(s, ms, text, holdMs) {
  if (state === 'waiting') return; // 等用户优先
  transientState = s;
  transientUntil = perfNow() + ms;
  setState(s);
  clearTimeout(transientTimer);
  transientTimer = setTimeout(() => { if (lastStats) applyStats(lastStats); }, ms + 30);
  if (text) showBubble(text, holdMs || ms);
}
// 高优先级稳态（waiting/needsinput/error）接管时清掉残留短暂态，
// 否则 talking/thinking 会在下个快照借 transientUntil 复活盖回来。
function clearTransient() {
  transientUntil = 0;
  clearTimeout(transientTimer);
}

// ---------- 声音提示（Web Audio 合成，无需音频文件） ----------
let audioCtx = null;
function beep(freqs, dur = 0.13, type = 'sine', gain = 0.06) {
  if (muted) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    let t = audioCtx.currentTime;
    for (const f of freqs) {
      const o = audioCtx.createOscillator();
      const gnode = audioCtx.createGain();
      o.type = type;
      o.frequency.value = f;
      gnode.gain.setValueAtTime(0, t);
      gnode.gain.linearRampToValueAtTime(gain, t + 0.012);
      gnode.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(gnode);
      gnode.connect(audioCtx.destination);
      o.start(t);
      o.stop(t + dur);
      t += dur * 0.92;
    }
  } catch {}
}
const SOUND = {
  waiting: () => beep([660, 880], 0.2, 'sine', 0.08), // 上行提示音
  done: () => beep([784, 1047], 0.15, 'triangle', 0.06), // 愉快叮咚
  error: () => beep([220, 165], 0.2, 'sawtooth', 0.05), // 低沉
  greet: () => beep([523, 784], 0.13, 'sine', 0.05), // 招呼
  bigDone: () => beep([659, 784, 988, 1319], 0.13, 'triangle', 0.07), // 上行小号角
};

// 大任务完成的彩带
function confetti() {
  const el = curSkinEl();
  const sr = stage.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const cx = r.left - sr.left + r.width / 2;
  const cy = r.top - sr.top + r.height * 0.35;
  const emojis = ['🎉', '✨', '⭐', '🧡', '🎊'];
  for (let i = 0; i < 12; i++) {
    const s = document.createElement('span');
    s.className = 'confetti';
    s.textContent = emojis[i % emojis.length];
    const ang = -Math.PI / 2 + (Math.random() - 0.5) * 1.8; // 向上扇形
    const dist = 45 + Math.random() * 70;
    s.style.left = cx + 'px';
    s.style.top = cy + 'px';
    s.style.fontSize = 12 + Math.random() * 12 + 'px';
    s.style.setProperty('--dx', Math.cos(ang) * dist + 'px');
    s.style.setProperty('--dy', Math.sin(ang) * dist + 'px');
    s.style.animationDelay = Math.random() * 0.12 + 's';
    stage.appendChild(s);
    setTimeout(() => s.remove(), 1300);
  }
}

function showBubble(text, holdMs = 3200, force = false) {
  if (!force && (muted || radialOpen || askActive)) return; // 选项面板开着时不弹气泡盖住它(force=重要提示强制显示)
  // emoji → 内联 SVG（OctoIcons 在 emoji 字符与 SVG 之间做安全替换；不可识别字符原样保留）
  if (window.OctoIcons && window.OctoIcons.hasMappedEmoji(text)) {
    window.OctoIcons.setTextWithIcons(bubbleText, text);
  } else {
    bubbleText.textContent = text;
  }
  bubble.classList.remove('hidden');
  bubble.scrollTop = 0; // 重置滚动到顶（上次长气泡可能滚到了下边）
  // 大段文字：把窗口按实际高度撑开（fitPopup 已按屏幕封顶，永远不顶出屏幕；
  // 实在超屏时由 #bubble 自身 overflow-y:auto 内滚动兜底）。
  fitPopup(bubble);
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(hideBubble, holdMs);
}
function hideBubble() {
  bubble.classList.add('hidden');
  // 若没有其它弹层占用大窗口尺寸，恢复原始尺寸（避免 pet 一直停在加大窗口里）
  if (!askActive && !sessListOpen && !todoPopOpen) resetPetSize();
}

function scheduleBlink() {
  blinkTimer = setTimeout(() => {
    // 仅像素怪兽保留 class 眨眼位（cat 是 GIF 自带动效；mascot 之前的
    // 「眨眼」是把整幅工作场景换成闭眼底图 150ms，观感是画面闪断，已移除）。
    if (skin === 'pixel' && state !== 'sleeping' && state !== 'waiting') {
      pixel.classList.add('blink');
      setTimeout(() => pixel.classList.remove('blink'), 160);
    }
    scheduleBlink();
  }, 2500 + Math.random() * 4000);
}
scheduleBlink();

// 空闲小动作：闲着时偶尔东张西望 / 蹦一下，更有生命感
function scheduleIdleAction() {
  setTimeout(() => {
    if (state === 'idle' && !radialOpen && !muted) {
      // 只有像素怪兽有 peek 动画；mascot 的 glance CSS 指向已不存在的
      // #teyes（img 皮肤没有 SVG 眼睛节点），cat 由 GIF 自带动效。
      if (skin === 'pixel') {
        pixel.classList.add('peek');
        setTimeout(() => pixel.classList.remove('peek'), 620);
      }
    }
    scheduleIdleAction();
  }, 7000 + Math.random() * 7000);
}
scheduleIdleAction();

const curSkinEl = () => (skin === 'pixel' ? pixel : skin === 'cat' ? cat : mascot);

window.pet.onTravel((event) => {
  if (!event) return;
  if (event.state) travelData = event.state;
  if (
    event.trip &&
    event.type === 'completed' &&
    event.trip.status === 'completed' &&
    event.trip.result
  ) {
    travelPostcards = [
      event.trip,
      ...travelPostcards.filter((trip) => trip && trip.id !== event.trip.id),
    ];
    selectedPostcardId = event.trip.id;
    selectedPostcardStop = 0;
    renderedPostcardKey = '';
  }
  if (sessListOpen && !slTravelView.classList.contains('hidden')) renderTravelPage();
  const trip = event.trip;
  if (!travelBelongsToThisPet(trip)) return;
  if (event.type === 'started') {
    clearTransient();
    setState('roam');
    showBubble(t(trip.mode === 'wander' ? 'travel.bubWanderStart' : 'travel.bubStart'), 3600, true);
  } else if (event.type === 'completed') {
    transient('happy', 2600, t('travel.bubDone'), 4200);
    confetti();
    SOUND.bigDone();
  } else if (event.type === 'cancelled') {
    showBubble(t('travel.bubCancel'), 3000, true);
    if (lastStats) applyStats(lastStats);
  } else if (event.type === 'failed') {
    transient('error', 2800, t('travel.bubFailed'), 4200);
    SOUND.error();
  }
});

// ---------- 桌宠气泡（主进程主动推送，比如巡视/夺舍反馈、注意力提醒） ----------
if (window.pet.onBubble) {
  window.pet.onBubble((b) => {
    if (!b || typeof b.text !== 'string' || !b.text) return;
    try { showBubble(b.text, 5200, true); } catch {}
  });
}

// ---------- 事件 ----------
window.pet.onEvent((ev) => {
  // 你正在答面板/打字时：新的待答任务只悄悄进队列(不抢面板)，其余动画/彩带/气泡/状态变化一律不打断
  if (isInteracting()) {
    if ((ev.kind === 'waiting' || ev.kind === 'needsinput') && ev.choice) enqueueChoice(ev.choice);
    return;
  }
  // 旅行中的宠物不被其它普通 session 的动画瞬间拉回工位；真正需要用户
  // 处理的 waiting / needsinput / error 仍会通过快照优先级接管。
  if (
    travelBelongsToThisPet(travelData && travelData.active) &&
    ['operation', 'say', 'user-turn', 'turn-done', 'big-done', 'greet', 'longcmd'].includes(ev.kind)
  ) return;
  // 表情包刚下发时，紧随其后的 user-turn / operation 正是这条 Prompt 自己
  // 产生的。不能让它们在几十毫秒内把配置好的「汗流浃背」应对盖成 thinking /
  // working；错误、授权和需回复等高优先级事件仍继续穿透并接管。
  if (memeLayoutActive && ['user-turn', 'operation', 'say', 'turn-done', 'big-done', 'greet', 'longcmd'].includes(ev.kind)) {
    return;
  }
  // 掠夺是一段不可被普通会话事件插播的完整演出；否则正在运行的 Codex
  // operation 会把“拿来吧你”和掠夺进度气泡瞬间盖掉。
  if (lootPerformanceActive
      && ['user-turn', 'operation', 'say', 'turn-done', 'big-done', 'greet', 'longcmd'].includes(ev.kind)) {
    return;
  }
  switch (ev.kind) {
    case 'operation': {
      // 高优先级稳态（等授权/等回复/出错/清理）不被工具事件降级成 working——
      // 之前 error 期间其它会话干活会导致 working↔error 持续闪烁。
      const hold = state === 'waiting' || state === 'needsinput' || state === 'error' || state === 'sweeping';
      // “收到任务”产生的 thinking 只是等待首个动作的过渡态；真实工具一开始就应
      // 立刻切到 working。庆祝/说话/情绪等其它 transient 仍完整播放。
      const startingWork = transientState === 'thinking' && perfNow() < transientUntil;
      if (!hold && (startingWork || perfNow() >= transientUntil)) {
        if (startingWork) clearTransient();
        setState('working');
        playAction(ev.tool, ev.icon);
      }
      showBubble(`${ev.icon || '🔧'} ${ev.detail}`);
      break;
    }
    case 'say':
      if (ev.text && ev.text.length > 2 && state !== 'waiting') {
        const dur = Math.min(6000, Math.max(2200, ev.text.length * 80));
        // Stop 会同批派生 turn-done(happy) + say(talking)：让庆祝先演完，
        // talking 排在 happy 结束后接棒，气泡文本立刻显示不用等。
        if (transientState === 'happy' && perfNow() < transientUntil) {
          showBubble(`💬 ${ev.text}`, Math.min(4200, dur));
          const token = ++sayToken;
          setTimeout(() => {
            if (token === sayToken && state !== 'waiting') transient(ev.emotion || 'talking', dur);
          }, Math.max(0, transientUntil - perfNow()));
        } else if (ev.emotion) {
          // Claude 的话里带情绪（sorry/puzzled/excited）→ 短暂表情替代 talking
          transient(ev.emotion, 2800, `💬 ${ev.text}`, Math.min(4200, ev.text.length * 80));
        } else {
          transient('talking', dur, `💬 ${ev.text}`, Math.min(4200, dur));
        }
      }
      break;
    case 'user-turn':
      // 你的输入里带情绪（loved/sad/excited）→ 章鱼即时反应；否则像以前一样进 thinking
      if (ev.emotion && state !== 'waiting') {
        const tip = ev.emotion === 'loved' ? t('bub.loved') : ev.emotion === 'sad' ? t('bub.sad') : t('bub.ack');
        transient(ev.emotion, 2800, tip, 2600);
      } else {
        // 多会话时聚合里 working > thinking，直接 setState 会在下个快照被盖掉
        // （只闪 ~150ms）。用 transient 保证「刚提交任务」的思考表情至少停留一会。
        if (state !== 'waiting') transient('thinking', 3500);
        showBubble(t('bub.newTask'), 2600);
      }
      break;
    case 'turn-done':
      transient('happy', 1800, t('bub.roundDone'), 3400);
      SOUND.done();
      break;
    case 'big-done':
      transient('happy', 2200, t('bub.bigDone', { ops: ev.ops || '' }), 3800);
      confetti();
      SOUND.bigDone();
      break;
    case 'error':
      transient('error', 2600, ev.text || t('bub.error'), 3000);
      SOUND.error();
      break;
    case 'waiting':
      clearTransient(); // 残留的 talking/thinking 短暂态不得盖过等授权
      setState('waiting');
      SOUND.waiting();
      if (ev.choice && ((ev.choice.options && ev.choice.options.length) || ev.choice.allowInput)) {
        enqueueChoice(ev.choice); // 直接弹出选项/输入
      } else {
        showBubble(t('bub.waitYou', { project: ev.project || '', wait: waitPhrase(ev.reason) }), 6000);
      }
      break;
    case 'needsinput':
      // Claude 在末尾问「要不要继续」之类，等你回复 → 黄点 + 可在桌宠上继续/回复
      if (state !== 'waiting') { clearTransient(); setState('needsinput'); }
      SOUND.done();
      if (ev.choice && ((ev.choice.options && ev.choice.options.length) || ev.choice.allowInput)) {
        enqueueChoice(ev.choice);
      } else {
        showBubble(t('bub.needReply', { project: ev.project || '' }), 6000);
      }
      break;
    case 'greet':
      transient('greet', 2000, t('bub.greet', { project: ev.project || '' }), 2600);
      SOUND.greet();
      break;
    case 'longcmd':
      if (state !== 'waiting') showBubble(t('bub.slowCmd'), 3000);
      break;
    case 'territory':
      // 领地模式(main 的 territory 编排):发现别的桌宠 → 走过去顶到屏幕边上。
      // 全程复用现成情绪态,窗口走位由主进程完成,这里只负责表情/气泡/音效。
      switch (ev.phase) {
        case 'spotted':
          transient('puzzled', 2400, t('terr.spotted', { rival: ev.rival || t('terr.unknownRival') }), 2600);
          SOUND.waiting();
          break;
        case 'march':
          // 推挤最长十几秒,给个长时限的斗志表情,victory/defeat 到了自然接管
          transient('excited', 16000, t('terr.shove'), 3200);
          break;
        case 'victory':
          transient('happy', 2800, t('terr.won'), 3400);
          confetti();
          SOUND.bigDone();
          break;
        case 'defeat':
          transient('sad', 3000, t('terr.stuck', { rival: ev.rival || t('terr.itPronoun') }), 3200);
          SOUND.error();
          break;
        case 'partial':
          transient('excited', 3200, t('terr.edge', { rival: ev.rival || t('terr.itPronoun') }), 3600);
          SOUND.done();
          break;
        case 'ontop':
          // 猫爪在上定律:发现别的桌宠进程,窗口层级已被主进程抬到最上
          transient('excited', 2600, t('terr.onTop', { rival: ev.rival || t('terr.intruder') }), 3000);
          SOUND.greet();
          break;
        case 'noperm':
          showBubble(t('terr.noPerm'), 7000);
          break;
        case 'granted':
          // 用户在设置里勾上「辅助功能」后主进程轮询到位，自动接着巡视——
          // 给个明确的成功反馈，闭合"点开设置→授权→开跑"这条链。
          transient('happy', 2800, t('terr.granted'), 3400);
          SOUND.greet();
          break;
        case 'searching':
          showBubble(t('terr.scanning'), 2400);
          break;
        case 'clear':
          showBubble(t('terr.clear'), 2600);
          break;
        case 'busy':
          showBubble(t('terr.busy'), 2600);
          break;
        case 'blocked':
          // 面板/菜单还在收口时没有真正执行窗口扫描，不能误报“地盘安静”。
          showBubble(t('terr.deferred'), 2800);
          break;
        case 'abort':
          // 中途撤退(用户来了/弹层打开):静默收掉 march 的长斗志表情,
          // 立刻回落到真实聚合态,不冒气泡打扰正事。
          clearTransient();
          if (lastStats) applyStats(lastStats);
          break;
      }
      break;
    case 'loot':
      switch (ev.phase) {
        case 'searching':
          beginLootPerformance();
          lootTargetClosed = false;
          showBubble(t('loot.searching'), 2600);
          break;
        case 'approach':
          transient('excited', 12000, t('loot.approach'), 3000);
          SOUND.greet();
          break;
        case 'taunt':
          transient('excited', 12000, t('loot.taunt'), 2200);
          break;
        case 'captureStart':
          startLootCaptureVisual(ev.direction);
          startLootCapture(ev.available);
          break;
        case 'sessionCaptured':
          appendLootSession(ev.session);
          break;
        case 'captureWaiting':
          markLootCaptureWaiting();
          break;
        case 'ready':
          revealLootReady(ev.count);
          break;
        case 'kick':
          startLootKick(ev.direction);
          setLootBanner('loot.kicking');
          showBubble(t('loot.kicking'), 2400);
          break;
        case 'push':
          setLootBanner('loot.pushing');
          showBubble(t('loot.pushing'), 3000);
          break;
        case 'targetClosed':
          // 精确关闭动作已经成功：立即结束踢击 GIF，先切到眺望战果。
          // 最终 closed 仍等复扫确认，不在这里提前庆祝成功。
          lootTargetClosed = true;
          startLootLookout(ev.direction, 3600);
          setLootBanner('loot.targetClosed');
          break;
        case 'closed':
          if (!lootTargetClosed) startLootLookout(ev.direction, 3600);
          finishLootCapture(true);
          transient('happy', 2400, t('loot.closed'), 2800);
          confetti();
          SOUND.bigDone();
          endLootPerformance(1600);
          break;
        case 'notFound':
          stopLootActionVisual();
          showBubble(t('loot.notFound'), 3600);
          SOUND.error();
          endLootPerformance(3600);
          break;
        case 'pushFailed':
          stopLootActionVisual();
          finishLootCapture(false);
          transient('sad', 3200, t('loot.pushFailed'), 3600);
          SOUND.error();
          endLootPerformance(3600);
          break;
        case 'closeFailed':
          stopLootActionVisual();
          finishLootCapture(false);
          transient('puzzled', 3600, t('loot.closeFailed'), 4200);
          SOUND.error();
          endLootPerformance(4200);
          break;
        case 'failed':
          stopLootActionVisual();
          finishLootCapture(false);
          transient('sad', 3000, t('loot.failed'), 3400);
          SOUND.error();
          endLootPerformance(3400);
          break;
        case 'busy':
          showBubble(t('loot.busy'), 2800);
          endLootPerformance(2800);
          break;
        case 'blocked':
          showBubble(t('loot.blocked'), 3200);
          endLootPerformance(3200);
          break;
        case 'abort':
          stopLootActionVisual();
          finishLootCapture(false);
          clearTransient();
          endLootPerformance();
          if (lastStats) applyStats(lastStats);
          break;
      }
      break;
  }
});

function perfNow() {
  return Date.now();
}

// ---------- 统计 + 聚合状态 ----------
let lastStats = null; // 最近一次快照：transient 到期时用它立即重算聚合态
let sayToken = 0;     // say 接棒 happy 的排队令牌（新事件作废旧排队）
function compactTokens(value) {
  const n = Number(value) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function applyStats(s) {
  if (!s) return;
  lastStats = s;
  if (s.travel) travelData = s.travel;
  if (AGENT === 'codex') {
    // Codex 没有逐 token 价目，额度条显示套餐窗口用量（5h 主窗口 + 周窗口）
    const rl = s.codexLimits;
    const today = s.codexUsage && s.codexUsage.today;
    chipCost.textContent = today && today.tokens
      ? compactTokens(today.tokens) + ' tok'
      : 'Codex' + (rl && rl.planType ? ' ' + rl.planType : '');
    chipWindow.textContent = rl && rl.usedPercent != null
      ? t('chip.quota', { pct: Math.round(rl.usedPercent) })
        + (rl.secondaryUsedPercent != null ? t('chip.weekly', { pct: Math.round(rl.secondaryUsedPercent) }) : '')
      : t('chip.quotaNone');
    chipWindow.title = t('chip.codexTitle');
  } else {
    chipCost.textContent = '$' + (s.today.cost || 0).toFixed(3);
    chipWindow.textContent = '5h $' + (s.window5h.cost || 0).toFixed(3);
  }
  lastWaiting = (s.waitingCount || 0) + (s.needsinputCount || 0); // 待处理徽标含「等你回复」
  lastBgZombie = (s.bg && s.bg.zombie) || 0;
  if (radialOpen) updateRadialBadge();
  renderSessions(s.sessions || []);
  updateNotepad(s); // 记事本：行动清单 + 待办
  if (sessListOpen) {
    if (!slTravelView.classList.contains('hidden')) renderTravelPage();
    // 掠夺面板由 captureStart/sessionCaptured/ready 显式驱动。普通快照每次
    // 刷新都 innerHTML 重建列表，会让当前 Session 的入场 CSS 动画反复重播。
    else if (!lootCapture) { renderSessList(); fitPopup(sesslist); }
  } // HUD 开着时随快照刷新并重定高

  // 选项面板：按快照重建队列（多任务都在、标明项目；防漏事件/启动时已在等待）
  refreshAsk(s);

  if (DEBUG_STATE) { setState(DEBUG_STATE); return; }

  // 你正在看面板/打字 → 不再改小章鱼状态(别动来动去打断你)，安静等你答完
  if (isInteracting()) return;

  // 聚合梯子，对齐 STATES.md 的优先级表：
  //   waiting > 短暂态 > error(8) > needsinput/notification(7) > sweeping(6)
  //   > juggling(4) > working(3) > thinking(2) > idle(1) > sleeping(0)
  // 之前 working 排在 needsinput 前面，多会话时「等你回复」被干活态彻底盖住。
  if (s.waitingCount > 0) {
    setState('waiting');
  } else if (perfNow() < transientUntil) {
    setState(transientState);
  } else if (s.errorCount > 0) {
    setState('error'); // 有会话卡在 API 错误 → 瘫倒，直到该会话恢复或 oneshot 衰减
  } else if (s.needsinputCount > 0) {
    setState('needsinput');
  } else if (travelBelongsToThisPet(travelData && travelData.active)) {
    setState('roam');
  } else if (s.sweepingCount > 0) {
    setState('sweeping');
  } else if (s.jugglingCount > 0) {
    setState('juggling');
  } else if (s.workingCount > 0) {
    setState('working');
  } else if (s.thinkingCount > 0) {
    setState('thinking');
  } else if (s.loafingCount > 0) {
    setState('loafing'); // 工具间隙：上一步干完等下一步 → 摸鱼
  } else if (s.idleMs == null || s.idleMs > IDLE_SLEEP_MS) {
    // idleMs=null 表示已无任何活跃会话——什么都没发生就该睡觉；
    // 之前 null 落到 idle，桌宠永不入睡，睡着后会话被回收还会凭空惊醒。
    setState('sleeping');
  } else {
    setState('idle');
  }
}
window.pet.onStats(applyStats);

function renderSessions(sessions) {
  sessionsEl.innerHTML = '';
  // 与会话列表 HUD 完全联动：同一过滤(非 headless/非睡眠)、同一配色、同一排序。
  const list = (sessions || []).filter(isVisibleSession).sort((a, b) => {
    const pinA = pinnedSessionIds.includes(sessionKey(a)) ? 0 : 1;
    const pinB = pinnedSessionIds.includes(sessionKey(b)) ? 0 : 1;
    if (pinA !== pinB) return pinA - pinB;
    const pa = SESS_SORT[a.state] != null ? SESS_SORT[a.state] : 3;
    const pb = SESS_SORT[b.state] != null ? SESS_SORT[b.state] : 3;
    return pa !== pb ? pa - pb : (a.idleMs || 0) - (b.idleMs || 0);
  });
  for (const s of list) {
    const d = document.createElement('div');
    d.className = 'sess-dot ' + sessionDotClass(s);
    const label = s.state === 'waiting' ? waitPhrase(s.reason) : (sessMeta(s.state) || s.state);
    d.title = `${s.project} · ${label}`;
    sessionsEl.appendChild(d);
  }
  // 菜单开着时同步「待处理」角标
  if (radialOpen) updateRadialBadge();
}

window.pet.onConfig((cfg) => {
  if (!cfg) return;
  muted = !!cfg.muted;
  territorySupported = !!cfg.territorySupported;
  lootSupported = !!cfg.lootSupported;
  if (cfg.lang) applyLang(cfg.lang);
  if (cfg.skin) applySkin(cfg.skin);
  pinnedSessionIds = Array.isArray(cfg.pinnedSessions) ? cfg.pinnedSessions.slice() : [];
  archivedSessionIds = Array.isArray(cfg.archivedSessions) ? cfg.archivedSessions.slice() : [];
  lootKeptSessions = Array.isArray(cfg.lootCapturedSessions) ? cfg.lootCapturedSessions.slice() : [];
  scheduleLootKeptExpiry();
  if (sessListOpen && !memeTarget && !lootCapture) renderSessList();
});

// Static markup carries its Chinese text inline (so the window is never blank
// before the first config push); data-i18n rewrites it once the language is
// known and again on every switch.
function applyStaticI18n() {
  document.documentElement.lang = window.OctoI18n.getLang();
  for (const el of document.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll('[data-i18n-title]')) el.title = t(el.dataset.i18nTitle);
  for (const el of document.querySelectorAll('[data-i18n-aria]')) el.setAttribute('aria-label', t(el.dataset.i18nAria));
  for (const el of document.querySelectorAll('[data-i18n-ph]')) {
    el.placeholder = t(el.dataset.i18nPh);
    delete el.dataset.ph; // drop the cached original so the warn/restore pair re-seeds
  }
}

function applyLang(_next) {
  // 纯中文界面：语言恒为 zh，不再切换。
  if (window.OctoI18n.getLang() === 'zh') return;
  window.OctoI18n.setLang('zh');
  applyStaticI18n();
  lastAskSig = '';
  // Live views rebuild from the state we already hold; everything else refreshes
  // on the stats push the main process fires right after the switch.
  if (sessListOpen) { if (memeTarget) openMemePage(memeTarget); else renderSessList(); }
  if (todoPopOpen) renderTodoPop();
  if (radialOpen) buildRadial();
  if (lastStats) applyStats(lastStats);
}

function applySkin(s) {
  // 只保留 cat（月薪喵）皮肤；历史配置里的 mascot/pixel 一律回落 cat。
  skin = 'cat';
  document.body.classList.toggle('skin-pixel', false);
  document.body.classList.toggle('skin-mascot', false);
  document.body.classList.toggle('skin-cat', true);
  updateCat(state);
  requestAnimationFrame(reportPetVisualBounds);
}

function reportPetVisualBounds() {
  const el = curSkinEl();
  if (!el) return;
  const r = el.getBoundingClientRect();
  try { window.pet.petVisualBounds({ x: r.left, y: r.top, width: r.width, height: r.height }); } catch {}
}

// ====================================================================
// 拖动 + 点击（短按=泡泡菜单 / 拖动=移动窗口）
// ====================================================================
let g = null; // 当前手势（同步建立，保证快速点击也能识别）
function attachDrag(el) {
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    try { el.setPointerCapture(e.pointerId); } catch {}
    el.classList.add('dragging');
    g = { el, pid: e.pointerId, sx: e.screenX, sy: e.screenY, moved: false, win: null };
    try { window.pet.petDragging(true); } catch {} // 拖动期间主进程保持窗口可点
    window.pet.getWinPos().then(([wx, wy]) => { if (g) g.win = [wx, wy]; });
  });
  el.addEventListener('pointermove', (e) => {
    if (!g) return;
    const dx = e.screenX - g.sx;
    const dy = e.screenY - g.sy;
    if (!g.moved && Math.abs(dx) + Math.abs(dy) > 4) g.moved = true;
    if (g.moved && g.win) {
      if (radialOpen) closeRadial();
      movePetDuringDrag(g, e, g.win[0] + dx, g.win[1] + dy);
    }
  });
  el.addEventListener('pointerup', () => {
    if (!g) return;
    const wasMove = g.moved;
    try { el.releasePointerCapture(g.pid); } catch {}
    el.classList.remove('dragging');
    g = null;
    try { window.pet.petDragging(false); } catch {}
    if (wasMove) {
      // Let the final setBounds land, then exchange the internal top/bottom or
      // left/right anchor without moving the visible pet.
      setTimeout(settleEdgeLayout, 0);
    } else {
      // 左键短按 = 会话列表 HUD（状态/会话名/上下文用量一览，点行聚焦该会话）。
      // 权限的允许/拒绝仍由 waiting 事件自动弹气泡，不走这里。
      if (radialOpen) closeRadial();
      else toggleSessList();
    }
  });
  el.addEventListener('pointercancel', () => {
    if (g) el.classList.remove('dragging');
    g = null;
    try { window.pet.petDragging(false); } catch {}
    setTimeout(settleEdgeLayout, 0);
  });
  // 右键 = 泡泡菜单（右键时绝不移动：取消任何进行中的左键手势，锁死窗口位置）
  el.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (g) {
      try { el.releasePointerCapture(g.pid); } catch {}
      el.classList.remove('dragging');
      g = null;
      try { window.pet.petDragging(false); } catch {}
    }
    toggleRadial();
  });
}
stateEls.forEach(attachDrag);

// 卡片按钮：Submit/Next、Back、Go to Terminal、Other 输入
askSubmit.addEventListener('click', () => { const c = askQueue[askIdx]; if (c && c.kind === 'ask') elicNextOrSubmit(c); });
askBack.addEventListener('click', () => { const c = askQueue[askIdx]; if (c && c.kind === 'ask') elicBack(c); });
askTerm.addEventListener('click', () => { const c = askQueue[askIdx]; if (c) gotoSession(c); });
askText.addEventListener('input', () => updateSubmitEnabled());
// 自定义输入里按回车直接发送（仅 elicitation）；空内容不发、提示别忘了填
askText.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const c = askQueue[askIdx];
  if (!c || !elic) return;
  if (!(askText.value || '').trim()) { warnEmptyInput(); return; }
  if (askSubmit.classList.contains('disabled')) { warnEmptyInput(); return; }
  elicNextOrSubmit(c);
});
// 鼠标在面板上 = 交互中（配合 isInteracting 冻结轮询）
askEl.addEventListener('pointerenter', () => { askHover = true; });
askEl.addEventListener('pointerleave', () => { askHover = false; });

// 记事本：点击开/关 行动清单弹层
notepad.addEventListener('click', (e) => { e.stopPropagation(); todoPopOpen ? closeTodoPop() : openTodoPop(); });
notepad.addEventListener('contextmenu', (e) => e.stopPropagation());
document.getElementById('tp-close').addEventListener('click', (e) => { e.stopPropagation(); closeTodoPop(); });

// 会话列表 HUD：关闭 + 底部操作（新开按钮按本窗口的 agent 分流）
document.getElementById('sl-close').addEventListener('click', (e) => { e.stopPropagation(); closeSessList(); });
slTravelMission.addEventListener('input', () => { travelMissionDirty = true; });
slTravelMission.addEventListener('focus', () => window.pet.focusPet());
slTravelMission.addEventListener('blur', () => window.pet.blurPet());
slTravelStart.addEventListener('click', async (e) => {
  e.stopPropagation();
  if (!travelTarget) {
    setTravelStatus(t('travel.invalid'), 'error');
    return;
  }
  const mission = String(slTravelMission.value || '').trim();
  if (!mission) {
    setTravelStatus(t('travel.invalid'), 'error');
    slTravelMission.focus();
    return;
  }
  slTravelStart.disabled = true;
  setTravelStatus(t('travel.departing'));
  let result;
  try {
    result = await window.pet.startTravel(travelTarget.sessionId || '', travelTemplateId || '', mission);
  } catch {
    result = { ok: false, code: 'not-ready' };
  }
  slTravelStart.disabled = false;
  if (result && result.state) travelData = result.state;
  if (!result || !result.ok) setTravelStatus(travelErrorText(result && result.code), 'error');
  else setTravelStatus('');
  renderTravelPage();
});
slTravelCancel.addEventListener('click', async (e) => {
  e.stopPropagation();
  slTravelCancel.disabled = true;
  let result;
  try { result = await window.pet.cancelTravel(); } catch { result = { ok: false, code: 'not-ready' }; }
  slTravelCancel.disabled = false;
  if (result && result.state) travelData = result.state;
  if (!result || !result.ok) setTravelStatus(travelErrorText(result && result.code), 'error');
  renderTravelPage();
});
slTravelStopPrev.addEventListener('click', (e) => {
  e.stopPropagation();
  goTravelStop(selectedPostcardStop - 1);
});
slTravelStopNext.addEventListener('click', (e) => {
  e.stopPropagation();
  goTravelStop(selectedPostcardStop + 1);
});
slBack.addEventListener('click', (e) => { e.stopPropagation(); showSessionPage(); });
slSearch.addEventListener('input', () => {
  sessionSearch = slSearch.value || '';
  renderSessList();
  fitPopup(sesslist);
});
slSearch.addEventListener('focus', () => window.pet.focusPet());
slSearch.addEventListener('blur', () => window.pet.blurPet());
slFilters.addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  e.stopPropagation();
  if (btn === slArchivedToggle) {
    showArchived = !showArchived;
    btn.classList.toggle('active', showArchived);
  } else if (btn.dataset.filter) {
    sessionFilter = btn.dataset.filter;
    slFilters.querySelectorAll('[data-filter]').forEach((el) => el.classList.toggle('active', el === btn));
  }
  renderSessList();
  fitPopup(sesslist);
});
const slNewBtn = document.getElementById('sl-new');
if (slTravelInbox) slTravelInbox.addEventListener('click', async (e) => {
  e.stopPropagation();
  await openTravelInbox();
});
if (slWander) slWander.addEventListener('click', async (e) => {
  e.stopPropagation();
  slWander.disabled = true;
  slWander.textContent = t('travel.wanderDeparting');
  let result;
  try { result = await window.pet.wanderTravel(); } catch { result = { ok: false, code: 'not-ready' }; }
  if (result && result.state) travelData = result.state;
  slWander.textContent = t('travel.wanderEntry');
  if (result && result.ok) {
    closeSessList();
    return;
  }
  slWander.disabled = !!(travelData && travelData.active);
  slSub.textContent = travelErrorText(result && result.code);
  fitPopup(sesslist);
});
slNewBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  window.pet.newChat();
  closeSessList();
});
document.getElementById('sl-panel').addEventListener('click', (e) => { e.stopPropagation(); window.pet.openPanel(); closeSessList(); });
sesslist.addEventListener('contextmenu', (e) => e.stopPropagation());
todopop.querySelectorAll('.tp-ops button').forEach((b) => {
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    const op = b.dataset.op;
    if (op === 'panel') window.pet.openPanel();
    else if (op === 'newchat' || op === 'claude') window.pet.newChat();
    else if (op === 'log') window.pet.openLog();
    closeTodoPop();
  });
});

// ---------- 泡泡菜单 ----------
let territorySupported = false; // 由 pet:config 下发(仅 macOS true)
let lootSupported = false;
let radialOpenSeq = 0;
let lastRadialMetrics = null;
// labelKey (not label): buildRadial resolves it at render time, so the menu
// follows a language switch without rebuilding this table.
// 精简后的菜单：面板 / 待处理 / 日志 / 静音 / 退出。巡视/夺舍/后台移出——
// 巡视与其它桌宠设置都在 OpenSquilla 设置页的「Pet」区块里。
const MENU = [
  { ic: 'chart',  labelKey: 'menu.panel', act: () => window.pet.openPanel() },
  { ic: 'hand',   labelKey: 'menu.pending', badge: true, act: () => window.pet.openPanel() },
  { ic: 'doc',    labelKey: 'menu.log', act: () => window.pet.openLog() },
  { ic: 'bell',   labelKey: 'menu.mute', act: () => window.pet.toggleMute() },
  // 双宠模式下「退出」只收起自己这只（独立事件，另一只照常干活）；
  // 整个 app 的退出走托盘。单宠模式保持原语义：退出 app。
  AGENT === 'all'
    ? { ic: 'power', labelKey: 'menu.quit', act: () => window.pet.quit() }
    : { ic: 'power', labelKey: 'menu.collapse', act: () => window.pet.closePet() },
];

function toggleSkin() {
  // 只保留 cat 皮肤：切换无效果。
  applySkin('cat');
  window.pet.setSkin('cat');
}

function usableRadialMetrics(metrics) {
  if (!metrics || !metrics.window || !metrics.workArea) return null;
  const wr = metrics.window;
  const wa = metrics.workArea;
  if (![wr.x, wr.y, wr.width, wr.height, wa.x, wa.y, wa.width, wa.height].every(Number.isFinite)) return null;
  if (wr.width <= 0 || wr.height <= 0 || wa.width <= 0 || wa.height <= 0) return null;
  return metrics;
}

function radialFrame() {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

async function settledRadialMetrics() {
  if (!window.pet || typeof window.pet.getWindowMetrics !== 'function') return null;
  let metrics = null;
  try { metrics = usableRadialMetrics(await window.pet.getWindowMetrics()); } catch { return null; }
  // setPetSize/resetPetSize 在主进程同步落 bounds，但 renderer 的 resize 与
  // flex 重排会晚一拍。等到 DOM viewport 也追上主进程尺寸后再取 pet rect。
  for (let i = 0; metrics && i < 6; i++) {
    const wr = metrics.window;
    const settled = Math.abs((window.innerWidth || 0) - wr.width) <= 1
      && Math.abs((window.innerHeight || 0) - wr.height) <= 1;
    if (settled) break;
    await radialFrame();
    try { metrics = usableRadialMetrics(await window.pet.getWindowMetrics()) || metrics; } catch {}
  }
  await radialFrame();
  return metrics;
}

function buildRadial(metrics = lastRadialMetrics) {
  radial.innerHTML = '';
  const el = curSkinEl();
  const sr = stage.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const cx = r.left - sr.left + r.width / 2;
  const cy = r.top - sr.top + r.height / 2;
  const items = MENU.filter((it) => !it.when || it.when()); // 平台不支持的项(如非 mac 的巡视)不渲染
  const n = items.length;
  const exact = usableRadialMetrics(metrics);
  if (exact) lastRadialMetrics = exact;
  const frame = exact && exact.window;
  const viewportW = Math.max(1, frame ? frame.width : (window.innerWidth || 320));
  const viewportH = Math.max(1, frame ? frame.height : (window.innerHeight || 340));
  const wa = exact ? exact.workArea : browserWorkArea();
  const winX = frame ? frame.x : (Number.isFinite(window.screenX) ? window.screenX : wa.x);
  const winY = frame ? frame.y : (Number.isFinite(window.screenY) ? window.screenY : wa.y);
  const pad = 5;
  // Intersect the BrowserWindow viewport with the actually visible work area.
  // This protects old saved positions that may still have part of the
  // transparent window off-screen before the first drag normalises them.
  const safeRect = {
    x: Math.max(pad, wa.x - winX + pad),
    y: Math.max(pad, wa.y - winY + pad),
    width: Math.max(46, Math.min(viewportW - pad, wa.x + wa.width - winX - pad) - Math.max(pad, wa.x - winX + pad)),
    height: Math.max(46, Math.min(viewportH - pad, wa.y + wa.height - winY - pad) - Math.max(pad, wa.y - winY + pad)),
  };
  const preferred = [];
  // A side-edge pet must fan into the desktop first. Trying the vertical fan
  // before the inward fan is what created the clipped half-heart in corners.
  if (edgeLayout.horizontal === 'left') preferred.push('right');
  else if (edgeLayout.horizontal === 'right') preferred.push('left');
  if (edgeLayout.vertical === 'below') preferred.push('below');
  else preferred.push('above');
  preferred.push(edgeLayout.vertical === 'below' ? 'above' : 'below');
  const layout = window.PetGeometry
    ? window.PetGeometry.radialLayout({ count: n, center: { x: cx, y: cy }, safeRect, preferred })
    : { direction: 'above', points: [] };
  rlog(
    'radial',
    `layout=${layout.direction} frame=${winX},${winY} ${viewportW}x${viewportH} ` +
      `safe=${safeRect.x},${safeRect.y} ${safeRect.width}x${safeRect.height}`,
  );
  items.forEach((it, i) => {
    const point = layout.points[i] || { x: cx, y: cy };
    const x = point.x;
    const y = point.y;
    const b = document.createElement('div');
    b.className = 'radial-item';
    b.style.left = x + 'px';
    b.style.top = y + 'px';
    b.style.transitionDelay = i * 0.03 + 's';
    // Key, not label: the old `it.label === '静音'` test silently picked the
    // wrong bell icon under any non-Chinese UI.
    const icName = it.labelKey === 'menu.mute' ? (muted ? 'bell-off' : 'bell') : it.ic;
    const icHtml = (window.OctoIcons && window.OctoIcons.icon(icName)) || '';
    b.innerHTML = `<span class="ri-ic oi">${icHtml}</span><span class="ri-lb">${esc(t(it.labelKey))}</span>`;
    const cnt = it.badge ? lastWaiting : it.badgeBg ? lastBgZombie : 0;
    if ((it.badge || it.badgeBg) && cnt > 0) {
      const bd = document.createElement('span');
      bd.className = 'ri-badge';
      bd.textContent = cnt;
      b.appendChild(bd);
    }
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      closeRadial();
      it.act();
    });
    radial.appendChild(b);
  });
}

function updateRadialBadge() {
  const items = radial.querySelectorAll('.radial-item');
  MENU.forEach((m, idx) => {
    if (!m.badge && !m.badgeBg) return;
    const node = items[idx];
    if (!node) return;
    const cnt = m.badge ? lastWaiting : lastBgZombie;
    let bd = node.querySelector('.ri-badge');
    if (cnt > 0) {
      if (!bd) { bd = document.createElement('span'); bd.className = 'ri-badge'; node.appendChild(bd); }
      bd.textContent = cnt;
    } else if (bd) bd.remove();
  });
}

async function openRadial() {
  const seq = ++radialOpenSeq;
  if (todoPopOpen) closeTodoPop();
  if (sessListOpen) closeSessList();
  radialOpen = true;
  try { window.pet.uiBusy(true); } catch {}
  bubble.classList.add('hidden');
  // closeSessList/closeTodoPop 会异步把 BrowserWindow 从弹层尺寸缩回基础
  // 尺寸。必须等窗口和 DOM 都归位后再布局，否则菜单会按旧大窗坐标生成，
  // 随后的缩窗会把按钮直接裁出可见区域。
  let metrics = await settledRadialMetrics();
  if (seq !== radialOpenSeq || !radialOpen) return;
  settleEdgeLayout();
  metrics = await settledRadialMetrics() || metrics;
  if (seq !== radialOpenSeq || !radialOpen) return;
  buildRadial(metrics);
  radial.classList.remove('hidden');
}
function closeRadial() {
  radialOpenSeq++;
  radial.classList.add('hidden');
  radialOpen = false;
  try { window.pet.uiBusy(!!(todoPopOpen || sessListOpen || askActive || isInteracting())); } catch {}
}
function toggleRadial() {
  if (radialOpen) closeRadial();
  else openRadial().catch(() => closeRadial());
}
// 点遮罩空白处关闭
radial.addEventListener('click', () => closeRadial());
window.addEventListener('blur', () => { if (radialOpen) closeRadial(); });

// ---------- 初始化 ----------
(async () => {
  // 单宠模式：名牌只在双宠时亮，这里保持隐藏
  const agentTag = document.getElementById('agent-tag');
  if (AGENT !== 'all' && agentTag) {
    agentTag.innerHTML = `<span class="at-ic">${AGENT_ICON}</span><span>${AGENT_LABEL}</span>`;
    agentTag.classList.add(AGENT);
    agentTag.classList.remove('hidden');
  }
  // Single-agent build: no per-agent rebinding of the notepad launcher button.
  const cfg = await window.pet.getConfig();
  if (cfg) {
    muted = !!cfg.muted;
    territorySupported = !!cfg.territorySupported;
    lootSupported = !!cfg.lootSupported;
    window.OctoI18n.setLang(cfg.lang || 'zh');
    applySkin(cfg.skin || 'mascot');
  }
  // Convert positions saved by older builds (which anchored the transparent
  // window rather than the visible pet) as soon as the real skin is known.
  requestAnimationFrame(settleEdgeLayout);
  applyStaticI18n();
  await loadMemeCatalog();
  const s = await window.pet.getStats();
  // 有快照就按真实聚合态亮相；之前无条件 setState('idle') 会把刚算出的
  // working/waiting 盖掉，启动瞬间总是先闪一下空闲。getStats 落空但推送
  // 已先到时（lastStats 已有值）同样不能清。
  if (s) applyStats(s);
  else if (!lastStats) setState('idle');
  showBubble(t('bub.online'), 3000);
  if (DEBUG_CONFETTI) setInterval(() => confetti(), 2500);
})();

// ---------- 透明区域点击穿透（命中测试）----------
// 桌宠窗口是透明矩形，空白处不该拦住后面的应用。光标在内容(小章鱼/卡片/菜单/记事本)
// 上 → 接收点击；在透明区 → 让窗口穿透。forward:true 使穿透时 mousemove 仍回传，
// 因此一旦光标回到内容上即可恢复可点。拖动中(g)始终保持可点。
const HIT_SEL = '#pixel,#mascot,#cat,#radial,#notepad,#todopop,#ask,#sesslist';
let mouseIgnoring = false;
function setMouseIgnore(on) {
  if (on === mouseIgnoring) return;
  mouseIgnoring = on;
  try { window.pet.setIgnoreMouse(on); } catch {}
}
window.addEventListener('mousemove', (e) => {
  if (g) { setMouseIgnore(false); return; } // 拖动中保持可点
  const el = document.elementFromPoint(e.clientX, e.clientY);
  // 命中测试权威同步悬停态：穿透切换时 pointerleave 可能漏发，会把 askHover 卡在 true，
  // 进而让 isInteracting() 永远为真、refreshAsk 永不对账（旧卡片冻结、新卡片进不来）。
  askHover = !!(el && el.closest('#ask'));
  setMouseIgnore(!(el && el.closest(HIT_SEL)));
}, true);
// 启动即默认穿透（透明区不挡），光标移到内容上时由上面的命中测试恢复
setMouseIgnore(true);

// ---------- 交互状态上报(领地模式避战用) ----------
// 主进程无法区分「气泡 fitPopup 撑大的窗口」和「用户真的开着面板」,由渲染端
// 每 700ms 对账一次,变化才上报。覆盖:选项面板交互/右键菜单/记事本/会话列表。
let lastUiBusy = null;
setInterval(() => {
  const busy = !!(radialOpen || todoPopOpen || sessListOpen || askActive || isInteracting());
  if (busy === lastUiBusy) return;
  lastUiBusy = busy;
  try { window.pet.uiBusy(busy); } catch {}
}, 700);
// 气泡、皮肤切换和窗口自适应都可能改变本体在透明窗里的局部位置。
// 窗口尺寸变化(fitPopup/resetPetSize)在渲染端表现为 resize 事件,按事件上报;
// 常驻轮询只留一个低频兜底,不必每 500ms 强制一次 getBoundingClientRect 回流。
window.addEventListener('resize', () => requestAnimationFrame(() => {
  reportPetVisualBounds();
  alignMemePlayer();
}));
setInterval(reportPetVisualBounds, 3000);
