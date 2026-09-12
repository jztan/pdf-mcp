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

// Tests that spawn processes or open sockets: a hang becomes a named failure
// instead of stalling the whole file (CI once timed the file out at 300 s).
const SPAWNS = { timeout: 30000 };

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

test('download follows redirects and returns the sha256', SPAWNS, async () => {
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

test('ensureUv downloads, verifies, extracts, and caches', { ...SPAWNS, skip: process.platform === 'win32' }, async () => {
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

test('ensureUv rejects a hash mismatch and leaves no binary', SPAWNS, async () => {
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

test('ensureUv reports an offline download in plain words', SPAWNS, async () => {
  const pins = { version: 'v1', assets: { 'linux-x64': { file: 'uv.tar.gz', sha256: 'a'.repeat(64) } } };
  const dir = tmpdir();
  await assert.rejects(
    L.ensureUv({ pins, platform: 'linux', arch: 'x64', env: { PDF_MCP_CACHE_DIR: dir }, home: dir, get: http.get, base: 'http://127.0.0.1:9/' }),
    (err) => err instanceof L.SetupError && /internet/i.test(err.userMessage),
  );
});

const { PassThrough } = require('node:stream');

function collect(stream) {
  const lines = [];
  let buf = '';
  stream.on('data', (c) => {
    buf += c.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { lines.push(JSON.parse(buf.slice(0, i))); buf = buf.slice(i + 1); }
  });
  return lines;
}
const tick = () => new Promise((r) => setTimeout(r, 30));
const TOOLS = [{ name: 'pdf_info', description: 'Page count.' }, { name: 'pdf_search', description: 'Search.' }];

test('fallback answers initialize, ping, tools/list, tools/call; ignores notifications', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough();
  const out = collect(stdout);
  L.fallbackServe('offline, reconnect', { stdin, stdout, tools: TOOLS, version: '1.2.3' });
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'ping' }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 3, method: 'tools/list' }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'pdf_info', arguments: {} } }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 5, method: 'resources/list' }) + '\n');
  await tick();
  assert.deepStrictEqual(out.map((m) => m.id), [1, 2, 3, 4, 5]); // no reply to the notification
  assert.strictEqual(out[0].result.protocolVersion, '2025-06-18');
  assert.deepStrictEqual(out[0].result.capabilities, { tools: {} });
  assert.deepStrictEqual(out[0].result.serverInfo, { name: 'pdf-mcp', version: '1.2.3' });
  assert.deepStrictEqual(out[1].result, {});
  assert.strictEqual(out[2].result.tools.length, 2);
  assert.ok(out[2].result.tools.every((t) => t.description.startsWith('UNAVAILABLE: pdf-mcp could not finish setting up.')));
  assert.ok(out[2].result.tools.every((t) => t.inputSchema.type === 'object'));
  assert.strictEqual(out[3].result.isError, true);
  assert.match(out[3].result.content[0].text, /offline, reconnect/);
  assert.match(out[4].error.message, /offline, reconnect/);
});

test('fallback replays prefilled input (the initialize the child never answered)', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough();
  const out = collect(stdout);
  const prefill = JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'initialize', params: {} }) + '\n';
  L.fallbackServe('m', { stdin, stdout, tools: TOOLS, version: '1', prefill });
  await tick();
  assert.strictEqual(out[0].id, 7);
});

test('runServer pipes bytes both ways', SPAWNS, async () => {
  const echo = path.join(tmpdir(), 'echo.js');
  fs.writeFileSync(echo, "process.stdin.on('data', (d) => process.stdout.write(d));");
  const stdin = new PassThrough(); const stdout = new PassThrough(); const stderr = new PassThrough();
  const child = L.runServer(process.execPath, [echo], { env: process.env, stdin, stdout, stderr, onEarlyExit: () => assert.fail('no early exit'), onExit: () => {} });
  try {
    // Wait for the echo, not a fixed delay: a loaded CI runner can take far
    // longer than a few ticks to start a node child.
    const got = await new Promise((resolve) => {
      let buf = '';
      stdout.on('data', (d) => { buf += d; if (buf.endsWith('\n')) resolve(buf); });
      stdin.write('{"hello":1}\n');
    });
    assert.strictEqual(got, '{"hello":1}\n');
  } finally {
    child.kill(); // a live child would keep this file's event loop alive
  }
});

test('runServer reports an early exit with the forwarded input and stderr tail', SPAWNS, async () => {
  const dies = path.join(tmpdir(), 'dies.js');
  fs.writeFileSync(dies, "process.stdin.once('data', () => { process.stderr.write('no network'); process.exit(2); });");
  const stdin = new PassThrough(); const stdout = new PassThrough(); const stderr = new PassThrough();
  const seen = await new Promise((resolve) => {
    L.runServer(process.execPath, [dies], { env: process.env, stdin, stdout, stderr, onEarlyExit: (prefill, tail) => resolve({ prefill, tail }) });
    stdin.write('{"id":1}\n');
  });
  assert.strictEqual(seen.prefill, '{"id":1}\n');
  assert.match(seen.tail, /no network/);
});

test('venvDir is <cache>/venv/<bundle version>, outside the extension folder', () => {
  assert.strictEqual(
    L.venvDir({}, '/home/u', '3.2.0'),
    path.join('/home/u', '.cache', 'pdf-mcp', 'venv', '3.2.0'),
  );
  assert.strictEqual(L.venvDir({ PDF_MCP_CACHE_DIR: '/x' }, '/home/u', '1'), path.join('/x', 'venv', '1'));
});

test('bundleEnv keeps every uv dir under the pdf-mcp cache and forces managed Python', () => {
  const env = L.bundleEnv({ PATH: '/bin', PDF_MCP_CACHE_DIR: '/c' }, '/home/u', '3.2.0');
  assert.strictEqual(env.PATH, '/bin');
  assert.strictEqual(env.UV_PROJECT_ENVIRONMENT, path.join('/c', 'venv', '3.2.0'));
  assert.strictEqual(env.UV_CACHE_DIR, path.join('/c', 'uv-cache'));
  assert.strictEqual(env.UV_PYTHON_INSTALL_DIR, path.join('/c', 'python'));
  assert.strictEqual(env.UV_PYTHON_PREFERENCE, 'only-managed');
});

test('pruneVenvs removes other versions, keeps the current one, survives a failed delete', () => {
  const root = tmpdir();
  for (const v of ['3.0.0', '3.1.0', '3.2.0']) fs.mkdirSync(path.join(root, v));
  const logs = [];
  L.pruneVenvs(root, '3.2.0', {
    rm: (p, o) => { if (p.endsWith('3.0.0')) throw new Error('EBUSY: locked'); fs.rmSync(p, o); },
    log: (m) => logs.push(m),
  });
  assert.deepStrictEqual(fs.readdirSync(root).sort(), ['3.0.0', '3.2.0']);
  assert.match(logs.join('\n'), /EBUSY/);
});

test('pruneVenvs is a no-op when no venv dir exists yet', () => {
  L.pruneVenvs(path.join(tmpdir(), 'missing'), '1', {});
});

test('stdin EOF ends the child and reports its exit code', SPAWNS, async () => {
  const eof = path.join(tmpdir(), 'eof.js');
  fs.writeFileSync(eof, "process.stdin.on('data', (d) => process.stdout.write(d)); process.stdin.on('end', () => process.exit(3));");
  const stdin = new PassThrough(); const stdout = new PassThrough(); const stderr = new PassThrough();
  const code = await new Promise((resolve) => {
    L.runServer(process.execPath, [eof], {
      env: process.env, stdin, stdout, stderr,
      onEarlyExit: () => assert.fail('no early exit'), onExit: resolve,
    });
    stdin.write('{"id":1}\n');
    setTimeout(() => stdin.end(), 100);
  });
  assert.strictEqual(code, 3);
});

test('a killed launcher leaves no child running (EOF, not signals)', SPAWNS, async () => {
  const { spawn } = require('node:child_process');
  const dir = tmpdir();
  const childJs = path.join(dir, 'child.js');
  fs.writeFileSync(childJs, "process.stdout.write(process.pid + '\\n'); process.stdin.resume(); process.stdin.on('end', () => process.exit(0));");
  const parentJs = path.join(dir, 'parent.js');
  const launcher = path.resolve(__dirname, '../../packaging/mcpb/server/launcher.js');
  fs.writeFileSync(parentJs, [
    "process.env.PDF_MCP_LAUNCHER_TEST = '1';",
    `const L = require(${JSON.stringify(launcher)});`,
    `L.runServer(process.execPath, [${JSON.stringify(childJs)}], { env: process.env,`,
    '  stdin: process.stdin, stdout: process.stdout, stderr: process.stderr, onEarlyExit() {} });',
  ].join('\n'));
  const parent = spawn(process.execPath, [parentJs], { stdio: ['pipe', 'pipe', 'pipe'] });
  let childPid = 0;
  try {
    childPid = await new Promise((resolve, reject) => {
      let buf = '';
      parent.stdout.on('data', (d) => {
        buf += d;
        const line = buf.split('\n')[0];
        if (buf.includes('\n')) resolve(Number(line.trim()));
      });
      parent.on('exit', (code) => reject(new Error(`launcher exited early (${code})`)));
    });
    assert.ok(childPid > 0, `bad child pid ${childPid}`);
    parent.kill('SIGKILL'); // TerminateProcess on Windows: no signal reaches the child either way
    const alive = () => { try { process.kill(childPid, 0); return true; } catch { return false; } };
    for (let i = 0; i < 50 && alive(); i += 1) await new Promise((r) => setTimeout(r, 100));
    assert.strictEqual(alive(), false, `child ${childPid} outlived its launcher`);
  } finally {
    // Never leave a process or pipe behind: either would keep this test
    // file's event loop alive and hang the whole run.
    if (childPid > 0) { try { process.kill(childPid, 'SIGKILL'); } catch { /* already gone */ } }
    try { parent.kill('SIGKILL'); } catch { /* already gone */ }
    parent.stdin.destroy(); parent.stdout.destroy(); parent.stderr.destroy();
  }
});

// ---- Early-init mode (Task 4a): answer initialize at once, hand over later.

function fakeChild() {
  const stdin = new PassThrough(); const stdout = new PassThrough();
  // What the launcher sent to the child. Tolerant: after hand-over the
  // launcher forwards raw bytes, and one test sends non-JSON on purpose.
  const got = [];
  let buf = '';
  stdin.on('data', (c) => {
    buf += c.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      try { got.push(JSON.parse(line)); } catch { /* raw passthrough */ }
    }
  });
  return { stdin, stdout, got };
}
const INIT = (id = 0, v = '2025-06-18') => JSON.stringify({ jsonrpc: '2.0', id, method: 'initialize', params: { protocolVersion: v, capabilities: {}, clientInfo: { name: 'claude-ai', version: '1' } } }) + '\n';

test('negotiateVersion follows the Python SDK rule', () => {
  const supported = ['2024-11-05', '2025-06-18', '2025-11-25'];
  assert.strictEqual(L.negotiateVersion('2025-06-18', supported, '2025-11-25'), '2025-06-18');
  assert.strictEqual(L.negotiateVersion('2099-01-01', supported, '2025-11-25'), '2025-11-25');
  assert.strictEqual(L.negotiateVersion(undefined, supported, '2025-11-25'), '2025-11-25');
});

test('early: initialize is answered at once and advertises listChanged', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough(); const out = collect(stdout);
  L.earlyServe({ stdin, stdout, tools: TOOLS, version: '1.2.3' });
  stdin.write(INIT(0, '2025-06-18'));
  await tick();
  assert.strictEqual(out[0].id, 0);
  assert.strictEqual(out[0].result.protocolVersion, L.negotiateVersion('2025-06-18'));
  assert.deepStrictEqual(out[0].result.capabilities, { tools: { listChanged: true } });
  assert.deepStrictEqual(out[0].result.serverInfo, { name: 'pdf-mcp', version: '1.2.3' });
});

test('early: tools/list returns the real names as STARTING placeholders; calls say retry', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough(); const out = collect(stdout);
  L.earlyServe({ stdin, stdout, tools: TOOLS, version: '1' });
  stdin.write(INIT());
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'pdf_info', arguments: {} } }) + '\n');
  await tick();
  const list = out.find((m) => m.id === 1).result.tools;
  assert.deepStrictEqual(list.map((t) => t.name), TOOLS.map((t) => t.name));
  assert.ok(list.every((t) => t.description.startsWith('STARTING: pdf-mcp is finishing first-time setup')));
  const call = out.find((m) => m.id === 2).result;
  assert.strictEqual(call.isError, true);
  assert.match(call.content[0].text, /try again in a minute/);
});

test('early: handOver replays the client initialize, hides the reply, then pipes raw bytes', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough(); const out = collect(stdout);
  let raw = ''; stdout.on('data', (d) => { raw += d; });
  const early = L.earlyServe({ stdin, stdout, tools: TOOLS, version: '1' });
  stdin.write(INIT(0, '2025-06-18'));
  await tick();
  const child = fakeChild();
  const done = early.handOver(child);
  await tick();
  // The child got the client's own initialize params under the launcher's id.
  assert.strictEqual(child.got[0].method, 'initialize');
  assert.strictEqual(child.got[0].params.protocolVersion, '2025-06-18');
  assert.deepStrictEqual(child.got[0].params.clientInfo, { name: 'claude-ai', version: '1' });
  // A request arriving mid-handshake is queued, not answered by the placeholder.
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'pdf_info', arguments: {} } }) + '\n');
  await tick();
  assert.ok(!out.some((m) => m.id === 7));
  child.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: child.got[0].id, result: { protocolVersion: '2025-06-18', capabilities: {}, serverInfo: { name: 'pdf-mcp', version: '1' } } }) + '\n');
  await done;
  await tick();
  assert.strictEqual(child.got[1].method, 'notifications/initialized');
  assert.strictEqual(child.got[2].id, 7); // the queued call, after initialized
  // The client never saw the child's initialize reply; it did get one list_changed.
  assert.strictEqual(out.filter((m) => m.id === 0).length, 1);
  assert.strictEqual(out.filter((m) => m.method === 'notifications/tools/list_changed').length, 1);
  // From here on the launcher parses nothing, in either direction.
  child.stdout.write('{"id":7,"result":{"content":[]}}\n');
  stdin.write('not json at all\n');
  await tick();
  assert.ok(raw.endsWith('{"id":7,"result":{"content":[]}}\n'));
  let childRaw = ''; child.stdin.on('data', (d) => { childRaw += d; });
  stdin.write('still not json\n');
  await tick();
  assert.strictEqual(childRaw, 'still not json\n');
});

test('early: handOver waits for the client initialize when the server is faster', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough();
  const early = L.earlyServe({ stdin, stdout, tools: TOOLS, version: '1' });
  const child = fakeChild();
  early.handOver(child);
  await tick();
  assert.strictEqual(child.got.length, 0); // nothing to replay yet
  stdin.write(INIT(3, '2025-03-26'));
  await tick();
  assert.strictEqual(child.got[0].method, 'initialize');
  assert.strictEqual(child.got[0].params.protocolVersion, '2025-03-26');
});

test('early: fail() turns the placeholders into the fallback responder', async () => {
  const stdin = new PassThrough(); const stdout = new PassThrough(); const out = collect(stdout);
  const early = L.earlyServe({ stdin, stdout, tools: TOOLS, version: '1' });
  stdin.write(INIT());
  await tick();
  early.fail('offline, reconnect');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'pdf_info', arguments: {} } }) + '\n');
  stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 6, method: 'tools/list' }) + '\n');
  await tick();
  assert.ok(out.some((m) => m.method === 'notifications/tools/list_changed'));
  assert.match(out.find((m) => m.id === 5).result.content[0].text, /offline, reconnect/);
  assert.ok(out.find((m) => m.id === 6).result.tools.every((t) => t.description.startsWith('UNAVAILABLE:')));
});

test('uv is started outside the extension folder so an upgrade can replace it', () => {
  // Measured on Windows: Claude Desktop replaces the extension folder in place
  // while the server runs, and `uv run --directory <bundle>` made that folder
  // the working directory of uv and python, so rmdir failed with EBUSY.
  const { args, cwd } = L.uvRunCommand('/ext/pdf-mcp', '/home/u');
  assert.ok(!args.includes('--directory'));
  assert.deepStrictEqual(args.slice(0, 3), ['run', '--project', '/ext/pdf-mcp']);
  assert.strictEqual(args[3], path.join('/ext/pdf-mcp', 'src', 'server.py'));
  assert.strictEqual(cwd, '/home/u');
});

test('runServer passes its cwd to the child', async () => {
  const dir = tmpdir();
  const pwd = path.join(tmpdir(), 'pwd.js');
  fs.writeFileSync(pwd, "process.stdout.write(process.cwd() + '\\n'); process.stdin.resume();");
  const stdin = new PassThrough(); const stdout = new PassThrough(); const stderr = new PassThrough();
  const child = L.runServer(process.execPath, [pwd], { env: process.env, cwd: dir, stdin, stdout, stderr, onEarlyExit: () => {}, onExit: () => {} });
  try {
    const got = await new Promise((resolve) => stdout.once('data', (d) => resolve(String(d).trim())));
    assert.strictEqual(fs.realpathSync(got), fs.realpathSync(dir));
  } finally {
    child.kill();
  }
});
