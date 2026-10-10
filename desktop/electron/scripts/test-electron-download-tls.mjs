import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createServer as createHttpsServer } from 'node:https';
import { connect } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

// Public synthetic fixtures are trusted only by the isolated test child.
const certPath = fileURLToPath(new URL('./fixtures/download-proxy/synthetic.cert.pem', import.meta.url));
const cert = await readFile(certPath);
const key = await readFile(new URL('./fixtures/download-proxy/synthetic.key.pem', import.meta.url));
const artifact = Buffer.from('synthetic installer TLS download');
const checksum = createHash('sha256').update(artifact).digest('hex');

for (const wrongHostname of [false, true]) {
  test(`installer HTTPS proxy ${wrongHostname ? 'rejects mismatched hosts' : 'preserves custom CA'}`, {
    timeout: 15_000,
  }, async () => {
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
    try {
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
