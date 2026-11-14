// 库级冒烟测试（无 GUI）：companion 契约 +  live 网关 + TTS 音色枚举 + 派发队列。
// 运行：node scripts/smoke-libs.cjs
"use strict";
const path = require("node:path");
const { createCompanionServer, DispatchQueue } = require("../lib/companion");
const { GatewayClient } = require("../lib/gateway");
const tts = require("../lib/tts");

const PORT = 45999;
const results = [];
function check(name, ok, extra) {
  results.push({ name, ok, extra });
  console.log((ok ? "PASS " : "FAIL ") + name + (extra ? " :: " + extra : ""));
}

async function main() {
  const dispatch = new DispatchQueue();
  let notified = null;
  let spoken = null;
  const companion = createCompanionServer({
    port: PORT,
    dispatch,
    handlers: {
      getState: () => ({ pet: "smoke", tts_ready: true }),
      onNotify: (p) => { notified = p; },
      onSpeak: (p) => { spoken = p; },
    },
    onLog: () => {},
  });
  await companion.ready;

  // /state
  const state = await (await fetch(`http://127.0.0.1:${PORT}/state`)).json();
  check("GET /state", state.pet === "smoke", JSON.stringify(state));

  // /speak → 204
  const speakRes = await fetch(`http://127.0.0.1:${PORT}/speak`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: "你好蛋仔" }),
  });
  check("POST /speak 204", speakRes.status === 204 && spoken && spoken.message === "你好蛋仔", "status=" + speakRes.status);

  // /notify → 204
  const notifyRes = await fetch(`http://127.0.0.1:${PORT}/notify`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event: "permission", title: "T", message: "M", session_id: "s1", event_id: "e1" }),
  });
  check("POST /notify 204", notifyRes.status === 204 && notified && notified.event === "permission", "status=" + notifyRes.status);

  // dispatch enqueue → pending → ack
  const id = dispatch.enqueue({ session_id: "s1", text: "回复内容", exe: "OpenSquilla.exe" });
  const pending = await (await fetch(`http://127.0.0.1:${PORT}/dispatch/pending?exe=OpenSquilla.exe`)).json();
  check("dispatch pending", Array.isArray(pending) && pending.some((x) => x.dispatch_id === id && x.text === "回复内容"), JSON.stringify(pending));
  const ackRes = await fetch(`http://127.0.0.1:${PORT}/dispatch/ack`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dispatch_id: id, ok: true }),
  });
  const pending2 = await (await fetch(`http://127.0.0.1:${PORT}/dispatch/pending?exe=OpenSquilla.exe`)).json();
  check("dispatch ack removes", ackRes.status === 204 && !pending2.some((x) => x.dispatch_id === id), "status=" + ackRes.status);

  // 64KB cap → 413
  const big = await fetch(`http://127.0.0.1:${PORT}/notify`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: "x".repeat(70000) }),
  });
  check("body cap 413", big.status === 413, "status=" + big.status);

  await companion.close();

  // live gateway
  const gw = new GatewayClient({ url: "ws://127.0.0.1:18791/ws" });
  const ready = new Promise((resolve) => gw.once("ready", resolve));
  gw.start();
  const timed = await Promise.race([ready.then(() => true), new Promise((r) => setTimeout(() => r(false), 8000))]);
  check("gateway handshake", timed === true);
  if (timed) {
    const sessions = await gw.listSessions();
    check("sessions.list", Array.isArray(sessions) && sessions.length > 0, "count=" + sessions.length);
    const approvals = await gw.getApprovals();
    check("exec.approvals.get", approvals && typeof approvals.mode === "string", "mode=" + approvals.mode);
    gw.stop();
  }

  // TTS windows voices
  try {
    const voices = await tts.listWindowsVoices();
    check("windows voices", Array.isArray(voices) && voices.length > 0, voices.slice(0, 4).join(" | "));
  } catch (err) {
    check("windows voices", false, String(err.message || err));
  }

  const failed = results.filter((r) => !r.ok);
  console.log("\nSUMMARY: " + (results.length - failed.length) + "/" + results.length + " passed");
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => { console.error("SMOKE ERROR", err); process.exit(2); });