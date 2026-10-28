import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

function isolatedProxyEnvironment() {
  return Object.fromEntries(Object.entries(process.env).filter(([name]) =>
    !/^(?:https?|all|no)_proxy$/i.test(name)
    && !/^global_agent_/i.test(name)
    && !/^roarr_/i.test(name)))
}

test('installer logger serializer retains URI resolution and schema validation', () => {
  const require = createRequire(import.meta.url)
  const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'))
  const getRequire = createRequire(builderRequire.resolve('@electron/get'))
  const agentRequire = createRequire(getRequire.resolve('global-agent'))
  const loggerRequire = createRequire(agentRequire.resolve('roarr'))
  const serializerRequire = createRequire(loggerRequire.resolve('fast-json-stringify'))
  assert.equal(serializerRequire('fast-uri/package.json').version, '3.1.9')
  const uri = serializerRequire('fast-uri')
  const schemaId = 'https://synthetic.invalid/schemas/event.json'
  const reference = `${schemaId}#/definitions/ready`
  assert.equal(uri.resolve(schemaId, '#/definitions/ready'), reference)
  assert.equal(uri.serialize(uri.parse(reference)), reference)
  const stringify = loggerRequire('fast-json-stringify')({
    $id: schemaId,
    type: 'object',
    properties: {
      context: { anyOf: [{ $ref: '#/definitions/ready' }, { $ref: '#/definitions/error' }] },
    },
    definitions: {
      ready: {
        type: 'object', properties: { ready: { type: 'boolean' } }, required: ['ready'],
        additionalProperties: false,
      },
      error: {
        type: 'object', properties: { code: { type: 'string' } }, required: ['code'],
        additionalProperties: false,
      },
    },
  })
  for (const context of [{ ready: true }, { code: 'ECONNRESET' }]) {
    assert.deepEqual(JSON.parse(stringify({ context })), { context })
  }
  assert.throws(() => stringify({ context: { unknown: true } }))
})

test('builder artifact downloads retain proxy logging, routing and checksum verification', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'opensquilla-build-proxy-'))
  const body = Buffer.from('Synthetic verified build artifact')
  const digest = createHash('sha256').update(body).digest('hex')
  const requests = []
  const proxy = createServer((request, response) => {
    requests.push(request.url)
    response.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': body.length })
    response.end(body)
  })
  try {
    await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve))
    const script = `
      const assert = require('node:assert/strict');
      const { readFile } = require('node:fs/promises');
      const { createRequire } = require('node:module');
      const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
      const downloader = builderRequire('@electron/get');
      const proxyLogs = [];
      globalThis.ROARR.write = line => proxyLogs.push(JSON.parse(line));
      const destination = process.env.OPENSQUILLA_SYNTHETIC_DOWNLOAD_CACHE;
      const options = {
        version: '9.9.9', artifactName: 'fixture.txt', isGeneric: true,
        cacheRoot: destination, force: true,
        mirrorOptions: { resolveAssetURL: async () => 'http://artifact.invalid/fixture.txt' },
      };
      (async () => {
        const downloaded = await downloader.downloadArtifact({ ...options,
          checksums: { 'fixture.txt': process.env.OPENSQUILLA_SYNTHETIC_DOWNLOAD_DIGEST } });
        assert.equal(await readFile(downloaded, 'utf8'), 'Synthetic verified build artifact');
        const cached = await downloader.downloadArtifact({ ...options, force: false,
          checksums: { 'fixture.txt': process.env.OPENSQUILLA_SYNTHETIC_DOWNLOAD_DIGEST } });
        assert.equal(cached, downloaded);
        assert.equal(await readFile(cached, 'utf8'), 'Synthetic verified build artifact');
        await assert.rejects(downloader.downloadArtifact({ ...options,
          checksums: { 'fixture.txt': '0'.repeat(64) } }), /checksum|hash/i);
        const requests = proxyLogs.filter(row => row.message === 'proxying request');
        assert.equal(requests.length, 2);
        assert.ok(requests.every(row => row.context.package === 'global-agent'
          && row.context.namespace === 'Agent' && row.context.logLevel === 10
          && Number.isInteger(row.context.requestId)
          && row.context.destination === 'http://artifact.invalid/fixture.txt'));
        const responses = proxyLogs.filter(row => row.message === 'proxying response');
        assert.equal(responses.length, 2);
        assert.ok(responses.every(row => row.context.statusCode === 200
          && row.context.headers['content-type'] === 'application/octet-stream'));
      })().catch(error => { console.error(error); process.exitCode = 1; });
    `
    const child = spawn(process.execPath, ['-e', script], {
      cwd: new URL('..', import.meta.url),
      env: {
        ...isolatedProxyEnvironment(),
        ELECTRON_GET_USE_PROXY: '1',
        GLOBAL_AGENT_HTTP_PROXY: `http://127.0.0.1:${proxy.address().port}`,
        GLOBAL_AGENT_NO_PROXY: '',
        ROARR_LOG: 'true',
        OPENSQUILLA_SYNTHETIC_DOWNLOAD_CACHE: directory,
        OPENSQUILLA_SYNTHETIC_DOWNLOAD_DIGEST: digest,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let output = ''
    child.stdout.on('data', chunk => { output += chunk })
    child.stderr.on('data', chunk => { output += chunk })
    const watchdog = setTimeout(() => child.kill('SIGKILL'), 10_000)
    try {
      const code = await new Promise((resolve, reject) => {
        child.once('error', reject)
        child.once('exit', status => resolve(status))
      })
      assert.equal(code, 0, output)
      assert.equal(requests.length, 2)
      assert.ok(requests.every(url => url === 'http://artifact.invalid/fixture.txt'))
    } finally {
      clearTimeout(watchdog)
    }
  } finally {
    proxy.closeAllConnections()
    await new Promise(resolve => proxy.close(resolve))
    await rm(directory, { recursive: true, force: true })
  }
})

for (const blankHttpsProxy of [false, true]) {
  test(`HTTPS downloads use the HTTP proxy when HTTPS proxy is ${blankHttpsProxy ? 'blank' : 'unset'}`, async () => {
    let connections = 0
    const tunnels = new Set()
    const proxy = createServer()
    proxy.on('connect', (_request, socket) => {
      connections += 1
      tunnels.add(socket)
      socket.on('close', () => tunnels.delete(socket))
      socket.end('HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n', () => socket.destroy())
    })
    try {
      await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve))
      const child = spawn(process.execPath, ['-e', `
        const assert = require('node:assert/strict');
        const { createRequire } = require('node:module');
        const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
        builderRequire('@electron/get');
        require('node:https').get('https://artifact.invalid/fixture.txt').on('error', error => {
          try { assert.notEqual(error.code, 'ENOTFOUND'); }
          catch (failure) { console.error(failure); process.exitCode = 1; }
        });
      `], {
        cwd: new URL('..', import.meta.url),
        env: {
          ...isolatedProxyEnvironment(),
          ELECTRON_GET_USE_PROXY: '1',
          GLOBAL_AGENT_HTTP_PROXY: `http://127.0.0.1:${proxy.address().port}`,
          ...(blankHttpsProxy ? { GLOBAL_AGENT_HTTPS_PROXY: '' } : {}),
          GLOBAL_AGENT_NO_PROXY: '',
          ROARR_LOG: 'true',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      let output = ''
      child.stdout.on('data', chunk => { output += chunk })
      child.stderr.on('data', chunk => { output += chunk })
      const watchdog = setTimeout(() => child.kill('SIGKILL'), 10_000)
      try {
        const code = await new Promise((resolve, reject) => {
          child.once('error', reject)
          child.once('exit', status => resolve(status))
        })
        assert.equal(code, 0, output)
        assert.equal(connections, 1)
      } finally {
        clearTimeout(watchdog)
      }
    } finally {
      for (const socket of tunnels) socket.destroy()
      proxy.closeAllConnections()
      await new Promise(resolve => proxy.close(resolve))
    }
  })
}
