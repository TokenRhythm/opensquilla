import assert from "node:assert/strict";
import test from "node:test";
import net from "node:net";

import { HOST_PROTOCOL_VERSION, connectIpc, createIpc } from "./ipc.mjs";

async function socketPair() {
  let resolveServer;
  const accepted = new Promise((resolve) => { resolveServer = resolve; });
  const server = net.createServer((socket) => resolveServer(socket));
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  const client = net.createConnection({ host: "127.0.0.1", port });
  const peer = await accepted;
  server.close();
  return { client, peer };
}

async function readLines(socket, count) {
  let data = "";
  return await new Promise((resolve, reject) => {
    const onData = (chunk) => {
      data += chunk.toString("utf8");
      const lines = data.split("\n").filter(Boolean);
      if (lines.length >= count) {
        socket.off("data", onData);
        resolve(lines.slice(0, count));
      }
    };
    socket.on("data", onData);
    socket.once("error", reject);
  });
}

test("send() writes complete JSON lines in order", async () => {
  const { client, peer } = await socketPair();
  const { send } = createIpc({ socket: client });
  const received = readLines(peer, 2);
  send({ type: "ready" });
  send({ type: "resize", width: 80, height: 24 });
  assert.deepEqual(await received, [
    '{"type":"ready"}',
    '{"type":"resize","width":80,"height":24}',
  ]);
  client.destroy();
  peer.destroy();
});

test("send() never throws for a closed socket or circular payload", async () => {
  const { client, peer } = await socketPair();
  const { send } = createIpc({ socket: client });
  client.destroy();
  const circular = {};
  circular.self = circular;
  assert.doesNotThrow(() => send({ type: "resize", width: 80, height: 24 }));
  assert.doesNotThrow(() => send({ type: "error", circular }));
  peer.destroy();
});

test("start() reports malformed input and keeps dispatching", async () => {
  const { client, peer } = await socketPair();
  const ipc = createIpc({ socket: client });
  const seen = [];
  const reported = readLines(peer, 1);
  const closed = new Promise((resolve) => ipc.start((message) => seen.push(message), resolve));
  peer.write('not json\n{"type":"ready"}\n');
  const [line] = await reported;
  assert.equal(JSON.parse(line).type, "error");
  assert.deepEqual(seen, [{ type: "ready" }]);
  peer.destroy();
  await closed;
});

test("connectIpc authenticates before product frames", async () => {
  const token = "test-secret";
  const server = net.createServer((socket) => {
    let data = "";
    socket.on("data", (chunk) => {
      data += chunk.toString("utf8");
      const index = data.indexOf("\n");
      if (index < 0) return;
      const auth = JSON.parse(data.slice(0, index));
      assert.deepEqual(auth, { type: "auth", token, protocol: HOST_PROTOCOL_VERSION });
      socket.write(`{"type":"auth.ok","protocol":${HOST_PROTOCOL_VERSION}}\n`);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const ipc = await connectIpc({
    host: "127.0.0.1",
    port: server.address().port,
    token,
  });
  assert.equal(typeof ipc.send, "function");
  server.close();
});

test("connectIpc retains bootstrap frames received before start installs its dispatcher", async () => {
  const token = "bootstrap-race-secret";
  let peer = null;
  let resolveBootstrapSent;
  const bootstrapSent = new Promise((resolve) => { resolveBootstrapSent = resolve; });
  const server = net.createServer((socket) => {
    peer = socket;
    let data = "";
    socket.on("data", (chunk) => {
      data += chunk.toString("utf8");
      const lines = data.split("\n");
      data = lines.pop() ?? "";
      for (const line of lines.filter(Boolean)) {
        const message = JSON.parse(line);
        if (message.type === "auth") {
          socket.write(`{"type":"auth.ok","protocol":${HOST_PROTOCOL_VERSION}}\n`);
        } else if (message.type === "ready") {
          // Python begins canonical bootstrap as soon as it sees HostReady,
          // while main.mjs has not quite called ipc.start(). These complete
          // lines must stay buffered across that authentication/start gap.
          socket.write(
            '{"type":"router.update","model":"bootstrap-model"}\n'
            + '{"type":"context.update","agent":{"name":"Mira"}}\n',
          );
          resolveBootstrapSent();
        }
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

  const ipc = await connectIpc({
    host: "127.0.0.1",
    port: server.address().port,
    token,
  });
  ipc.send({ type: "ready" });
  await bootstrapSent;
  // Give readline a chance to expose the exact pre-start loss seen in the
  // repeated real-terminal launch gate.
  await new Promise((resolve) => setTimeout(resolve, 20));

  const seen = [];
  const delivered = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("bootstrap frames were dropped")), 500);
    ipc.start((message) => {
      seen.push(message);
      if (seen.length === 2) {
        clearTimeout(timer);
        resolve();
      }
    }, () => {});
  });
  await delivered;
  assert.deepEqual(seen, [
    { type: "router.update", model: "bootstrap-model" },
    { type: "context.update", agent: { name: "Mira" } },
  ]);

  peer?.destroy();
  server.close();
});

test("connectIpc rejects a failed authentication response", async () => {
  const server = net.createServer((socket) => {
    socket.once("data", () => socket.write('{"type":"auth.error","message":"denied"}\n'));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await assert.rejects(
    connectIpc({ host: "127.0.0.1", port: server.address().port, token: "wrong" }),
    /denied/,
  );
  server.close();
});
