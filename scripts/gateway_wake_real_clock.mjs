/*
 * Real-clock wake incident harness.
 *
 * This is deliberately independent from the product test suite: it loads the
 * production RpcClient source into a real Chromium page, connects through a
 * local WebSocket relay, and black-holes only the first connection. A fresh
 * connection stays healthy so the test represents a remote/proxy topology,
 * rather than a loopback Desktop Gateway.
 *
 * Evidence boundary: this is a controlled loopback relay with real timers and
 * real WebSocket framing. It does not claim to reproduce physical Windows
 * sleep/resume, Wi-Fi changes, VPN transitions, or packaged Electron behavior.
 */
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import crypto from 'node:crypto';
import path from 'node:path';
import os from 'node:os';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

import playwrightPackage from '../opensquilla-webui/node_modules/playwright/index.js';
import ts from '../opensquilla-webui/node_modules/@typescript/typescript6/lib/typescript.js';
import wsPackage from '../opensquilla-webui/node_modules/ws/index.js';

const WebSocket = wsPackage;
const { WebSocketServer } = wsPackage;
const { chromium } = playwrightPackage;

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const sourcePath = path.resolve(process.env.OSQ_CURRENT_SOURCE || path.join(repo, 'opensquilla-webui/src/lib/rpc.ts'));
const historicalBaselineSha = '6750223bf72b418a257a2276c18e6edec07ef14f';
const baselineRef = process.env.OSQ_WAKE_BASELINE_SHA?.trim() || historicalBaselineSha;
const baselineSha = execFileSync('git', ['rev-parse', '--verify', '--end-of-options', `${baselineRef}^{commit}`], {
  cwd: repo, encoding: 'utf8',
}).trim();
if (!/^[0-9a-f]{40}$/.test(baselineSha)) throw new Error(`Invalid resolved baseline commit: ${baselineSha}`);
const baselineName = baselineSha === historicalBaselineSha ? 'baseline675' : `baseline${baselineSha.slice(0, 8)}`;
const outputDir = process.env.OSQ_WAKE_EVIDENCE_DIR
  ? path.resolve(process.env.OSQ_WAKE_EVIDENCE_DIR)
  : fs.mkdtempSync(path.join(os.tmpdir(), 'opensquilla-gateway-wake-'));
const loops = Math.max(1, Number.parseInt(process.env.OSQ_WAKE_LOOPS || '30', 10));
const concurrency = Math.max(1, Number.parseInt(process.env.OSQ_WAKE_CONCURRENCY || '30', 10));
const vueProxy = process.env.OSQ_WAKE_CLIENT_MODE === 'vue';
const variantFilter = process.env.OSQ_WAKE_VARIANTS?.split(',');
const scenarioFilter = process.env.OSQ_WAKE_SCENARIOS?.split(',');
if (fs.existsSync(outputDir) && (!fs.statSync(outputDir).isDirectory() || fs.readdirSync(outputDir).length)) {
  throw new Error(`Evidence output directory must be empty: ${outputDir}`);
}
fs.mkdirSync(outputDir, { recursive: true });

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function sourceCode(source) {
  return ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText;
}

function helloFrame(connId) {
  return {
    type: 'hello-ok', protocol: 3,
    server: { version: 'real-clock-harness', conn_id: connId },
    features: { methods: ['audit.echo'], events: [] },
    snapshot: {}, policy: { tick_interval_ms: 30000, transport_probe_nonce: true }, auth: null,
  };
}

function runGateway(server, id) {
  const wss = new WebSocketServer({ noServer: true });
  server.on('upgrade', (request, socket, head) => {
    if (request.url !== '/ws') { socket.destroy(); return; }
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit('connection', ws, request));
  });
  let sequence = 0;
  wss.on('connection', (ws) => {
    const connId = `${id}-upstream-${++sequence}`;
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge' }));
    ws.on('message', (raw) => {
      let frame;
      try { frame = JSON.parse(String(raw)); } catch { return; }
      if (frame.type === 'req' && frame.method === 'connect') {
        // The production handshake is a challenge followed by a direct
        // hello-ok frame; only ordinary RPCs use the res envelope.
        ws.send(JSON.stringify(helloFrame(connId)));
      } else if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong', ...(frame.nonce ? { nonce: frame.nonce } : {}) }));
      } else if (frame.type === 'req' && frame.method === 'audit.echo') {
        ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
          payload: { echo: frame.params?.value ?? null, conn_id: connId } }));
      }
    });
  });
  return { wss, close: () => new Promise((resolve) => wss.close(resolve)) };
}

async function createRelay(upstreamPort) {
  const connections = [];
  const timeline = [];
  const server = net.createServer((front) => {
    const upstream = net.connect({ port: upstreamPort, host: '127.0.0.1' });
    const state = { front, upstream, fault: false, buffer: false, buffered: [], droppedUp: 0, droppedDown: 0 };
    const index = connections.length;
    connections.push(state);
    timeline.push({ event: 'tcp_created', index, at: performance.now() });
    const forward = (target, data) => {
      if (!target.destroyed) target.write(data);
    };
    for (const socket of [front, upstream]) { socket.setNoDelay(true); socket.on('error', () => {}); }
    front.on('data', (data) => {
      if (state.fault) {
        if (state.buffer) state.buffered.push({ target: upstream, data });
        else state.droppedUp += data.length;
      } else forward(upstream, data);
    });
    upstream.on('data', (data) => {
      if (state.fault) {
        if (state.buffer) state.buffered.push({ target: front, data });
        else state.droppedDown += data.length;
      } else forward(front, data);
    });
    front.on('close', () => { upstream.destroy(); timeline.push({ event: 'client_closed', index, at: performance.now() }); });
    upstream.on('end', () => { if (!state.fault) front.end(); });
    upstream.on('close', () => { if (!state.fault) front.destroy(); });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  return {
    url: `ws://127.0.0.1:${port}/ws`, connections, timeline,
    fault(index = 0, buffer = false) {
      const state = connections[index];
      if (!state) throw new Error(`relay connection ${index} not available`);
      state.fault = true; state.buffer = buffer;
      timeline.push({ event: 'fault', index, buffer, at: performance.now() });
    },
    heal(index = 0) {
      const state = connections[index];
      if (!state) return;
      state.fault = false;
      for (const item of state.buffered.splice(0)) if (!item.target.destroyed) item.target.write(item.data);
      timeline.push({ event: 'heal', index, at: performance.now() });
    },
    close() {
      for (const state of connections) { state.front.destroy(); state.upstream.destroy(); }
      return new Promise((resolve) => server.close(() => resolve()));
    },
  };
}

async function runTrial(browser, variant, scenario, repetition) {
  const upstream = http.createServer();
  const gateway = runGateway(upstream, variant.name);
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const relay = await createRelay(upstream.address().port);
  const context = await browser.newContext();
  const page = await context.newPage();
  let healingTimer;
  try {
    await page.setContent('<!doctype html><title>gateway wake real clock</title>');
    if (vueProxy) await page.addScriptTag({ path: path.join(repo, 'opensquilla-webui/node_modules/vue/dist/vue.global.prod.js') });
    await page.addScriptTag({ content: `var exports = {};\n${variant.code}\nwindow.AuditRpcClient = exports.RpcClient;` });
    await page.evaluate(({ url, vueProxy }) => {
      const base = performance.now();
      const events = [];
      const record = (event, detail = {}) => events.push({ ...detail,
        ...(detail.at === undefined ? {} : { sourceEpochAt: detail.at }),
        event, at: performance.now() - base });
      const nativeClose = WebSocket.prototype.close;
      WebSocket.prototype.close = function(...args) {
        record('socket_close_called', { code: args[0] ?? null, reason: args[1] ?? null });
        return Reflect.apply(nativeClose, this, args);
      };
      const rawRpc = new window.AuditRpcClient();
      const rpc = vueProxy ? window.Vue.ref(rawRpc).value : rawRpc;
      window.audit = { rpc, events, base, hello: 0, usable: 0, ready: false, vueProxy };
      rpc.on('_transport', (detail) => record('transport', detail));
      rpc.on('_state', (state) => record('state', { state }));
      rpc.on('_status', (status) => record('status', status));
      rpc.on('_hello', () => {
        window.audit.hello += 1; record('hello', { count: window.audit.hello });
        setTimeout(async () => {
          try {
            const reply = await rpc.call('audit.echo', { value: `usable-${window.audit.hello}` }, { timeoutMs: 5000 });
            window.audit.usable += 1; record('usable_echo', { connId: reply.conn_id }); window.audit.ready = true;
          } catch (error) { record('echo_error', { message: error.message }); }
        }, 0);
      });
      rpc.connect(url);
    }, { url: relay.url, vueProxy });
    const connected = await page.waitForFunction(() => window.audit.ready, null, { timeout: 10000 }).then(() => true).catch(() => false);
    if (!connected) {
      const diagnostic = await page.evaluate(() => ({ state: window.audit.rpc.state, health: window.audit.rpc.health, events: window.audit.events }));
      throw new Error(`initial connection did not become usable: ${JSON.stringify(diagnostic)}`);
    }
    await page.evaluate(() => { window.audit.ready = false; });
    relay.fault(0, scenario.buffer);
    await page.evaluate((repeatMs) => {
      window.audit.faultAt = performance.now() - window.audit.base;
      window.audit.events.push({ event: 'fault_injected', at: window.audit.faultAt });
      if (window.audit.vueProxy) window.audit.rpc.notifyResume();
      else window.dispatchEvent(new Event('pageshow'));
      if (repeatMs) window.audit.repeatTimer = setInterval(() => {
        window.audit.events.push({ event: 'wake_signal', at: performance.now() - window.audit.base });
        if (window.audit.vueProxy) window.audit.rpc.notifyResume();
        else window.dispatchEvent(new Event('pageshow'));
      }, repeatMs);
    }, scenario.repeatMs);
    if (scenario.healMs !== null) healingTimer = setTimeout(async () => {
      relay.heal(0);
      // A delayed pong proves transport recovery; follow it with a real RPC so
      // the result also proves that the connection remains application-usable.
      await page.evaluate(async () => {
        // Yield once so the released, nonce-matching pong can be processed
        // before checking whether business calls are permitted.
        await new Promise((resolve) => setTimeout(resolve, 50));
        try {
          const reply = await window.audit.rpc.call('audit.echo', { value: 'post-heal' }, { timeoutMs: 5000 });
          window.audit.usable += 1;
          window.audit.events.push({ event: 'usable_echo', at: performance.now() - window.audit.base, connId: reply.conn_id });
        } catch (error) {
          window.audit.events.push({ event: 'echo_error', at: performance.now() - window.audit.base, message: error.message });
        }
      }).catch(() => {});
    }, scenario.healMs);
    const browserTimeoutMs = variant.name === baselineName ? (scenario.repeatMs ? 90000 : 55000) : Math.max(variant.budgetMs + 8000, 18000);
    const recovered = await page.waitForFunction(() => window.audit.usable >= 2, null, { timeout: browserTimeoutMs, polling: 100 }).then(() => true).catch(() => false);
    const snapshot = await page.evaluate(() => ({
      events: window.audit.events, state: window.audit.rpc.state, health: window.audit.rpc.health,
      hello: window.audit.hello, usable: window.audit.usable,
    }));
    const faultAt = snapshot.events.find((event) => event.event === 'fault_injected')?.at ?? 0;
    const after = snapshot.events.filter((event) => event.at >= faultAt);
    const transport = after.filter((event) => event.event === 'transport');
    const firstPhase = (phase) => transport.find((event) => event.phase === phase);
    const close = after.find((event) => event.event === 'socket_close_called');
    const suspect = firstPhase('probe_timeout');
    const reconnect = firstPhase('connect_start');
    const hello = after.find((event) => event.event === 'hello' && event.count >= 2);
    const usable = after.find((event) => event.event === 'usable_echo');
    const retire = firstPhase('retire');
    const incident = firstPhase('wake_incident_start');
    const recoveredIncident = firstPhase('wake_incident_recovered');
    return {
      variant: variant.name, scenario: scenario.name, repetition, recovered, observationLimitMs: browserTimeoutMs,
      outcome: recovered ? (snapshot.hello === 1 ? 'original_connection_usable' : 'replacement_connection_usable') : 'no_recovery_before_observation_limit',
      retirementReason: retire?.reason ?? null,
      faultToSuspectMs: suspect ? +(suspect.at - faultAt).toFixed(1) : null,
      faultToCloseMs: close ? +(close.at - faultAt).toFixed(1) : null,
      faultToReconnectMs: reconnect ? +(reconnect.at - faultAt).toFixed(1) : null,
      faultToReplacementHelloMs: hello ? +(hello.at - faultAt).toFixed(1) : null,
      faultToUsableMs: usable ? +(usable.at - faultAt).toFixed(1) : null,
      faultToIncidentRecoveryMs: recoveredIncident ? +(recoveredIncident.at - faultAt).toFixed(1) : null,
      incidentDeadlineMs: incident ? incident.deadlineAt - incident.wakeIncidentStartedAt : null,
      finalState: snapshot.state, finalHealth: snapshot.health, helloCount: snapshot.hello,
      relayConnections: relay.connections.length,
      droppedBytes: relay.connections.map((c) => ({ up: c.droppedUp, down: c.droppedDown })),
      relay: relay.timeline,
      events: snapshot.events,
    };
  } finally {
    if (healingTimer) clearTimeout(healingTimer);
    await context.close(); await relay.close(); await gateway.close();
    await new Promise((resolve) => upstream.close(resolve));
  }
}

function summarize(results) {
  const fields = ['faultToSuspectMs', 'faultToCloseMs', 'faultToReconnectMs', 'faultToReplacementHelloMs', 'faultToUsableMs'];
  const summary = {};
  for (const field of fields) {
    const values = results.map((r) => r[field]).filter((value) => typeof value === 'number').sort((a, b) => a - b);
    summary[field] = values.length ? {
      count: values.length, p50: values[Math.floor(values.length * 0.5)],
      p95: values[Math.min(values.length - 1, Math.ceil(values.length * 0.95) - 1)],
      max: values.at(-1), min: values[0], range: [values[0], values.at(-1)],
    } : null;
  }
  return summary;
}

async function main() {
  const harnessPath = fileURLToPath(import.meta.url);
  const harnessSha = sha256(harnessPath);
  const packageLockSha = sha256(path.join(repo, 'opensquilla-webui/package-lock.json'));
  const currentSource = fs.readFileSync(sourcePath, 'utf8');
  const baselineSource = execFileSync('git', ['show', `${baselineSha}:opensquilla-webui/src/lib/rpc.ts`], { cwd: repo, encoding: 'utf8' });
  const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim();
  const status = execFileSync('git', ['status', '--short'], { cwd: repo, encoding: 'utf8' });
  const variants = [
    { name: baselineName, source: baselineSource, budgetMs: null },
    ...[10000, 15000, 20000, 30000].map((budgetMs) => {
      const source = currentSource.replace(/const WAKE_INCIDENT_BUDGET_MS = [\d_]+;/, `const WAKE_INCIDENT_BUDGET_MS = ${budgetMs};`);
      if (source === currentSource && budgetMs !== 20000) throw new Error('candidate incident constant not found');
      return { name: `candidate${budgetMs / 1000}`, source, budgetMs };
    }),
  ].filter((variant) => !variantFilter || variantFilter.includes(variant.name)).map((variant) => {
    const snapshot = path.join(outputDir, `source-${variant.name}.ts`);
    fs.writeFileSync(snapshot, variant.source);
    return { ...variant, path: path.relative(outputDir, snapshot), code: sourceCode(variant.source), sha256: sha256(snapshot) };
  });
  const browser = await chromium.launch({ headless: true });
  const browserVersion = browser.version();
  const scenarios = [
    { name: 'single-wake-blackhole', repeatMs: null, healMs: null, buffer: false },
    { name: 'repeat3-blackhole', repeatMs: 3000, healMs: null, buffer: false },
    { name: 'repeat14-blackhole', repeatMs: 14000, healMs: null, buffer: false },
    { name: 'repeat40-blackhole', repeatMs: 40000, healMs: null, buffer: false },
    { name: 'buffer13-recovery', repeatMs: null, healMs: 13000, buffer: true },
  ].filter((scenario) => !scenarioFilter || scenarioFilter.includes(scenario.name));
  const work = variants.flatMap((variant) => scenarios.filter((scenario) =>
    variant.name === baselineName || variant.name === 'candidate20' || scenario.repeatMs === null
  ).flatMap((scenario) => Array.from({ length: loops }, (_, index) => ({ variant, scenario, repetition: index + 1 }))));
  const total = work.length;
  const results = [];
  const failures = [];
  const startedAt = new Date().toISOString();
  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, total) }, async () => {
      while (work.length) {
        const { variant, scenario, repetition } = work.shift();
        try {
          const result = await runTrial(browser, variant, scenario, repetition);
          results.push(result);
          fs.writeFileSync(path.join(outputDir, `${variant.name}-${scenario.name}-${repetition}.json`), JSON.stringify(result, null, 2));
          process.stdout.write(JSON.stringify({ completed: results.length + failures.length, total,
            variant: variant.name, scenario: scenario.name, repetition, outcome: result.outcome,
            usableMs: result.faultToUsableMs }) + '\n');
        } catch (error) {
          const failure = { variant: variant.name, scenario: scenario.name, repetition, error: error.stack || String(error) };
          failures.push(failure);
          process.stdout.write(JSON.stringify({ failure }) + '\n');
        }
      }
    }));
  } finally { await browser.close(); }
  const groups = {};
  for (const variant of variants) for (const scenario of scenarios) {
    const selected = results.filter((result) => result.variant === variant.name && result.scenario === scenario.name);
    if (!selected.length) continue;
    groups[`${variant.name}/${scenario.name}`] = {
      count: selected.length, recovered: selected.filter((result) => result.recovered).length,
      originalConnectionPreserved: selected.filter((result) => result.recovered && result.helloCount === 1).length,
      censoredAtObservationLimit: selected.filter((result) => !result.recovered).length,
      timing: summarize(selected),
    };
  }
  const evidence = {
    schema: 'opensquilla.gateway.wake-real-clock.v1', startedAt, generatedAt: new Date().toISOString(),
    gitHead: head, baselineSha, gitStatus: status,
    harness: { file: path.relative(repo, harnessPath), sha256: harnessSha,
      unchangedAtEnd: harnessSha === sha256(harnessPath),
      node: process.version, chromium: browserVersion, typescript: ts.version, platform: process.platform,
      loops, concurrency, clientMode: vueProxy ? 'vue-ref-resume' : 'raw-pageshow',
      vueBundleSha256: vueProxy ? sha256(path.join(repo, 'opensquilla-webui/node_modules/vue/dist/vue.global.prod.js')) : null,
      packageLockSha256: packageLockSha },
    topology: 'loopback real Chromium/native WebSocket + TCP byte relay + synthetic contract upstream; old connection black-holed, replacement healthy',
    physicalCoverage: { windowsSleepResume: false, wifiChange: false, vpnOrRemoteNic: false, packagedElectron: false },
    scopeLimitations: [
      'Parallel trials share one host; sample spread is fixture timing, not a population latency estimate.',
      'Synthetic handler performs handshake, nonce pong, echo only; no production Python writer, flow control, Goal, hydration or exactly-once mutation acceptance.',
      'buffer13 is a scripted TCP pause, not a measured natural recovery distribution or transport.flow.v1 test.',
      'Baseline periodic cases are right-censored at90s; no finite run proves mathematical infinity.',
      'Headless Chromium foreground timers do not test Electron background throttling.',
    ],
    variants: variants.map(({ name, path: source, sha256: sourceSha }) => ({ name, source, sha256: sourceSha })),
    scenarios, summary: groups, results, failures,
  };
  const output = path.join(outputDir, 'results.json');
  fs.writeFileSync(output, JSON.stringify(evidence, null, 2));
  process.stdout.write(`${JSON.stringify({ output, count: results.length, failures: failures.length })}\n`);
  if (failures.length) process.exitCode = 1;
}

main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
