// 网关 WebSocket 客户端：握手 / RPC / 事件订阅 / 自动重连。
// 协议参考 src/opensquilla/gateway_client.py 与 gateway/websocket.py：
//   connect.challenge → connect(req) → hello-ok；req/res；event 帧 payload 带 session_key。
// 仅用 Node 内置 WebSocket（Electron 42 / Node 22+），零第三方依赖。
"use strict";
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");

const READ_WRITE_ADMIN = ["operator.admin"];

function normalizeWsUrl(url) {
  let u = String(url || "").trim();
  if (!u) u = "ws://127.0.0.1:18791/ws";
  if (!/^[a-z]+:\/\//i.test(u)) u = "ws://" + u;
  u = u.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
  const parsed = new URL(u);
  if (!parsed.pathname || parsed.pathname === "/") parsed.pathname = "/ws";
  return parsed.toString();
}

function httpBaseFromWs(wsUrl) {
  const parsed = new URL(wsUrl);
  const scheme = parsed.protocol === "wss:" ? "https:" : "http:";
  let p = parsed.pathname.replace(/\/ws\/?$/, "");
  return `${scheme}//${parsed.host}${p}`.replace(/\/$/, "");
}

class GatewayClient extends EventEmitter {
  constructor({ url, token } = {}) {
    super();
    this.url = normalizeWsUrl(url);
    this.token = token || "";
    this.httpBase = httpBaseFromWs(this.url);
    this.ws = null;
    this.connected = false;
    this.destroyed = false;
    this._pending = new Map();
    this._nextId = 1;
    this._backoff = 1000;
    this._heartbeat = null;
    this._reconnectTimer = null;
    this._handshake = null;
    this._subscribed = new Set();
    this._sessions = [];
    this._refreshTimer = null;
  }

  start() {
    this.destroyed = false;
    this._connect();
  }

  stop() {
    this.destroyed = true;
    this.connected = false;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    if (this._heartbeat) clearInterval(this._heartbeat);
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this._failAllPending(new Error("client stopped"));
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
  }

  setTarget({ url, token }) {
    const nextUrl = normalizeWsUrl(url);
    const changed = nextUrl !== this.url || (token || "") !== this.token;
    this.url = nextUrl;
    this.token = token || "";
    this.httpBase = httpBaseFromWs(this.url);
    if (changed && !this.destroyed) {
      this._subscribed.clear();
      if (this.ws) { try { this.ws.close(); } catch { /* ignore */ } }
      else this._connect();
    }
  }

  _emitStatus(extra = {}) {
    this.emit("status", { connected: this.connected, url: this.url, ...extra });
  }

  _connect() {
    if (this.destroyed) return;
    if (typeof WebSocket === "undefined") {
      this._emitStatus({ error: "当前运行时没有内置 WebSocket" });
      this._scheduleReconnect();
      return;
    }
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (err) {
      this._emitStatus({ error: String(err.message || err) });
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    this._handshake = { stage: "challenge" };

    ws.addEventListener("message", (ev) => this._onMessage(ev));
    ws.addEventListener("error", () => {
      // close 事件随后触发重连；这里只记录。
    });
    ws.addEventListener("close", () => {
      if (this.ws !== ws) return;
      this.connected = false;
      this.ws = null;
      this._subscribed.clear();
      if (this._heartbeat) clearInterval(this._heartbeat);
      this._failAllPending(new Error("gateway connection closed"));
      this._emitStatus({ error: "连接断开" });
      this._scheduleReconnect();
    });
  }

  _scheduleReconnect() {
    if (this.destroyed) return;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    const delay = this._backoff;
    this._backoff = Math.min(this._backoff * 2, 30000);
    this._reconnectTimer = setTimeout(() => this._connect(), delay);
  }

  async _onMessage(ev) {
    let frame;
    try { frame = JSON.parse(ev.data); } catch { return; }
    const type = frame.type;

    if (type === "event" && frame.event === "connect.challenge") {
      const params = {
        minProtocol: 1,
        maxProtocol: 3,
        role: "operator",
        scopes: READ_WRITE_ADMIN,
        client: { id: "opensquilla-pet", version: "0.1.0", platform: process.platform, mode: "pet" },
      };
      if (this.token) params.auth = { token: this.token };
      this._send({ type: "req", id: "__connect__", method: "connect", params });
      this._handshake = { stage: "hello" };
      return;
    }

    if (type === "hello-ok") {
      this.connected = true;
      this._backoff = 1000;
      const policy = frame.policy || {};
      const keepalive = Number(policy.client_ws_keepalive_timeout_ms || 120000);
      const interval = Math.max(15000, Math.floor(keepalive * 0.4));
      if (this._heartbeat) clearInterval(this._heartbeat);
      this._heartbeat = setInterval(() => {
        this._send({ type: "ping" });
      }, interval);
      this._emitStatus();
      this.emit("ready", { policy });
      // 订阅全局会话变更 + 逐会话消息事件。
      this._bootstrapSubscriptions().catch((err) => {
        this.emit("log", "subscribe failed: " + (err.message || err));
      });
      return;
    }

    if (type === "res") {
      if (frame.id === "__connect__") {
        if (!frame.ok) {
          this._emitStatus({ error: (frame.error && frame.error.message) || "握手被拒" });
          try { this.ws && this.ws.close(); } catch { /* ignore */ }
        }
        return;
      }
      const p = this._pending.get(frame.id);
      if (p) {
        this._pending.delete(frame.id);
        clearTimeout(p.timer);
        if (frame.ok) p.resolve(frame.payload || {});
        else p.reject(new Error((frame.error && frame.error.message) || "RPC failed"));
      }
      return;
    }

    if (type === "event") {
      const name = frame.event || "";
      const payload = frame.payload || {};
      if (name === "sessions.changed") {
        this._scheduleSubscriptionRefresh();
      }
      this.emit("event", { name, payload });
      return;
    }
    // pong / 其它忽略
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      try { this.ws.send(JSON.stringify(obj)); } catch { /* ignore */ }
    }
  }

  call(method, params, timeoutMs = 15000) {
    if (!this.connected || !this.ws) return Promise.reject(new Error("网关未连接"));
    const id = String(this._nextId++);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(method + " 超时"));
      }, timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      this._send({ type: "req", id, method, params: params || {} });
    });
  }

  _failAllPending(err) {
    for (const [, p] of this._pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this._pending.clear();
  }

  async _bootstrapSubscriptions() {
    try { await this.call("sessions.subscribe", {}); } catch { /* ignore */ }
    await this.refreshSessions();
  }

  _scheduleSubscriptionRefresh() {
    if (this._refreshTimer) return;
    this._refreshTimer = setTimeout(() => {
      this._refreshTimer = null;
      this.refreshSessions().catch(() => {});
    }, 800);
  }

  async refreshSessions() {
    const res = await this.call("sessions.list", { limit: 60 });
    const list = Array.isArray(res.sessions) ? res.sessions : [];
    this._sessions = list;
    this.emit("sessions", list);
    // 逐个串行订阅，避免并发请求撑爆网关的单连接请求队列。
    for (const s of list) {
      const key = s && s.key;
      if (!key || this._subscribed.has(key)) continue;
      this._subscribed.add(key);
      try {
        await this.call("sessions.messages.subscribe", { key });
      } catch {
        this._subscribed.delete(key);
      }
    }
    return list;
  }

  // ── 高层 RPC 封装 ───────────────────────────────────────────────
  listSessions() {
    return this.call("sessions.list", { limit: 60 }).then((r) => r.sessions || []);
  }

  getApprovals() {
    return this.call("exec.approvals.get", {});
  }

  resolveApproval({ id, approved, namespace, choice }) {
    const method = namespace === "plugin" ? "plugin.approval.resolve" : "exec.approval.resolve";
    const params = { id, approved: !!approved };
    if (choice) params.choice = String(choice);
    return this.call(method, params);
  }

  submitClarify({ sessionKey, requestId, fields }) {
    return this.call("chat.clarify_submit", { sessionKey, requestId, fields });
  }

  sendToSession(key, text) {
    const cmid = "pet-" + crypto.randomUUID().replace(/-/g, "");
    return this.call("sessions.send", {
      key,
      message: text,
      attachments: [],
      client_message_id: cmid,
    });
  }

  sessionHistory(key, limit = 40) {
    return this.call("chat.history", { sessionKey: key, limit });
  }
}

module.exports = { GatewayClient, normalizeWsUrl, httpBaseFromWs };