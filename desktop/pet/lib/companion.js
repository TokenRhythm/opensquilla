// 本地回环 companion HTTP 契约（与网关 opensquilla.pet.bridge_client 对齐）：
//   GET  /state                 → 200 JSON
//   POST /speak   {message,title?}        → 204
//   POST /notify  {event,title,message,…} → 204
//   GET  /dispatch/pending?exe= → 200 JSON 列表
//   POST /dispatch/ack {dispatch_id,ok,error?} → 204
// body ≤ 64KB（超 413）；仅绑定 127.0.0.1。零第三方依赖（node:http）。
"use strict";
const http = require("node:http");
const crypto = require("node:crypto");

const MAX_BODY = 64 * 1024;

class DispatchQueue {
  constructor() {
    this.items = new Map(); // dispatch_id → item
  }

  enqueue({ session_id, text, exe }) {
    const dispatch_id = crypto.randomUUID();
    const item = {
      dispatch_id,
      session_id: String(session_id || ""),
      text: String(text || ""),
      exe: String(exe || "OpenSquilla.exe"),
      created_at: Date.now(),
    };
    this.items.set(dispatch_id, item);
    // 简单上限，避免无限增长。
    if (this.items.size > 200) {
      const oldest = [...this.items.values()].sort((a, b) => a.created_at - b.created_at)[0];
      if (oldest) this.items.delete(oldest.dispatch_id);
    }
    return dispatch_id;
  }

  pending(exe) {
    const now = Date.now();
    const out = [];
    for (const [id, item] of this.items) {
      // 120s 未被取走/ack 的条目过期（与原生侧超时取消语义一致）。
      if (now - item.created_at > 120000) {
        this.items.delete(id);
        continue;
      }
      if (!exe || item.exe.toLowerCase() === String(exe).toLowerCase()) {
        out.push({ dispatch_id: item.dispatch_id, session_id: item.session_id, text: item.text });
      }
    }
    return out;
  }

  ack(dispatch_id) {
    return this.items.delete(String(dispatch_id || ""));
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    let tooLarge = false;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        // 超限：丢弃后续内容但继续排空请求体，随后回 413。
        // 不 req.destroy()——在 Windows 上 abrupt reset 会触发 libuv 断言。
        tooLarge = true;
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (tooLarge) {
        reject(Object.assign(new Error("body too large"), { status: 413 }));
        return;
      }
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(body);
}

function send204(res) {
  res.writeHead(204);
  res.end();
}

// handlers: { getState, onNotify(payload), onSpeak({message,title}) }
function createCompanionServer({ port, handlers, dispatch, onLog }) {
  const log = onLog || (() => {});
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const path = url.pathname;
    try {
      if (req.method === "GET" && path === "/state") {
        sendJson(res, 200, handlers.getState());
        return;
      }
      if (req.method === "GET" && path === "/dispatch/pending") {
        sendJson(res, 200, dispatch.pending(url.searchParams.get("exe") || ""));
        return;
      }
      if (req.method === "POST" && (path === "/speak" || path === "/notify" || path === "/dispatch/ack")) {
        let body = {};
        const raw = await readBody(req);
        if (raw.trim()) {
          try { body = JSON.parse(raw); } catch {
            sendJson(res, 400, { error: "invalid JSON" });
            return;
          }
        }
        if (path === "/speak") {
          handlers.onSpeak({ message: String(body.message || ""), title: String(body.title || "") });
          send204(res);
          return;
        }
        if (path === "/notify") {
          handlers.onNotify(body || {});
          send204(res);
          return;
        }
        // /dispatch/ack
        dispatch.ack(body.dispatch_id);
        send204(res);
        return;
      }
      sendJson(res, 404, { error: "not found" });
    } catch (err) {
      const status = err && err.status ? err.status : 500;
      try { sendJson(res, status, { error: String(err.message || err) }); } catch { /* ignore */ }
    }
  });

  server.on("error", (err) => {
    log("companion server error: " + (err.message || err));
  });

  // 端口被占（例如 AgentRMC 占用 45915）时自动顺延，最多 +10，保证 companion 可用。
  // ready 解析为实际绑定端口（0 表示全部失败）。
  const ready = (async () => {
    for (let p = port; p < port + 10; p++) {
      const ok = await new Promise((resolve) => {
        let settled = false;
        const finish = (val) => { if (!settled) { settled = true; resolve(val); } };
        const onErr = (err) => {
          server.removeListener("error", onErr);
          if (err && err.code === "EADDRINUSE") finish(false);
          else {
            log("companion listen failed on " + p + ": " + (err.message || err));
            finish(false);
          }
        };
        server.once("error", onErr);
        server.listen(p, "127.0.0.1", () => {
          server.removeListener("error", onErr);
          if (settled) return; // 该尝试已被 error 结算，忽略迟到的 listening 回调
          log("companion listening on 127.0.0.1:" + p);
          finish(true);
        });
      });
      if (ok) return p;
    }
    log("companion: no free port in range " + port + ".." + (port + 9));
    return 0;
  })();

  return {
    server,
    ready,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}

module.exports = { createCompanionServer, DispatchQueue, MAX_BODY };