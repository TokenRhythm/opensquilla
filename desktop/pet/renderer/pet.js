// 蛋仔渲染进程：角色状态 / 气泡 / 设置 / 拖动 / 音频。
"use strict";
const api = window.petAPI;
const $ = (sel) => document.querySelector(sel);

const pet = $("#pet");
const bubbleLayer = $("#bubble-layer");
const settingsEl = $("#settings");
const toastEl = $("#toast");
const ctxMenu = $("#ctxmenu");
const connDot = $("#conn-dot");

let config = null;
let sessions = [];
let petSize = 1;
let connConnected = false;
let connMuted = false;
let lastActiveKey = "";
const bubbles = new Map(); // id → {node, timer}
let currentAudio = null;
let toastTimer = null;

// ── DOM 工具 ──────────────────────────────────────────────────────
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function toast(text) {
  toastEl.textContent = text;
  toastEl.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 3500);
}

// ── 角色状态 ──────────────────────────────────────────────────────
let moodTimer = null;
function setMood(mood) {
  pet.classList.remove("mood-idle", "mood-talk", "mood-happy", "mood-alert", "mood-think");
  pet.classList.add("mood-" + (mood || "idle"));
  if (mood === "happy") spawnSparkles();
}
function spawnSparkles() {
  const fx = $("#fx");
  for (let i = 0; i < 6; i++) {
    const s = el("div", { class: "sparkle" });
    s.style.left = 30 + Math.random() * 110 + "px";
    s.style.top = 30 + Math.random() * 90 + "px";
    s.style.animationDelay = Math.random() * 0.3 + "s";
    fx.appendChild(s);
    setTimeout(() => s.remove(), 1300);
  }
}
function wave() {
  pet.classList.add("waving");
  setTimeout(() => pet.classList.remove("waving"), 1600);
}

// ── 气泡 ──────────────────────────────────────────────────────────
const KIND_ICON = { permission: "🔐", plan: "🧭", clarify: "❓", reply: "💬", stop: "✅", message: "💬" };

function closeBubble(id) {
  const entry = bubbles.get(id);
  if (!entry) return;
  if (entry.timer) clearTimeout(entry.timer);
  entry.node.remove();
  bubbles.delete(id);
  fitWindow();
}

function bubbleShell(b, bodyChildren) {
  const closeBtn = el("button", { class: "icon-btn", title: "关闭", onclick: () => { closeBubble(b.id); api.invoke("bubble:dismiss", { id: b.id }); } }, "×");
  const head = el("div", { class: "b-head" }, [
    el("span", { text: KIND_ICON[b.kind] || "💬" }),
    el("span", { class: "b-title", text: b.title || "蛋仔" }),
    b.sessionLabel ? el("span", { class: "b-session", text: b.sessionLabel, title: b.sessionKey || "" }) : null,
    closeBtn,
  ]);
  const node = el("div", { class: "bubble", "data-id": b.id }, [head, ...bodyChildren]);
  return node;
}

function sessionSelect(selected) {
  const sel = el("select", { class: "b-select" });
  const list = sessions.length ? sessions : [{ key: selected || lastActiveKey, label: selected || lastActiveKey || "当前会话" }];
  for (const s of list) {
    if (!s.key) continue;
    sel.appendChild(el("option", { value: s.key, text: s.label || s.key, selected: s.key === selected ? "" : null }));
  }
  return sel;
}

function errorLine(node) {
  const line = el("div", { class: "b-err" });
  node.appendChild(line);
  return (msg) => { line.textContent = msg || ""; };
}

function renderBubble(b) {
  closeBubble(b.id);
  let body = [];
  let node;

  if (b.kind === "permission") {
    if (b.toolName) body.push(el("div", { class: "b-msg", text: "工具：" + b.toolName }));
    if (b.message) body.push(el("div", { class: "b-cmd", text: b.message }));
    if (b.warning) body.push(el("div", { class: "b-warn", text: "⚠ " + b.warning }));
    if (b.destructive) body.push(el("div", { class: "b-warn", text: "⚠ 该操作可能不可逆，请谨慎确认。" }));
    const err = el("div", { class: "b-err" });
    const act = async (approved) => {
      const r = await api.invoke("approval:resolve", { id: b.approval.id, approved, namespace: b.approval.namespace, bubbleId: b.id });
      if (r && r.error) err.textContent = r.error;
    };
    body.push(el("div", { class: "b-actions" }, [
      el("button", { class: "btn primary", onclick: () => act(true) }, "允许"),
      el("button", { class: "btn danger", onclick: () => act(false) }, "拒绝"),
      el("button", { class: "btn", onclick: () => api.invoke("pet:open-session", { key: b.sessionKey }) }, "去会话"),
    ]));
    body.push(err);
    node = bubbleShell(b, body);
  } else if (b.kind === "clarify") {
    if (b.message) body.push(el("div", { class: "b-msg", text: b.message }));
    const inputs = {};
    for (const f of b.fields || []) {
      const name = f.name || f.key;
      if (!name) continue;
      const label = f.prompt || f.label || f.title || name;
      const type = String(f.type || "string").toLowerCase();
      const isEnum = type === "enum" || type === "choice";
      const rawOpts = (Array.isArray(f.options) && f.options.length) ? f.options : (f.choices || f.enum || []);
      const opts = (isEnum || rawOpts.length)
        ? rawOpts.map((o) => typeof o === "string"
          ? { value: o, label: o, desc: "" }
          : { value: String(o.value ?? o.label ?? o.title ?? ""), label: String(o.label ?? o.title ?? o.value ?? ""), desc: o.description || "" })
        : [];

      if (type === "boolean" || type === "bool") {
        const input = el("input", { type: "checkbox" });
        if (f.default) input.checked = true;
        inputs[name] = () => input.checked;
        body.push(el("div", { class: "b-field" }, [el("label", { class: "chk" }, [input, " " + label])]));
        continue;
      }

      if (opts.length) {
        // 选项按钮 + 「其他」：点击后该行原地变为输入框（合并，不另起一行）。
        let chosen = "";
        let otherMode = false;
        const optButtons = [];
        const wrap = el("div", { class: "b-opts" });
        const otherRow = el("div", { class: "b-opt b-other", tabindex: "0" });
        const otherLabel = el("span", { class: "b-other-label" }, [
          el("span", { class: "b-other-icon", text: "✏️" }),
          el("span", { text: "其他（自己输入）" }),
        ]);
        const otherInput = el("input", { type: "text", class: "b-other-input", placeholder: "输入其他回复…" });
        otherRow.appendChild(otherLabel);
        otherRow.appendChild(otherInput);
        const enterOther = () => {
          otherMode = true; chosen = "";
          optButtons.forEach((x) => x.classList.remove("selected"));
          otherRow.classList.add("selected", "editing");
          otherInput.focus();
        };
        otherRow.addEventListener("click", () => { if (!otherMode) enterOther(); else otherInput.focus(); });
        otherInput.addEventListener("click", (e) => e.stopPropagation());
        otherInput.addEventListener("keydown", (e) => {
          if (e.key === "Escape") { otherMode = false; otherRow.classList.remove("selected", "editing"); }
        });
        for (const o of opts) {
          const btn = el("button", { class: "b-opt", onclick: () => {
            otherMode = false; chosen = o.value;
            optButtons.forEach((x) => x.classList.remove("selected"));
            otherRow.classList.remove("selected", "editing");
            btn.classList.add("selected");
          } }, [
            el("span", { text: o.label }),
            o.desc ? el("span", { class: "b-opt-desc", text: o.desc }) : null,
          ]);
          optButtons.push(btn);
          wrap.appendChild(btn);
        }
        wrap.appendChild(otherRow);
        inputs[name] = () => (otherMode ? otherInput.value.trim() : chosen);
        body.push(el("div", { class: "b-field" }, [el("label", { text: label + (f.required ? " *" : "") }), wrap]));
        continue;
      }

      let input;
      if (type === "number" || type === "integer" || type === "int") {
        input = el("input", { type: "number", class: "b-input", placeholder: f.placeholder || "" });
        inputs[name] = () => (input.value === "" ? "" : Number(input.value));
      } else {
        input = el("input", { type: "text", class: "b-input", placeholder: f.placeholder || "输入回复…" });
        if (f.default != null) input.value = String(f.default);
        inputs[name] = () => input.value;
      }
      body.push(el("div", { class: "b-field" }, [el("label", { text: label + (f.required ? " *" : "") }), input]));
    }
    const err = el("div", { class: "b-err" });
    body.push(el("div", { class: "b-actions" }, [
      el("button", {
        class: "btn primary",
        onclick: async () => {
          const fields = {};
          for (const [k, get] of Object.entries(inputs)) fields[k] = get();
          const r = await api.invoke("clarify:submit", { sessionKey: b.sessionKey, requestId: b.requestId, fields, bubbleId: b.id });
          if (r && r.error) err.textContent = r.error;
        },
      }, "提交"),
      el("button", { class: "btn", onclick: () => api.invoke("pet:open-session", { key: b.sessionKey }) }, "去会话"),
    ]));
    body.push(err);
    node = bubbleShell(b, body);
  } else if (b.kind === "plan") {
    if (b.message) body.push(el("div", { class: "b-msg", text: b.message }));
    const err = el("div", { class: "b-err" });
    if (Array.isArray(b.options) && b.options.length) {
      const opts = el("div", { class: "b-opts" });
      for (const o of b.options) {
        const val = typeof o === "string" ? o : (o.value ?? o.label ?? o.title);
        const lab = typeof o === "string" ? o : (o.label ?? o.title ?? String(val));
        opts.appendChild(el("button", {
          class: "b-opt",
          onclick: async () => {
            const r = await api.invoke("reply:send", { key: b.sessionKey, text: String(val), bubbleId: b.id });
            if (r && r.error) err.textContent = r.error;
          },
        }, String(lab)));
      }
      body.push(opts);
    }
    const ta = el("textarea", { class: "b-textarea", placeholder: "自己输入其他回复…" });
    body.push(el("div", { class: "b-field" }, [ta]));
    body.push(el("div", { class: "b-actions" }, [
      el("button", {
        class: "btn primary",
        onclick: async () => {
          const r = await api.invoke("reply:send", { key: b.sessionKey, text: ta.value, bubbleId: b.id });
          if (r && r.error) err.textContent = r.error;
        },
      }, "发送"),
      el("button", { class: "btn", onclick: () => api.invoke("pet:open-session", { key: b.sessionKey }) }, "去会话"),
    ]));
    body.push(err);
    node = bubbleShell(b, body);
  } else if (b.kind === "reply") {
    const sel = sessionSelect(b.sessionKey || lastActiveKey);
    const ta = el("textarea", { class: "b-textarea", placeholder: "输入要发送到会话的内容…", rows: 3 });
    const err = el("div", { class: "b-err" });
    const send = async () => {
      const r = await api.invoke("reply:send", { key: sel.value, text: ta.value, bubbleId: b.id });
      if (r && r.error) err.textContent = r.error;
      else if (r && r.queued) toast("已加入派发队列（网关离线）");
    };
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); });
    body.push(el("div", { class: "b-field" }, [el("label", { text: "目标会话" }), sel]));
    body.push(el("div", { class: "b-field" }, [ta]));
    body.push(el("div", { class: "b-actions" }, [
      el("button", { class: "btn primary", onclick: send }, "发送 (Ctrl+Enter)"),
    ]));
    body.push(err);
    node = bubbleShell(b, body);
    setTimeout(() => ta.focus(), 50);
  } else if (b.kind === "confirm") {
    if (b.message) body.push(el("div", { class: "b-msg", text: b.message }));
    body.push(el("div", { class: "b-actions" }, [
      el("button", { class: "btn danger", onclick: () => { closeBubble(b.id); if (b.onConfirm) b.onConfirm(); } }, b.confirmLabel || "确认"),
      el("button", { class: "btn", onclick: () => closeBubble(b.id) }, "取消"),
    ]));
    node = bubbleShell(b, body);
  } else {
    // message / stop
    if (b.message) body.push(el("div", { class: "b-msg", text: b.message }));
    if (b.autoDismiss) {
      const bar = el("i");
      bar.style.animationDuration = b.autoDismiss + "ms";
      body.push(el("div", { class: "b-progress" }, [bar]));
    }
    node = bubbleShell(b, body);
  }

  bubbleLayer.appendChild(node);
  const entry = { node, timer: null };
  if (b.autoDismiss) entry.timer = setTimeout(() => closeBubble(b.id), b.autoDismiss);
  bubbles.set(b.id, entry);
  // 最多同时 2 个气泡（头顶空间有限），移除最旧。
  while (bubbles.size > 2) {
    const oldest = bubbles.keys().next().value;
    closeBubble(oldest);
  }
  fitWindow();
}

// ── 设置 ──────────────────────────────────────────────────────────
function fillSettings() {
  const c = config;
  $("#gw-url").value = c.gateway.url || "";
  $("#gw-token").value = c.gateway.token || "";
  $("#tts-provider").value = c.tts.provider || "windows";
  $("#tts-base").value = c.tts.baseUrl || "";
  $("#tts-key").value = c.tts.apiKey || "";
  $("#tts-model").value = c.tts.model || "";
  $("#tts-voice").value = c.tts.voice || "";
  $("#tts-voices-url").value = c.tts.voicesUrl || "";
  $("#tts-volume").value = c.tts.volume ?? 1;
  $("#tts-rate").value = c.tts.rate ?? 1;
  $("#tts-enabled").checked = c.tts.enabled !== false;
  $("#pet-muted").checked = !!c.pet.muted;
  $("#pet-autospeak").checked = c.pet.autoSpeak !== false;
  $("#pet-turnstart").checked = !!c.pet.showTurnStart;
  $("#pet-size").value = c.pet.size ?? 1;
  toggleProvider();
  if (c.tts.provider === "windows") loadWindowsVoices(c.tts.voice);
}

function toggleProvider() {
  const p = $("#tts-provider").value;
  $("#tts-windows").classList.toggle("hidden", p !== "windows");
  $("#tts-openai").classList.toggle("hidden", p !== "openai");
}

async function loadWindowsVoices(selected) {
  const sel = $("#win-voice");
  sel.innerHTML = "";
  sel.appendChild(el("option", { value: "", text: "（默认音色）" }));
  const r = await api.invoke("tts:windows-voices");
  if (r && r.voices) {
    for (const v of r.voices) sel.appendChild(el("option", { value: v, text: v, selected: v === selected ? "" : null }));
  } else if (r && r.error) {
    $("#tts-msg").textContent = "音色读取失败：" + r.error;
  }
}

function gatherSettings() {
  return {
    gateway: { url: $("#gw-url").value.trim(), token: $("#gw-token").value.trim() },
    tts: {
      provider: $("#tts-provider").value,
      baseUrl: $("#tts-base").value.trim(),
      apiKey: $("#tts-key").value.trim(),
      model: $("#tts-model").value.trim(),
      voice: $("#tts-provider").value === "windows" ? $("#win-voice").value : $("#tts-voice").value.trim(),
      voicesUrl: $("#tts-voices-url").value.trim(),
      volume: Number($("#tts-volume").value),
      rate: Number($("#tts-rate").value),
      enabled: $("#tts-enabled").checked,
    },
    pet: {
      muted: $("#pet-muted").checked,
      autoSpeak: $("#pet-autospeak").checked,
      showTurnStart: $("#pet-turnstart").checked,
      size: Number($("#pet-size").value),
    },
  };
}

function applySize() {
  const size = (config && config.pet && config.pet.size) || 1;
  petSize = size;
  pet.style.transform = `translateX(-50%) scale(${size})`;
  pet.style.transformOrigin = "50% 100%";
  // 气泡层始终贴在宠物头顶上方（随缩放联动），尾巴指向蛋仔。
  bubbleLayer.style.bottom = Math.round(200 * size + 18) + "px";
  fitWindow();
}

// 气泡层可用高度（窗口被屏幕封顶时启用滚动）。
function updateBubbleBounds() {
  const anchor = Math.round(200 * petSize + 18);
  bubbleLayer.style.maxHeight = Math.max(120, window.innerHeight - anchor - 12) + "px";
}

// 按气泡内容高度请求窗口向上生长（脚底固定），避免长选择卡被裁切。
function fitWindow() {
  requestAnimationFrame(() => {
    updateBubbleBounds();
    const content = bubbleLayer.scrollHeight;
    bubbleLayer.scrollTop = bubbleLayer.scrollHeight; // 最新气泡贴近宠物
    api.send("pet:fit", { height: Math.round(200 * petSize + 18) + content + 16 });
  });
}
window.addEventListener("resize", updateBubbleBounds);

function bindSettings() {
  $("#set-close").addEventListener("click", () => settingsEl.classList.add("hidden"));
  $("#tts-provider").addEventListener("change", () => {
    toggleProvider();
    if ($("#tts-provider").value === "windows") loadWindowsVoices($("#win-voice").value);
  });
  $("#win-voice-fetch").addEventListener("click", () => loadWindowsVoices($("#win-voice").value));
  $("#model-fetch").addEventListener("click", async () => {
    $("#tts-msg").textContent = "拉取模型中…";
    await api.invoke("config:set", { tts: { baseUrl: $("#tts-base").value.trim(), apiKey: $("#tts-key").value.trim() } });
    const r = await api.invoke("tts:models");
    const dl = $("#model-list");
    dl.innerHTML = "";
    if (r && r.models) {
      for (const m of r.models) dl.appendChild(el("option", { value: m }));
      $("#tts-msg").textContent = `拉到 ${r.models.length} 个模型`;
    } else {
      $("#tts-msg").textContent = "拉取失败：" + (r && r.error);
    }
  });
  $("#voice-fetch").addEventListener("click", async () => {
    $("#tts-msg").textContent = "拉取音色中…";
    await api.invoke("config:set", { tts: { baseUrl: $("#tts-base").value.trim(), apiKey: $("#tts-key").value.trim(), voicesUrl: $("#tts-voices-url").value.trim() } });
    const r = await api.invoke("tts:voices");
    const dl = $("#voice-list");
    dl.innerHTML = "";
    if (r && r.voices) {
      for (const v of r.voices) dl.appendChild(el("option", { value: v }));
      $("#tts-msg").textContent = `音色来源：${r.source}（${r.voices.length}）`;
    } else {
      $("#tts-msg").textContent = "拉取失败：" + (r && r.error);
    }
  });
  $("#tts-test").addEventListener("click", async () => {
    await api.invoke("config:set", gatherSettings());
    config = await api.invoke("config:get");
    $("#tts-msg").textContent = "试听中…";
    const r = await api.invoke("tts:test", { text: "你好，我是蛋仔，OpenSquilla 的桌面小伙伴。" });
    $("#tts-msg").textContent = r && r.error ? "试听失败：" + r.error : "已播放（" + (r.provider || "") + "）";
  });
  $("#set-save").addEventListener("click", async () => {
    config = await api.invoke("config:set", gatherSettings());
    applySize();
    settingsEl.classList.add("hidden");
    toast("设置已保存");
  });
}

// ── 拖动 / 点击 / 鼠标穿透 ─────────────────────────────────────────
function bindPetInteraction() {
  let dragging = false;
  let moved = 0;
  let lastX = 0, lastY = 0;
  pet.addEventListener("pointerdown", (e) => {
    if (e.button === 2) return;
    dragging = true; moved = 0; lastX = e.screenX; lastY = e.screenY;
    try { pet.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  });
  pet.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.screenX - lastX, dy = e.screenY - lastY;
    lastX = e.screenX; lastY = e.screenY;
    moved += Math.abs(dx) + Math.abs(dy);
    if (dx || dy) api.send("pet:drag", { dx, dy });
  });
  pet.addEventListener("pointerup", (e) => {
    if (!dragging) return;
    dragging = false;
    api.send("pet:drag-end");
    if (moved < 5) onPetClick();
  });
  pet.addEventListener("contextmenu", (e) => { e.preventDefault(); showContextMenu(e.clientX, e.clientY); });

  // 鼠标穿透：仅角色/气泡/设置/菜单/提示区域可交互。
  window.addEventListener("mousemove", (e) => {
    const target = document.elementFromPoint(e.clientX, e.clientY);
    const interactive = target && target.closest(".bubble, #pet, #settings, #ctxmenu, #toast");
    api.send("mouse-ignore", !interactive);
  });

  // 点击菜单外或按 Esc 关闭右键菜单。
  document.addEventListener("pointerdown", (e) => {
    if (!ctxMenu.classList.contains("hidden") && !ctxMenu.contains(e.target)) hideContextMenu();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideContextMenu(); });
}

async function onPetClick() {
  wave();
  await openReplyUI(lastActiveKey);
}

// ── 自定义右键菜单（图标占左槽，无原生菜单的空边） ──────────────────
function openSettingsUI() {
  api.invoke("config:get").then((c) => { config = c; fillSettings(); settingsEl.classList.remove("hidden"); });
}

async function openReplyUI(key) {
  if (key) lastActiveKey = key;
  const r = await api.invoke("sessions:list");
  if (Array.isArray(r)) sessions = r;
  renderBubble({ id: "reply-" + Date.now(), kind: "reply", title: "回复会话", sessionKey: lastActiveKey });
}

function hideContextMenu() {
  ctxMenu.classList.add("hidden");
  ctxMenu.innerHTML = "";
  api.send("menu:close");
}

function ctxItem(icon, label, onClick, opts = {}) {
  return el("button", { class: "ctx-item" + (opts.danger ? " danger" : ""), onclick: () => { hideContextMenu(); onClick(); } }, [
    el("span", { class: "ctx-icon", text: icon }),
    el("span", { class: "ctx-label", text: label }),
    opts.state ? el("span", { class: "ctx-state", text: opts.state }) : null,
  ]);
}

function showContextMenu(x, y) {
  ctxMenu.innerHTML = "";
  const muted = !!(config && config.pet && config.pet.muted);
  ctxMenu.appendChild(ctxItem("💬", "回复当前会话", () => openReplyUI(lastActiveKey)));
  ctxMenu.appendChild(ctxItem("⚙️", "打开设置", openSettingsUI));
  ctxMenu.appendChild(ctxItem(muted ? "🔇" : "🔊", "静音切换", async () => {
    const next = !muted;
    await api.invoke("pet:set-muted", { muted: next });
    if (config && config.pet) config.pet.muted = next;
    updateConnDot(undefined, next);
    toast(next ? "已静音" : "已取消静音");
  }));
  ctxMenu.appendChild(ctxItem("🙈", "隐藏蛋仔", () => api.invoke("pet:hide")));
  ctxMenu.appendChild(el("div", { class: "ctx-sep" }));
  ctxMenu.appendChild(ctxItem("🚪", "退出", showQuitConfirm, { danger: true }));
  ctxMenu.classList.remove("hidden");
  const w = ctxMenu.offsetWidth, h = ctxMenu.offsetHeight;
  const left = Math.min(Math.max(x - 12, 8), window.innerWidth - w - 8);
  let bottom = window.innerHeight - y + 8;
  if (bottom + h > window.innerHeight - 8) bottom = window.innerHeight - h - 8;
  if (bottom < 8) bottom = 8;
  ctxMenu.style.left = left + "px";
  ctxMenu.style.bottom = bottom + "px";
  api.send("menu:open");
}

// 退出不突兀：先挥手告别 + 确认气泡，确认后才真正退出。
function showQuitConfirm() {
  wave();
  renderBubble({
    id: "quit-confirm", kind: "confirm", title: "退出蛋仔",
    message: "确定要退出吗？退出后蛋仔不再播报提醒。",
    confirmLabel: "确认退出", onConfirm: () => api.invoke("pet:quit"),
  });
}

// ── 音频 ──────────────────────────────────────────────────────────
function playAudio({ mime, base64, volume }) {
  try {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    const audio = new Audio(`data:${mime};base64,${base64}`);
    audio.volume = Math.max(0, Math.min(1, volume ?? 1));
    currentAudio = audio;
    const done = () => { if (currentAudio === audio) currentAudio = null; api.send("tts:ended"); };
    audio.addEventListener("ended", done);
    audio.addEventListener("error", done);
    audio.play().catch(done);
  } catch {
    api.send("tts:ended");
  }
}

// ── IPC 订阅 ──────────────────────────────────────────────────────
// 连接状态指示灯：绿=已连接，灰=未连接，橙=静音；悬停显示文字说明。
function updateConnDot(connected, muted, error) {
  if (typeof connected === "boolean") connConnected = connected;
  if (typeof muted === "boolean") connMuted = muted;
  connDot.classList.toggle("on", connConnected);
  connDot.classList.toggle("muted", connMuted);
  const label = (connConnected ? "网关已连接" : "网关未连接") + (connMuted ? " · 已静音" : "") + (error ? " · " + error : "");
  connDot.dataset.label = label;
  connDot.setAttribute("aria-label", label);
}

function bindIpc() {
  api.on("bubble:show", (b) => { if (b.sessionKey) lastActiveKey = b.sessionKey; renderBubble(b); });
  api.on("bubble:dismiss", ({ id }) => closeBubble(id));
  api.on("pet:state", (s) => {
    if (s.mood) setMood(s.mood);
    updateConnDot(s.connected, s.muted, s.error);
  });
  api.on("pet:toast", ({ text }) => toast(text));
  api.on("tts:play", playAudio);
  api.on("sessions:updated", (list) => { sessions = list || []; });
  api.on("settings:open", () => openSettingsUI());
  api.on("reply:open", ({ key }) => openReplyUI(key));
  api.on("ctxmenu:dismiss", () => hideContextMenu());
}

// ── 启动 ──────────────────────────────────────────────────────────
async function init() {
  config = await api.invoke("config:get");
  applySize();
  bindSettings();
  bindPetInteraction();
  bindIpc();
  const state = await api.invoke("state:get");
  updateConnDot(!!state.gateway_connected, !!state.muted);
  setMood("idle");
}
init();