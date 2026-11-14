// Read-only live probe of the OpenSquilla gateway WS protocol.
// Usage: node scripts/probe-gateway.mjs [wsUrl] [token]
// Confirms handshake, sessions.list, exec.approvals.get, sessions.subscribe,
// sessions.messages.subscribe and prints the event frame shape.
const url = process.argv[2] || "ws://127.0.0.1:18791/ws";
const token = process.argv[3] || "";

if (typeof WebSocket === "undefined") {
  console.error("NO_GLOBAL_WEBSOCKET");
  process.exit(2);
}

const ws = new WebSocket(url);
let nextId = 1;
const pending = new Map();
const events = [];

function call(method, params) {
  const id = String(nextId++);
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ type: "req", id, method, params }));
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(method + " timeout"));
      }
    }, 8000);
  });
}

const timer = setTimeout(() => {
  console.log("EVENTS_CAPTURED", events.length);
  for (const e of events.slice(0, 3)) console.log("EVENT_FRAME", JSON.stringify(e).slice(0, 600));
  ws.close();
  process.exit(0);
}, 6000);

ws.addEventListener("message", async (ev) => {
  let frame;
  try { frame = JSON.parse(ev.data); } catch { return; }
  if (frame.type === "event" && frame.event === "connect.challenge") {
    const params = {
      minProtocol: 1, maxProtocol: 3, role: "operator",
      scopes: ["operator.read", "operator.write"],
      client: { id: "opensquilla-pet-probe", version: "0.0.0", platform: process.platform, mode: "pet" },
    };
    if (token) params.auth = { token };
    ws.send(JSON.stringify({ type: "req", id: "connect-1", method: "connect", params }));
    return;
  }
  if (frame.type === "hello-ok") {
    console.log("HELLO_OK policy=", JSON.stringify(frame.policy || {}).slice(0, 300));
    try {
      const sessions = await call("sessions.list", { limit: 8 });
      const list = sessions.sessions || [];
      console.log("SESSIONS_COUNT", list.length);
      console.log("SESSION_SAMPLE", JSON.stringify(list[0] || {}).slice(0, 500));
      const approvals = await call("exec.approvals.get", {});
      console.log("APPROVALS", JSON.stringify(approvals).slice(0, 400));
      const sub = await call("sessions.subscribe", {});
      console.log("SESSIONS_SUBSCRIBE", JSON.stringify(sub).slice(0, 200));
      if (list[0]) {
        const msub = await call("sessions.messages.subscribe", { key: list[0].key });
        console.log("MESSAGES_SUBSCRIBE", JSON.stringify(msub).slice(0, 300));
      }
    } catch (err) {
      console.log("RPC_ERROR", String(err && err.message || err));
    }
    return;
  }
  if (frame.type === "res") {
    const p = pending.get(frame.id);
    if (p) {
      pending.delete(frame.id);
      if (frame.ok) p.resolve(frame.payload || {});
      else p.reject(new Error((frame.error && frame.error.message) || "rpc failed"));
    }
    return;
  }
  if (frame.type === "event") {
    events.push(frame);
    return;
  }
});

ws.addEventListener("error", (e) => { console.log("WS_ERROR", String(e.message || e)); });
ws.addEventListener("close", () => { clearTimeout(timer); });