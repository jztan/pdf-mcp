'use strict';
process.env.PDF_MCP_LAUNCHER_TEST = '1';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const L = require('../../packaging/mcpb/server/launcher.js');

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pdfmcp-launcher-'));
}

function serve(routes) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const route = routes[req.url];
      if (!route) { res.writeHead(404); return res.end(); }
      if (route.redirect) { res.writeHead(302, { location: route.redirect }); return res.end(); }
      res.writeHead(200); res.end(route.body);
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

function makeTarball(dir, target) {
  const inner = path.join(dir, 'build', `uv-${target}`);
  fs.mkdirSync(inner, { recursive: true });
  fs.writeFileSync(path.join(inner, 'uv'), '#!/bin/sh\necho fake-uv\n', { mode: 0o755 });
  const out = path.join(dir, `uv-${target}.tar.gz`);
  spawnSync('tar', ['-czf', out, '-C', path.join(dir, 'build'), `uv-${target}`]);
  return fs.readFileSync(out);
}

const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

test('platformKey joins platform and arch', () => {
  assert.strictEqual(L.platformKey('win32', 'x64'), 'win32-x64');
});

test('bundled pins cover six targets', () => {
  const pins = require('../../packaging/mcpb/server/uv-pins.json');
  assert.deepStrictEqual(Object.keys(pins.assets).sort(), [
    'darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'win32-arm64', 'win32-x64',
  ]);
});

test('cacheRoot honours PDF_MCP_CACHE_DIR with ~ and defaults to ~/.cache/pdf-mcp', () => {
  assert.strictEqual(L.cacheRoot({}, '/home/u'), path.join('/home/u', '.cache', 'pdf-mcp'));
  assert.strictEqual(L.cacheRoot({ PDF_MCP_CACHE_DIR: '~/c' }, '/home/u'), path.join('/home/u', 'c'));
  assert.strictEqual(L.cacheRoot({ PDF_MCP_CACHE_DIR: '/x/y' }, '/home/u'), '/x/y');
});

test('tarCommand uses System32 tar.exe on Windows by absolute path', () => {
  assert.strictEqual(
    L.tarCommand('win32', { SystemRoot: 'C:\\Windows' }),
    path.join('C:\\Windows', 'System32', 'tar.exe'),
  );
  assert.strictEqual(L.tarCommand('linux', {}), 'tar');
});

test('download follows redirects and returns the sha256', async () => {
  const body = Buffer.from('payload');
  const server = await serve({ '/a': { redirect: '/b' }, '/b': { body } });
  const { port } = server.address();
  const dest = path.join(tmpdir(), 'f');
  try {
    const got = await L.download(`http://127.0.0.1:${port}/a`, dest, { get: http.get });
    assert.strictEqual(got, sha(body));
    assert.deepStrictEqual(fs.readFileSync(dest), body);
  } finally { server.close(); }
});

test('ensureUv downloads, verifies, extracts, and caches', { skip: process.platform === 'win32' }, async () => {
  const dir = tmpdir();
  const target = 'test-target';
  const archive = makeTarball(dir, target);
  const server = await serve({ [`/v1/uv-${target}.tar.gz`]: { body: archive } });
  const { port } = server.address();
  const pins = { version: 'v1', assets: { 'linux-x64': { file: `uv-${target}.tar.gz`, sha256: sha(archive) } } };
  const opts = { pins, platform: 'linux', arch: 'x64', env: { PDF_MCP_CACHE_DIR: dir }, home: dir, get: http.get, base: `http://127.0.0.1:${port}/` };
  try {
    const uv = await L.ensureUv(opts);
    assert.strictEqual(uv, path.join(dir, 'uv', 'v1', 'uv'));
    assert.ok(fs.existsSync(uv));
    server.close();
    // Second call must not touch the network (server is closed).
    assert.strictEqual(await L.ensureUv(opts), uv);
  } finally { server.close(); }
});

test('ensureUv rejects a hash mismatch and leaves no binary', async () => {
  const dir = tmpdir();
  const server = await serve({ '/v1/uv.tar.gz': { body: Buffer.from('evil') } });
  const { port } = server.address();
  const pins = { version: 'v1', assets: { 'linux-x64': { file: 'uv.tar.gz', sha256: 'f'.repeat(64) } } };
  try {
    await assert.rejects(
      L.ensureUv({ pins, platform: 'linux', arch: 'x64', env: { PDF_MCP_CACHE_DIR: dir }, home: dir, get: http.get, base: `http://127.0.0.1:${port}/` }),
      (err) => err instanceof L.SetupError && /damaged|verify/i.test(err.userMessage),
    );
    assert.ok(!fs.existsSync(path.join(dir, 'uv', 'v1')));
  } finally { server.close(); }
});

test('ensureUv names the computer when the platform is unsupported', async () => {
  await assert.rejects(
    L.ensureUv({ pins: { version: 'v1', assets: {} }, platform: 'sunos', arch: 'sparc', env: {}, home: tmpdir() }),
    (err) => err instanceof L.SetupError && err.userMessage.includes('sunos-sparc'),
  );
});

test('ensureUv reports an offline download in plain words', async () => {
  const pins = { version: 'v1', assets: { 'linux-x64': { file: 'uv.tar.gz', sha256: 'a'.repeat(64) } } };
  const dir = tmpdir();
  await assert.rejects(
    L.ensureUv({ pins, platform: 'linux', arch: 'x64', env: { PDF_MCP_CACHE_DIR: dir }, home: dir, get: http.get, base: 'http://127.0.0.1:9/' }),
    (err) => err instanceof L.SetupError && /internet/i.test(err.userMessage),
  );
});
