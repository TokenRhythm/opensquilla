import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, KeyObject, webcrypto } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createServer as createHttpsServer } from 'node:https';
import { createRequire } from 'node:module';
import { connect } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

// Resolve certificate APIs from the same locked build dependency as the downloader.
const require = createRequire(import.meta.url);
const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
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

for (const wrongHostname of [false, true]) {
  test(`installer HTTPS proxy ${wrongHostname ? 'rejects mismatched hosts' : 'preserves custom CA'}`, {
    timeout: 15_000,
  }, async () => {
    // The private key stays in memory; only this test's child trusts its temporary certificate.
    const { cert, key } = await createSyntheticCertificate();
    let received = 0;
    let tunnels = 0;
    const sockets = new Set();
    const origin = createHttpsServer({ cert, key }, (request, response) => {
      received += 1;
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
      const host = wrongHostname ? 'synthetic.invalid' : '127.0.0.1';
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
                ...(${wrongHostname} ? {} : {
                  https: { certificateAuthority: await readFile(${JSON.stringify(certPath)}) },
                }),
              },
            },
          });
          assert.equal(${wrongHostname}, false, 'accepted a certificate for another host');
          assert.equal((await readFile(path)).toString(), 'synthetic installer TLS download');
        } catch (error) {
          if (!${wrongHostname}) throw error;
          assert.equal(error.code, 'ERR_TLS_CERT_ALTNAME_INVALID');
        }
      `], {
        cwd: new URL('..', import.meta.url),
        env: {
          ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}),
          ELECTRON_GET_NO_PROGRESS: '1',
          ELECTRON_DOWNLOAD_CACHE_MODE: '3',
          GLOBAL_AGENT_HTTP_PROXY: proxyUrl,
          ...(wrongHostname ? { NODE_EXTRA_CA_CERTS: certPath } : {}),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let output = '';
      child.stdout.on('data', (data) => { output += data; });
      child.stderr.on('data', (data) => { output += data; });
      const [code] = await once(child, 'exit');
      assert.equal(code, 0, output);
      assert.equal(tunnels, 1);
      assert.equal(received, wrongHostname ? 0 : 1);
    } finally {
      for (const socket of sockets) socket.destroy();
      origin.closeAllConnections();
      await Promise.all([new Promise((resolve) => origin.close(resolve)),
        new Promise((resolve) => proxy.close(resolve))]);
      await rm(cache, { recursive: true, force: true });
    }
  });
}
