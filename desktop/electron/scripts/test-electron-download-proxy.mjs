import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

const artifact = Buffer.from('synthetic installer download');
const checksum = createHash('sha256').update(artifact).digest('hex');

for (const bypassProxy of [false, true]) {
  test(`installer download preserves ${bypassProxy ? 'NO_PROXY' : 'HTTP proxy'} routing`, {
    timeout: 15_000,
  }, async () => {
    const requests = [];
    const origin = createServer((request, response) => {
      requests.push({ source: 'origin', url: request.url });
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('Set-Cookie', 'synthetic-session=dummy');
      response.end(artifact);
    });
    const proxy = createServer((request, response) => {
      requests.push({ source: 'proxy', url: request.url });
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('Set-Cookie', 'synthetic-session=dummy');
      response.end(artifact);
    });
    const cache = await mkdtemp(join(tmpdir(), 'opensquilla-download-proxy-'));
    try {
      origin.listen(0, '127.0.0.1');
      proxy.listen(0, '127.0.0.1');
      await Promise.all([once(origin, 'listening'), once(proxy, 'listening')]);
      const mirror = `http://127.0.0.1:${origin.address().port}/electron.zip`;
      const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
      const child = spawn(process.execPath, ['--input-type=module', '-e', `
        import assert from 'node:assert/strict';
        import { readFile, rm } from 'node:fs/promises';
        import { createRequire } from 'node:module';
        import { dirname } from 'node:path';
        const require = createRequire(process.cwd() + '/package.json');
        const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
        const getRequire = createRequire(builderRequire.resolve('@electron/get'));
        const { bootstrap, createGlobalProxyAgent } = getRequire('global-agent');
        assert.equal(typeof bootstrap, 'function');
        assert.equal(typeof createGlobalProxyAgent, 'function');
        const logger = getRequire('roarr').default.child({ fixture: 'installer-download' });
        for (const level of ['trace', 'debug', 'info', 'warn', 'error']) {
          assert.equal(typeof logger[level], 'function');
        }
        logger.info('synthetic %s %.1000000000f', 'download', 1);
        const downloader = builderRequire('@electron/get');
        downloader.initializeProxy();
        assert.equal(globalThis.GLOBAL_AGENT.HTTP_PROXY, ${JSON.stringify(proxyUrl)});
        assert.equal(bootstrap(), false);
        const { downloadElectronArtifactZip } = builderRequire('./out/util/electronGet.js');
        let downloads = 0;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          const path = await downloadElectronArtifactZip({
            version: '42.0.0', artifactName: 'electron', platformName: 'win32', arch: 'x64',
            cacheDir: ${JSON.stringify(cache)},
            electronDownload: {
              tempDirectory: ${JSON.stringify(cache)},
              checksums: { 'electron-v42.0.0-win32-x64.zip': ${JSON.stringify(checksum)} },
              mirrorOptions: { resolveAssetURL: () => ${JSON.stringify(mirror)} },
              downloadOptions: {
                quiet: true, retry: { limit: 0 }, timeout: { request: 5_000 },
                headers: { 'cache-control': 'max-stale=1000' },
                hooks: { beforeRequest: [(options) => {
                  // The installer uses verified artifact caching. HTTP response
                  // caching must remain disabled in the underlying downloader.
                  assert.equal(options.cache, undefined);
                  downloads += 1;
                }] },
              },
            },
          });
          assert.equal((await readFile(path)).toString(), 'synthetic installer download');
          await rm(dirname(path), { recursive: true, force: true });
        }
        assert.equal(downloads, 2);
      `], {
        cwd: new URL('..', import.meta.url),
        env: {
          ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}),
          ELECTRON_GET_NO_PROGRESS: '1',
          ROARR_LOG: 'true',
          ELECTRON_DOWNLOAD_CACHE_MODE: '3',
          GLOBAL_AGENT_HTTP_PROXY: proxyUrl,
          GLOBAL_AGENT_NO_PROXY: bypassProxy ? '127.0.0.1' : '',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let output = '';
      child.stdout.on('data', (data) => { output += data; });
      child.stderr.on('data', (data) => { output += data; });
      const [code] = await once(child, 'exit');
      assert.equal(code, 0, output);
      const expectedRequest = {
        source: bypassProxy ? 'origin' : 'proxy',
        url: bypassProxy ? '/electron.zip' : mirror,
      };
      assert.deepEqual(requests, [expectedRequest, expectedRequest]);
    } finally {
      origin.closeAllConnections();
      proxy.closeAllConnections();
      await Promise.all([new Promise((resolve) => origin.close(resolve)),
        new Promise((resolve) => proxy.close(resolve))]);
      await rm(cache, { recursive: true, force: true });
    }
  });
}
