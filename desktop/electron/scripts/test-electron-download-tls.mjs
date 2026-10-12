import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, KeyObject, webcrypto } from 'node:crypto';
import { EventEmitter, once } from 'node:events';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createServer as createHttpsServer } from 'node:https';
import { createRequire } from 'node:module';
import { connect } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

// Resolve certificate APIs from the same locked build dependency as the downloader.
const require = createRequire(import.meta.url);
const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
const getRequire = createRequire(builderRequire.resolve('@electron/get'));
const patch = require('./prepare-global-agent-tls.cjs');
const agentRoot = dirname(getRequire.resolve('global-agent/package.json'));
const agentPath = ['dist', 'classes', 'Agent.js'];
const hash = (value) => createHash('sha256').update(value).digest('hex');
let lockedAgentSource = await readFile(join(agentRoot, ...agentPath));
if (hash(lockedAgentSource) === patch.PATCHED_SHA256) {
  // Build preparation may already have applied the exact patch. Recover the
  // reviewed source only in memory to create isolated dependency fixtures.
  lockedAgentSource = Buffer.from(lockedAgentSource.toString()
    .replace("if (configuration.secureEndpoint || this.protocol === 'https:') {",
      'if (configuration.secureEndpoint) {')
    .replace(patch.TLS_FORWARDING, 'ca: (_g = configuration.ca)'));
}
assert.equal(hash(lockedAgentSource), patch.ORIGINAL_SHA256);

async function dependencyFixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'opensquilla-proxy-tls-patch-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const target = join(root, ...agentPath);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '4.1.3' }));
  await writeFile(target, lockedAgentSource);
  return { root, target };
}

test('HTTPS preparation changes only reviewed dependency bytes and is idempotent', async (t) => {
  const { root, target } = await dependencyFixture(t);
  assert.equal((await patch.prepareProxyTls(root)).changed, true);
  const prepared = await readFile(target);
  assert.equal(hash(prepared), patch.PATCHED_SHA256);
  assert.deepEqual(prepared, patch.patchedSource(lockedAgentSource));
  assert.equal((await patch.prepareProxyTls(root)).changed, false);
  assert.deepEqual(await readFile(target), prepared);
});

test('HTTPS preparation rejects dependency upgrades without modifying source', async (t) => {
  const { root, target } = await dependencyFixture(t);
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '4.1.4' }));
  await assert.rejects(patch.prepareProxyTls(root), /requires global-agent 4\.1\.3/);
  assert.deepEqual(await readFile(target), lockedAgentSource);
});

for (const alreadyPatched of [false, true]) {
  test(`HTTPS preparation rejects modified dependency bytes (alreadyPatched=${alreadyPatched})`, async (t) => {
    const { root, target } = await dependencyFixture(t);
    if (alreadyPatched) await patch.prepareProxyTls(root);
    const modified = Buffer.concat([await readFile(target), Buffer.from('\n// Synthetic drift\n')]);
    await writeFile(target, modified);
    await assert.rejects(patch.prepareProxyTls(root), /Unexpected upstream/);
    assert.deepEqual(await readFile(target), modified);
  });
}

test('HTTPS forwarding retains native TLS controls and excludes HTTP request data', () => {
  const exports = {};
  const load = runInNewContext(`(function(require, exports) {${patch.patchedSource(lockedAgentSource)}\n})`);
  load(createRequire(join(agentRoot, ...agentPath)), exports);
  const agent = new exports.default(() => true, () => true,
    () => ({ hostname: '127.0.0.1', port: 8080, authorization: null }), {}, 1000);
  agent.protocol = 'https:';
  let forwarded;
  agent.createConnection = (configuration) => { forwarded = configuration.tls; };
  const request = Object.assign(new EventEmitter(), { method: 'GET', path: '/synthetic' });
  const controls = {
    ca: 'synthetic CA', rejectUnauthorized: true,
    checkServerIdentity: () => undefined, minVersion: 'TLSv1.2', maxVersion: 'TLSv1.3',
    secureContext: {}, session: Buffer.from('synthetic session'), ALPNProtocols: ['http/1.1'],
    sigalgs: 'rsa_pss_rsae_sha256', minDHSize: 2048,
  };
  agent.addRequest(request, {
    ...controls, host: '127.0.0.2', port: 443,
    headers: { authorization: 'Bearer synthetic-private' }, auth: 'synthetic-auth',
    path: '/synthetic-private', agent: { synthetic: true },
  });
  assert.equal(forwarded.host, '127.0.0.2');
  assert.equal(forwarded.servername, undefined);
  for (const [name, value] of Object.entries(controls)) assert.equal(forwarded[name], value);
  for (const name of ['headers', 'auth', 'path', 'agent']) assert.equal(name in forwarded, false);
});

await patch.prepareInstalledProxyTls();
const pki = builderRequire('pkijs');
const asn1 = builderRequire('asn1js');
const artifact = Buffer.from('synthetic installer TLS download');
const checksum = createHash('sha256').update(artifact).digest('hex');

async function createSyntheticCertificate() {
  const keys = await webcrypto.subtle.generateKey({
    name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256',
  }, true, ['sign', 'verify']);
  const engine = new pki.CryptoEngine({ crypto: webcrypto, subtle: webcrypto.subtle });
  const certificate = new pki.Certificate({
    version: 2,
    serialNumber: new asn1.Integer({ value: 1 }),
    notBefore: new pki.Time({ value: new Date(Date.now() - 60_000) }),
    notAfter: new pki.Time({ value: new Date(Date.now() + 86_400_000) }),
  });
  for (const name of [certificate.subject, certificate.issuer]) {
    name.typesAndValues.push(new pki.AttributeTypeAndValue({
      type: '2.5.4.3', value: new asn1.Utf8String({ value: 'localhost' }),
    }));
  }
  const constraints = new pki.BasicConstraints({ cA: true });
  const names = new pki.AltName({ altNames: [
    new pki.GeneralName({ type: 2, value: 'localhost' }),
    new pki.GeneralName({
      type: 7, value: new asn1.OctetString({ valueHex: new Uint8Array([127, 0, 0, 1]).buffer }),
    }),
  ] });
  certificate.extensions = [
    new pki.Extension({
      extnID: '2.5.29.19', critical: true, extnValue: constraints.toSchema().toBER(false),
    }),
    new pki.Extension({ extnID: '2.5.29.17', extnValue: names.toSchema().toBER(false) }),
  ];
  await certificate.subjectPublicKeyInfo.importKey(keys.publicKey, engine);
  await certificate.sign(keys.privateKey, 'SHA-256', engine);
  const encoded = Buffer.from(certificate.toSchema().toBER(false)).toString('base64');
  return {
    cert: `-----BEGIN CERTIFICATE-----\n${encoded.match(/.{1,64}/g).join('\n')}\n-----END CERTIFICATE-----\n`,
    key: KeyObject.from(keys.privateKey).export({ format: 'pem', type: 'pkcs8' }),
  };
}

for (const { name, host, trust, expectedError, identity, minimumTls } of [
  { name: 'preserves custom CA', host: '127.0.0.1', trust: 'request', expectedError: null },
  { name: 'preserves DNS server name', host: 'localhost', trust: 'request', expectedError: null },
  { name: 'rejects mismatched hosts', host: 'synthetic.invalid', trust: 'extra', expectedError: 'ERR_TLS_CERT_ALTNAME_INVALID' },
  { name: 'rejects mismatched IP addresses', host: '127.0.0.2', trust: 'extra', expectedError: 'ERR_TLS_CERT_ALTNAME_INVALID' },
  { name: 'preserves custom identity rejection', host: '127.0.0.1', trust: 'request', expectedError: 'SYNTHETIC_IDENTITY_REJECTED', identity: true },
  { name: 'preserves minimum TLS version', host: '127.0.0.1', trust: 'request', expectedError: 'EPROTO', minimumTls: true },
  { name: 'rejects untrusted certificates', host: '127.0.0.1', trust: 'none', expectedError: 'DEPTH_ZERO_SELF_SIGNED_CERT' },
]) {
  test(`installer HTTPS proxy ${name}`, {
    timeout: 15_000,
  }, async () => {
    // The private key stays in memory; only this test's child trusts its temporary certificate.
    const { cert, key } = await createSyntheticCertificate();
    let received = 0;
    let tunnels = 0;
    const sockets = new Set();
    const origin = createHttpsServer({ cert, key, ...(minimumTls ? { maxVersion: 'TLSv1.2' } : {}) }, (request, response) => {
      received += 1;
      if (host === 'localhost') assert.equal(request.socket.servername, 'localhost');
      response.end(artifact);
    });
    const proxy = createServer();
    proxy.on('connect', (request, socket, head) => {
      tunnels += 1;
      const upstream = connect(origin.address().port, '127.0.0.1', () => {
        socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        if (head.length) upstream.write(head);
        socket.pipe(upstream);
        upstream.pipe(socket);
      });
      sockets.add(socket);
      sockets.add(upstream);
      upstream.on('error', () => socket.destroy());
      socket.on('error', () => upstream.destroy());
    });
    const cache = await mkdtemp(join(tmpdir(), 'opensquilla-download-tls-'));
    const certPath = join(cache, 'synthetic.cert.pem');
    try {
      await writeFile(certPath, cert);
      origin.listen(0, '127.0.0.1');
      proxy.listen(0, '127.0.0.1');
      await Promise.all([once(origin, 'listening'), once(proxy, 'listening')]);
      const mirror = `https://${host}:${origin.address().port}/electron.zip`;
      const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
      const child = spawn(process.execPath, ['--input-type=module', '-e', `
        import assert from 'node:assert/strict';
        import { readFile } from 'node:fs/promises';
        import { createRequire } from 'node:module';
        const require = createRequire(process.cwd() + '/package.json');
        const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
        builderRequire('@electron/get').initializeProxy();
        assert.equal(globalThis.GLOBAL_AGENT.HTTP_PROXY, ${JSON.stringify(proxyUrl)});
        const { downloadElectronArtifactZip } = builderRequire('./out/util/electronGet.js');
        try {
          const path = await downloadElectronArtifactZip({
            version: '42.0.0', artifactName: 'electron', platformName: 'win32', arch: 'x64',
            cacheDir: ${JSON.stringify(cache)},
            electronDownload: {
              tempDirectory: ${JSON.stringify(cache)},
              checksums: { 'electron-v42.0.0-win32-x64.zip': ${JSON.stringify(checksum)} },
              mirrorOptions: { resolveAssetURL: () => ${JSON.stringify(mirror)} },
              downloadOptions: {
                quiet: true, retry: { limit: 0 }, timeout: { request: 5_000 },
                ...(${JSON.stringify(trust)} === 'request' ? {
                  https: {
                    certificateAuthority: await readFile(${JSON.stringify(certPath)}),
                    ...(${Boolean(identity)} ? { checkServerIdentity: (hostname) => {
                      assert.equal(hostname, ${JSON.stringify(host)});
                      return Object.assign(new Error('Synthetic identity rejection'), {
                        code: 'SYNTHETIC_IDENTITY_REJECTED',
                      });
                    } } : {}),
                  },
                } : {}),
                ...(${Boolean(minimumTls)} ? {
                  minVersion: 'TLSv1.3',
                } : {}),
              },
            },
          });
          assert.equal(${JSON.stringify(expectedError)}, null, 'accepted a rejected TLS request');
          assert.equal((await readFile(path)).toString(), 'synthetic installer TLS download');
        } catch (error) {
          if (${JSON.stringify(expectedError)} === null) throw error;
          assert.equal(error.code, ${JSON.stringify(expectedError)});
        }
      `], {
        cwd: new URL('..', import.meta.url),
        env: {
          ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}),
          ELECTRON_GET_NO_PROGRESS: '1',
          ELECTRON_DOWNLOAD_CACHE_MODE: '3',
          GLOBAL_AGENT_HTTP_PROXY: proxyUrl,
          ...(trust === 'extra' ? { NODE_EXTRA_CA_CERTS: certPath } : {}),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let output = '';
      child.stdout.on('data', (data) => { output += data; });
      child.stderr.on('data', (data) => { output += data; });
      const [code] = await once(child, 'exit');
      assert.equal(code, 0, output);
      assert.equal(tunnels, 1);
      assert.equal(received, expectedError ? 0 : 1);
    } finally {
      for (const socket of sockets) socket.destroy();
      origin.closeAllConnections();
      await Promise.all([new Promise((resolve) => origin.close(resolve)),
        new Promise((resolve) => proxy.close(resolve))]);
      await rm(cache, { recursive: true, force: true });
    }
  });
}
