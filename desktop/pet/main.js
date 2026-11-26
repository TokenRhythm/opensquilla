// 蛋仔 — OpenSquilla 桌面 Q 版桌宠（Electron 主进程）。
// 零运行时依赖：复用 desktop/electron 的 Electron 二进制；WS/HTTP/fetch 全用内置。
"use strict";
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, shell, nativeImage, dialog } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const crypto = require("node:crypto");

const { Config } = require("./lib/config");
const { GatewayClient } = require("./lib/gateway");
const { createCompanionServer, DispatchQueue } = require("./lib/companion");
const tts = require("./lib/tts");
const spool = require("./lib/spool");

const IS_WIN = process.platform === "win32";
const KIND_PERMISSION = "permission";
const KIND_QUESTION = "question";
const KIND_STOP = "stop";
const KIND_TURN_START = "turn-start";

let win = null;
let tray = null;
let config = null;
let gateway = null;
let companion = null;
const dispatch = new DispatchQueue();

let sessionsCache = [];
let lastActiveSession = "";
const activeBubbles = new Map(); // id → descriptor
const dedupe = new Map(); // key → ts
let paused = false;
let actualCompanionPort = 0;
let speaking = false;
const speakQueue = [];
let audioEndResolver = null;

// ── 工具 ──────────────────────────────────────────────────────────
function log(...args) {
  console.log("[pet]", ...args);
}

function cfg() {
  return config.get();
}

function isMuted() {
  return !!cfg().pet.muted || paused;
}

function sessionLabel(key) {
  const s = sessionsCache.find((x) => x && x.key === key);
  if (s) {
    const t = s.title || s.display_name || s.displayName || s.workspaceLabel || "";
    if (t && String(t).toLowerCase() !== "webchat") return String(t);
    if (s.workspace) return path.basename(String(s.workspace));
  }
  if (key) {
    const parts = String(key).split(":");
    return parts[parts.length - 1].slice(0, 12);
  }
  return "这个会话";
}

function identityOf(payload) {
  for (const k of ["approval_id", "approvalId", "request_id", "requestId", "turn_id", "turnId", "task_id", "taskId", "event_id", "eventId"]) {
    const v = payload[k];
    if (v != null && String(v).trim()) return String(v).trim();
  }
  const seq = payload.stream_seq != null ? payload.stream_seq : payload.streamSeq;
  return seq != null ? "seq:" + seq : "";
}

function dedupeAllow(sessionKey, kind, identity) {
  const key = identity ? `${sessionKey}|${kind}|${identity}` : `${sessionKey}|${kind}`;
  const now = Date.now();
  const last = dedupe.get(key);
  if (last != null && now - last < 15000) return false;
  dedupe.set(key, now);
  if (dedupe.size > 300) {
    for (const [k, ts] of dedupe) if (now - ts > 15000) dedupe.delete(k);
  }
  return true;
}

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function setMood(mood, extra = {}) {
  send("pet:state", { mood, connected: gateway ? gateway.connected : false, muted: isMuted(), ...extra });
}

// ── 语音 ──────────────────────────────────────────────────────────
function enqueueSpeak(text) {
  const clean = String(text || "").trim();
  if (!clean || isMuted() || cfg().tts.enabled === false) return;
  speakQueue.push(clean.slice(0, 200));
  processSpeakQueue();
}

async function processSpeakQueue() {
  if (speaking) return;
  const text = speakQueue.shift();
  if (!text) return;
  speaking = true;
  setMood("talk", { talking: true });
  try {
    const result = await tts.speak(text, cfg().tts, gateway ? gateway.httpBase : "");
    if (result && result.audio) {
      send("tts:play", { ...result.audio, volume: cfg().tts.volume });
      await waitForAudioEnd(30000);
    }
  } catch (err) {
    log("tts failed:", err.message || err);
    send("pet:toast", { text: "语音失败：" + (err.message || err) });
  } finally {
    speaking = false;
    setMood("idle", { talking: false });
    if (speakQueue.length) processSpeakQueue();
  }
}

function waitForAudioEnd(timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; audioEndResolver = null; resolve(); } };
    audioEndResolver = finish;
    setTimeout(finish, timeoutMs);
  });
}

// ── 气泡 ──────────────────────────────────────────────────────────
function showBubble(descriptor) {
  const id = descriptor.id || crypto.randomUUID();
  const bubble = { id, ts: Date.now(), ...descriptor };
  activeBubbles.set(id, bubble);
  send("bubble:show", bubble);
  return id;
}

function dismissBubble(id) {
  if (activeBubbles.delete(id)) send("bubble:dismiss", { id });
}

function dismissSessionBubbles(sessionKey, kinds) {
  for (const [id, b] of activeBubbles) {
    if (b.sessionKey === sessionKey && (!kinds || kinds.includes(b.kind))) dismissBubble(id);
  }
}

function raiseWindow() {
  if (!win || win.isDestroyed()) return;
  if (!win.isVisible()) win.show();
  win.setAlwaysOnTop(true, "screen-saver");
  win.moveTop();
}

// ── 网关事件 → 气泡 + 语音 ─────────────────────────────────────────
function pendingClarify(payload) {
  let result = payload.result;
  if (typeof result === "string") {
    try { result = JSON.parse(result); } catch { return null; }
  }
  if (!result || typeof result !== "object") result = payload.user_input_request;
  if (!result || result.kind !== "user_input" || result.paused !== true) return null;
  if (result.status != null && result.status !== "input_required") return null;
  const requestId = result.request_id || result.requestId;
  if (!requestId) return null;
  let schema = result.clarify_schema;
  if (typeof schema === "string") { try { schema = JSON.parse(schema); } catch { schema = null; } }
  if (!schema || !Array.isArray(schema.fields) || !schema.fields.length) return null;
  return { requestId: String(requestId), schema };
}

function onGatewayEvent({ name, payload }) {
  const sessionKey = payload.session_key || payload.sessionKey || "";
  if (sessionKey) lastActiveSession = sessionKey;

  if (name === "sessions.changed") {
    return; // gateway client 自行刷新订阅
  }

  if (name === "session.event.approval_required") {
    const id = identityOf(payload);
    if (!dedupeAllow(sessionKey, KIND_PERMISSION, id)) return;
    const command = payload.command || payload.display_target || payload.tool_name || "";
    showBubble({
      kind: "permission",
      title: "需要确认",
      message: command || "有个操作在等你确认",
      warning: payload.warning || "",
      toolName: payload.tool_name || "",
      destructive: !!payload.destructive,
      sessionKey,
      sessionLabel: sessionLabel(sessionKey),
      approval: { id: payload.approval_id || id, namespace: payload.namespace || "exec" },
    });
    setMood("alert");
    raiseWindow();
    if (cfg().pet.autoSpeak) enqueueSpeak("有个操作在等你确认");
    return;
  }

  if (name === "session.event.plan_presentation") {
    const id = identityOf(payload);
    if (!dedupeAllow(sessionKey, KIND_QUESTION, id)) return;
    const options = Array.isArray(payload.options) ? payload.options : (Array.isArray(payload.choices) ? payload.choices : []);
    showBubble({
      kind: "plan",
      title: "方案选择",
      message: payload.message || payload.text || payload.summary || "有个方案等你选择",
      options,
      sessionKey,
      sessionLabel: sessionLabel(sessionKey),
    });
    setMood("alert");
    raiseWindow();
    if (cfg().pet.autoSpeak) enqueueSpeak("有个方案等你选择");
    return;
  }

  if (name === "session.event.tool_result") {
    const clarify = pendingClarify(payload);
    if (!clarify) return;
    if (!dedupeAllow(sessionKey, KIND_QUESTION, clarify.requestId)) return;
    showBubble({
      kind: "clarify",
      title: clarify.schema.title || "需要回答",
      message: clarify.schema.prompt || clarify.schema.description || "有个问题等你回答",
      fields: clarify.schema.fields,
      requestId: clarify.requestId,
      sessionKey,
      sessionLabel: sessionLabel(sessionKey),
    });
    setMood("alert");
    raiseWindow();
    if (cfg().pet.autoSpeak) enqueueSpeak("有个问题等你回答");
    return;
  }

  if (name === "session.event.done" || name === "chat.done" || name === "task.succeeded") {
    const id = identityOf(payload);
    if (!dedupeAllow(sessionKey, KIND_STOP, id)) return;
    dismissSessionBubbles(sessionKey, ["permission", "plan", "clarify"]);
    showBubble({
      kind: "stop",
      title: "完成",
      message: (payload.reason === "aborted" ? "任务已中止" : "任务完成") + " · " + sessionLabel(sessionKey),
      sessionKey,
      sessionLabel: sessionLabel(sessionKey),
      autoDismiss: 6000,
    });
    setMood("happy");
    if (cfg().pet.autoSpeak) enqueueSpeak(payload.reason === "aborted" ? "任务已中止" : "任务完成");
    setTimeout(() => setMood("idle"), 2500);
    return;
  }

  if (name === "session.event.turn_committed") {
    setMood("think");
    if (cfg().pet.showTurnStart) {
      showBubble({ kind: "message", title: "开始", message: "新一轮对话开始 · " + sessionLabel(sessionKey), sessionKey, autoDismiss: 3000 });
    }
    return;
  }
}

// ── companion /notify /speak ──────────────────────────────────────
function onNotify(payload) {
  const kind = payload.event || "message";
  const sessionKey = payload.session_id || "";
  const eventId = payload.event_id || "";
  const connected = gateway && gateway.connected;

  // WS 在线时由 WS 事件驱动富气泡与语音；/notify 仅补位（离线时）。
  if (connected) {
    if (kind === KIND_PERMISSION || kind === KIND_QUESTION) raiseWindow();
    return;
  }
  if (!dedupeAllow(sessionKey, kind, eventId)) return;
  const map = {
    [KIND_PERMISSION]: "permission",
    [KIND_QUESTION]: "plan",
    [KIND_STOP]: "stop",
    [KIND_TURN_START]: "message",
    message: "message",
  };
  const bubbleKind = map[kind] || "message";
  if (bubbleKind === "message" && kind === KIND_TURN_START && !cfg().pet.showTurnStart) return;
  showBubble({
    kind: bubbleKind,
    title: payload.title || "OpenSquilla",
    message: payload.message || "",
    sessionKey,
    sessionLabel: sessionLabel(sessionKey),
    autoDismiss: bubbleKind === "stop" || bubbleKind === "message" ? 7000 : 0,
    fallback: true,
  });
  if (kind === KIND_PERMISSION || kind === KIND_QUESTION) {
    setMood("alert");
    raiseWindow();
  } else if (bubbleKind === "stop") {
    setMood("happy");
    setTimeout(() => setMood("idle"), 2500);
  }
  enqueueSpeak(payload.message || "");
}

function onSpeak({ message }) {
  enqueueSpeak(message);
}

function getState() {
  return {
    pet: "opensquilla-eggy",
    version: app.getVersion(),
    pet_visible: win ? win.isVisible() : false,
    pet_enabled: true,
    muted: isMuted(),
    paused,
    tts_ready: cfg().tts.enabled !== false,
    tts_provider: cfg().tts.provider,
    gateway_connected: gateway ? gateway.connected : false,
    gateway_url: cfg().gateway.url,
    companion_port: actualCompanionPort || cfg().companion.port,
    dispatch_pending: dispatch.pending("").length,
    plugin_version: "eggy-0.1.0",
  };
}

// ── spool / pause ─────────────────────────────────────────────────
function importSpool() {
  const dir = spool.resolveSpoolDir(cfg().spool.dir);
  const items = spool.readSpool(dir);
  if (!items.length) return;
  spool.clearSpool(dir);
  // 离线积压：静默回放为消息气泡（不出声），最多展示最近 5 条。
  for (const item of items.slice(-5)) {
    showBubble({
      kind: "message",
      title: item.title || "离线提醒",
      message: item.message || "",
      sessionKey: item.session_id || "",
      autoDismiss: 8000,
    });
  }
  log("imported spool:", items.length);
}

function refreshPause() {
  const file = spool.resolvePauseFile(cfg().spool.pauseFile);
  const next = spool.isPaused(file);
  if (next !== paused) {
    paused = next;
    setMood("idle");
  }
}

// ── 窗口 / 托盘 ───────────────────────────────────────────────────
const PET_WIDTH = 340;
const PET_MIN_HEIGHT = 470;

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const area = display.workArea;
  const width = PET_WIDTH;
  const height = PET_MIN_HEIGHT;
  const saved = cfg().pet;
  // 以窗口底边（蛋仔脚底）为锚点：气泡长高时窗口向上生长，脚底不动。
  const bottom = saved.bottom != null ? saved.bottom : area.y + area.height - 8;
  const clampedBottom = Math.min(Math.max(bottom, area.y + height), area.y + area.height);
  const rawX = saved.x != null ? saved.x : area.x + area.width - width - 24;
  const x = Math.min(Math.max(rawX, area.x), area.x + area.width - width);
  const y = clampedBottom - height;

  win = new BrowserWindow({
    width,
    height,
    x,
    y,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: true,
    show: false,
    roundedCorners: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.setAlwaysOnTop(true, "screen-saver");
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  win.webContents.on("did-finish-load", () => {
    log("renderer loaded; bounds=" + JSON.stringify(win.getBounds()) + " visible=" + win.isVisible());
    if (process.env.EGGY_DEBUG) {
      const capture = async (name) => {
        try {
          const img = await win.webContents.capturePage();
          const out = path.join(__dirname, "dev", name);
          fs.mkdirSync(path.dirname(out), { recursive: true });
          fs.writeFileSync(out, img.toPNG());
          log("captured " + name);
        } catch (err) {
          log("capture failed: " + (err.message || err));
        }
      };
      setTimeout(async () => {
        await capture("eggy-render.png");
        const pid = showBubble({
          kind: "permission", title: "需要确认", toolName: "pwsh",
          message: "Remove-Item -Recurse -Force D:\\temp\\build",
          warning: "该操作会删除文件", destructive: true,
          sessionKey: "agent:main:cli:demo", sessionLabel: "Opensquilla",
          approval: { id: "demo-approval", namespace: "exec" },
        });
        setMood("alert");
        setTimeout(() => capture("eggy-bubble.png"), 700);
        setTimeout(() => {
          dismissBubble(pid);
          showBubble({
            kind: "clarify", title: "需要回答", message: "任务需要先做个决定",
            requestId: "demo-clarify", sessionKey: "agent:main:cli:demo", sessionLabel: "Opensquilla",
            fields: [{
              name: "q1", prompt: "选择部署方式", type: "enum", required: true, allow_other: true,
              choices: ["方案 A", "方案 B", "方案 C", "方案 D"],
              options: [
                { label: "方案 A", description: "快速但保守" },
                { label: "方案 B", description: "彻底但耗时" },
                { label: "方案 C", description: "折中稳妥，推荐" },
                { label: "方案 D", description: "激进实验性" },
              ],
            }],
          });
          setTimeout(() => capture("eggy-clarify.png"), 700);
        }, 1600);
        setTimeout(async () => {
          try { await win.webContents.executeJavaScript("var r=document.querySelector('.b-other'); if(r) r.click(); true"); } catch { /* ignore */ }
          setTimeout(() => capture("eggy-other-editing.png"), 300);
        }, 2600);
        setTimeout(() => { send("settings:open", {}); setTimeout(() => capture("eggy-settings.png"), 700); }, 3200);
        setTimeout(async () => {
          try { await win.webContents.executeJavaScript("document.querySelector('#settings').classList.add('hidden'); showContextMenu(170, 330); true"); } catch { /* ignore */ }
          setTimeout(() => capture("eggy-ctxmenu.png"), 400);
        }, 4600);
        setTimeout(() => { if (win && !win.isDestroyed()) win.blur(); setTimeout(() => capture("eggy-ctxmenu-dismissed.png"), 300); }, 5200);
        setTimeout(async () => {
          try { await win.webContents.executeJavaScript("showContextMenu(170,330); true"); } catch { /* ignore */ }
          setTimeout(async () => {
            try { await win.webContents.executeJavaScript("var it=document.querySelector('.ctx-item'); if(it) it.click(); true"); } catch { /* ignore */ }
            setTimeout(() => capture("eggy-ctxmenu-clicked.png"), 400);
          }, 200);
        }, 5600);
        setTimeout(async () => {
          try { await win.webContents.executeJavaScript("hideContextMenu(); showQuitConfirm(); true"); } catch { /* ignore */ }
          setTimeout(() => capture("eggy-quit.png"), 400);
        }, 6800);
        setTimeout(async () => {
          try { await win.webContents.executeJavaScript("var d=document.querySelector('#conn-dot'); d.classList.add('force-label'); d.focus(); true"); } catch { /* ignore */ }
          setTimeout(() => capture("eggy-conndot.png"), 300);
        }, 7600);
      }, 1500);
    }
  });
  win.webContents.on("did-fail-load", (_e, code, desc) => log("renderer FAILED: " + code + " " + desc));
  win.webContents.on("console-message", (_e, level, message) => {
    if (level >= 2) log("renderer console: " + message);
  });
  win.once("ready-to-show", () => {
    win.show();
    setMood("idle");
  });
  win.on("closed", () => { win = null; });
  // 菜单打开时点击桌面/其它应用 → 窗口失焦 → 收起菜单。
  win.on("blur", () => send("ctxmenu:dismiss", {}));
}

function trayIcon() {
  // 优先本地 assets，回退到仓库默认 logo。
  const candidates = [
    path.join(__dirname, "assets", "opensquilla-mark.png"),
    path.join(__dirname, "..", "..", "assets", "opensquilla-mark.png"),
  ];
  for (const logo of candidates) {
    try {
      if (!fs.existsSync(logo)) continue;
      const img = nativeImage.createFromPath(logo);
      if (!img.isEmpty()) return img.resize({ width: 16, height: 16 });
    } catch { /* ignore */ }
  }
  return nativeImage.createEmpty();
}

function buildTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip("蛋仔 · OpenSquilla 桌宠");
  const menu = Menu.buildFromTemplate([
    { label: "显示 / 隐藏蛋仔", click: () => toggleVisible() },
    { label: "静音切换", type: "checkbox", checked: !!cfg().pet.muted, click: (mi) => setMuted(mi.checked) },
    { label: "打开设置", click: () => openSettings() },
    { label: "和蛋仔说话（测试语音）", click: () => enqueueSpeak("你好，我是蛋仔，OpenSquilla 的桌面小伙伴。") },
    { type: "separator" },
    { label: "退出", click: () => quitWithConfirm() },
  ]);
  tray.setContextMenu(menu);
  tray.on("click", () => toggleVisible());
}

// 托盘退出也先确认，避免误点 abrupt 退出。
function quitWithConfirm() {
  const choice = dialog.showMessageBoxSync({
    type: "question",
    buttons: ["取消", "退出"],
    defaultId: 0,
    cancelId: 0,
    title: "退出蛋仔",
    message: "确定要退出蛋仔吗？",
    detail: "退出后蛋仔不再播报提醒。",
  });
  if (choice === 1) quit();
}

function toggleVisible() {
  if (!win) return;
  if (win.isVisible()) { win.hide(); }
  else { win.show(); win.moveTop(); }
}

function setMuted(muted) {
  config.set({ pet: { muted: !!muted } });
  setMood("idle");
  if (tray) buildTray();
}

// 菜单打开时：取消鼠标穿透并聚焦宠物窗口，使菜单可点击；
// 点击桌面/其它应用会让宠物窗口失焦 → 收起菜单（无需额外幕布窗口）。
function openMenuFocus() {
  if (!win || win.isDestroyed()) return;
  win.setIgnoreMouseEvents(false);
  win.focus();
}

function closeMenuFocus() {
  if (!win || win.isDestroyed()) return;
  win.setIgnoreMouseEvents(true, { forward: true });
}

function openSettings() {
  if (!win) return;
  if (!win.isVisible()) win.show();
  win.moveTop();
  send("settings:open", {});
}

function quit() {
  if (gateway) gateway.stop();
  if (companion) companion.close();
  app.quit();
}

// ── IPC ───────────────────────────────────────────────────────────
function registerIpc() {
  ipcMain.handle("config:get", () => cfg());
  ipcMain.handle("config:set", (_e, partial) => {
    const before = cfg();
    const next = config.set(partial || {});
    // 网关目标变化 → 重连
    if (partial && partial.gateway && (partial.gateway.url !== before.gateway.url || partial.gateway.token !== before.gateway.token)) {
      gateway.setTarget(next.gateway);
    }
    if (partial && partial.pet && Object.prototype.hasOwnProperty.call(partial.pet, "muted")) {
      if (tray) buildTray();
    }
    setMood("idle");
    return next;
  });

  ipcMain.handle("state:get", () => getState());
  ipcMain.handle("sessions:list", async () => {
    try {
      const list = await gateway.listSessions();
      sessionsCache = list;
      return list.map((s) => ({ key: s.key, label: sessionLabel(s.key), status: s.status }));
    } catch (err) {
      return { error: String(err.message || err) };
    }
  });

  // 同一个气泡同时只允许一个在途动作。网关稍慢时重复点击会真的重复发送，
  // 而后端把每次新输入都当成 steer：进行中的回合会被打断，用户看到的就是
  // 「点了没反应、再点一次」，最后收到多份重复消息。
  const inflightBubbleActions = new Set();
  const withBubbleAction = async (bubbleId, action) => {
    const slot = String(bubbleId || "");
    if (slot && inflightBubbleActions.has(slot)) return { error: "正在发送中，请稍候…" };
    if (slot) inflightBubbleActions.add(slot);
    try {
      return await action();
    } finally {
      if (slot) inflightBubbleActions.delete(slot);
    }
  };

  ipcMain.handle("reply:send", async (_e, { key, text, bubbleId, clientMessageId }) => withBubbleAction(bubbleId, async () => {
    const target = key || lastActiveSession;
    if (!target) return { error: "没有可回复的会话" };
    if (!String(text || "").trim()) return { error: "内容为空" };
    try {
      if (gateway.connected) {
        await gateway.sendToSession(target, String(text), clientMessageId);
      } else {
        dispatch.enqueue({ session_id: target, text: String(text), exe: cfg().companion.exe });
      }
      // 气泡由渲染进程展示完「已发送」后自己收起，这里不再抢着关。
      setMood("happy");
      setTimeout(() => setMood("idle"), 1500);
      return { ok: true, queued: !gateway.connected };
    } catch (err) {
      return { error: String(err.message || err) };
    }
  }));

  ipcMain.handle("approval:resolve", async (_e, { id, approved, namespace, choice, bubbleId }) => withBubbleAction(bubbleId, async () => {
    try {
      await gateway.resolveApproval({ id, approved, namespace, choice });
      setMood(approved ? "happy" : "idle");
      setTimeout(() => setMood("idle"), 1500);
      return { ok: true };
    } catch (err) {
      return { error: String(err.message || err) };
    }
  }));

  ipcMain.handle("clarify:submit", async (_e, { sessionKey, requestId, fields, bubbleId }) => withBubbleAction(bubbleId, async () => {
    try {
      await gateway.submitClarify({ sessionKey, requestId, fields });
      setMood("happy");
      setTimeout(() => setMood("idle"), 1500);
      return { ok: true };
    } catch (err) {
      return { error: String(err.message || err) };
    }
  }));

  ipcMain.handle("bubble:dismiss", (_e, { id }) => { dismissBubble(id); return { ok: true }; });
  ipcMain.handle("pet:open-session", (_e, { key }) => {
    const base = gateway.httpBase;
    const url = key ? `${base}/?session=${encodeURIComponent(key)}` : base + "/";
    shell.openExternal(url);
    return { ok: true };
  });

  ipcMain.handle("tts:test", async (_e, { text } = {}) => {
    try {
      const result = await tts.speak(text || "你好，我是蛋仔。", cfg().tts, gateway ? gateway.httpBase : "");
      if (result && result.audio) send("tts:play", { ...result.audio, volume: cfg().tts.volume });
      return { ok: true, provider: result.provider || (result.skipped ? "skipped" : "windows") };
    } catch (err) {
      return { error: String(err.message || err) };
    }
  });
  ipcMain.handle("tts:models", async () => {
    try { return await tts.listModels(cfg().tts); } catch (err) { return { error: String(err.message || err) }; }
  });
  ipcMain.handle("tts:voices", async () => {
    try { return await tts.listVoices(cfg().tts); } catch (err) { return { error: String(err.message || err) }; }
  });
  ipcMain.handle("tts:windows-voices", async () => {
    try { return { voices: await tts.listWindowsVoices() }; } catch (err) { return { error: String(err.message || err) }; }
  });
  ipcMain.on("tts:ended", () => { if (audioEndResolver) audioEndResolver(); });

  ipcMain.handle("pet:set-muted", (_e, { muted }) => { setMuted(muted); return { ok: true }; });
  ipcMain.handle("pet:hide", () => { if (win) win.hide(); return { ok: true }; });
  ipcMain.handle("pet:quit", () => { quit(); return { ok: true }; });
  ipcMain.handle("pet:open-settings", () => { openSettings(); return { ok: true }; });

  ipcMain.on("pet:drag", (_e, { dx, dy }) => {
    if (!win) return;
    const [x, y] = win.getPosition();
    win.setPosition(Math.round(x + dx), Math.round(y + dy));
  });
  ipcMain.on("pet:drag-end", () => {
    if (!win) return;
    const b = win.getBounds();
    config.set({ pet: { x: b.x, bottom: b.y + b.height } });
  });
  // 气泡变化时按内容高度向上生长窗口（底边/脚底固定），超出屏幕则封顶。
  ipcMain.on("pet:fit", (_e, { height }) => {
    if (!win || win.isDestroyed()) return;
    const b = win.getBounds();
    const area = screen.getDisplayMatching(b).workArea;
    const bottom = b.y + b.height;
    const maxH = Math.max(PET_MIN_HEIGHT, bottom - area.y);
    const newH = Math.min(Math.max(Math.round(height || PET_MIN_HEIGHT), PET_MIN_HEIGHT), maxH);
    if (process.env.EGGY_DEBUG) log("pet:fit requested=" + height + " newH=" + newH + " curH=" + b.height + " bottom=" + bottom);
    if (newH === b.height) return;
    win.setBounds({ x: b.x, y: bottom - newH, width: PET_WIDTH, height: newH });
  });
  ipcMain.on("mouse-ignore", (_e, ignore) => {
    if (!win) return;
    win.setIgnoreMouseEvents(!!ignore, { forward: true });
  });
  ipcMain.on("menu:open", () => openMenuFocus());
  ipcMain.on("menu:close", () => closeMenuFocus());
}

// ── 启动 ──────────────────────────────────────────────────────────
function startServices() {
  const gatewayCfg = { ...cfg().gateway };
  // 桌面 app 拉起蛋仔时通过 EGGY_GATEWAY_URL 传入它自己的网关地址。
  if (process.env.EGGY_GATEWAY_URL) gatewayCfg.url = process.env.EGGY_GATEWAY_URL;
  gateway = new GatewayClient(gatewayCfg);
  gateway.on("event", onGatewayEvent);
  gateway.on("sessions", (list) => {
    sessionsCache = list;
    send("sessions:updated", list.map((s) => ({ key: s.key, label: sessionLabel(s.key), status: s.status })));
  });
  gateway.on("status", (s) => {
    send("pet:state", { connected: s.connected, muted: isMuted(), error: s.error || "" });
    if (s.connected) setMood("idle");
  });
  gateway.on("log", (m) => log(m));
  gateway.start();

  const port = Number(process.env.EGGY_PORT || cfg().companion.port);
  actualCompanionPort = port;
  companion = createCompanionServer({
    port,
    dispatch,
    handlers: { getState, onNotify, onSpeak },
    onLog: log,
  });
  companion.ready.then((p) => { if (p) actualCompanionPort = p; });

  importSpool();
  refreshPause();
  setInterval(refreshPause, 3000);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => { if (win) { win.show(); win.moveTop(); } });
  app.whenReady().then(() => {
    config = new Config(app.getPath("userData"));
    if (IS_WIN) app.setAppUserModelId("org.opensquilla.pet");
    registerIpc();
    createWindow();
    buildTray();
    startServices();
  });
  app.on("window-all-closed", () => { /* 托盘常驻，不退出 */ });
  app.on("before-quit", () => { if (gateway) gateway.stop(); });
}