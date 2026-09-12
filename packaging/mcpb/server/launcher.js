'use strict';
// pdf-mcp launcher for the Claude Desktop bundle.
//
// Runs on the Node.js that ships with Claude Desktop, so the machine needs
// nothing preinstalled. First start: download the pinned uv release,
// verify its SHA-256 against the pin shipped in this bundle, unpack it
// into the pdf-mcp cache dir, then hand the MCP stream to `uv run`.
// Any setup failure is answered over MCP (fallbackServe) so Claude can tell
// the user what happened: Claude Desktop does not show server
// `instructions` to the model, so the reason travels in tool results.
// Core modules only: no node_modules in the bundle.

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const crypto = require('crypto');
const { spawn } = require('child_process');

const BUNDLE_DIR = path.resolve(__dirname, '..');
const UV_BASE = 'https://github.com/astral-sh/uv/releases/download/';

// Early-init mode: answer `initialize` at once and hand the stream to the
// Python server when it is ready. Claude Desktop cancels `initialize` after
// 60 s and kills the server (measured on Windows Server 2025, 2026-09-12),
// while a first start downloads uv, Python and ~250 MB of dependencies.
const EARLY_INIT = true;

// The protocol versions of the pinned Python MCP SDK, which is the server
// this launcher hands over to. scripts/build_mcpb.py rewrites both lines
// from that SDK at build time; the values here are mcp 1.28.1's.
const SUPPORTED_PROTOCOL_VERSIONS = ['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'];
const LATEST_PROTOCOL_VERSION = '2025-11-25';

class SetupError extends Error {
  constructor(userMessage, detail) {
    super(detail || userMessage);
    this.userMessage = userMessage;
  }
}

const OFFLINE =
  'pdf-mcp could not download a component it needs to finish setting up. ' +
  'Check that this computer is connected to the internet, then quit and ' +
  'reopen Claude Desktop.';

function platformKey(platform, arch) {
  return `${platform}-${arch}`;
}

function cacheRoot(env, home) {
  const raw = (env.PDF_MCP_CACHE_DIR || '').trim();
  if (!raw) return path.join(home, '.cache', 'pdf-mcp');
  if (raw === '~') return home;
  if (raw.startsWith('~/') || raw.startsWith('~\\')) return path.join(home, raw.slice(2));
  return raw;
}

function uvExeName(platform) {
  return platform === 'win32' ? 'uv.exe' : 'uv';
}

function tarCommand(platform, env) {
  // Absolute path: a Git-for-Windows GNU tar earlier on PATH cannot read
  // .zip; Microsoft's bsdtar (Windows 10 1803+) can.
  if (platform === 'win32') {
    return path.join(env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe');
  }
  return 'tar';
}

function download(url, dest, { get = https.get, maxRedirects = 5 } = {}) {
  return new Promise((resolve, reject) => {
    const req = get(url, (res) => {
      const { statusCode, headers } = res;
      if ([301, 302, 303, 307, 308].includes(statusCode) && headers.location) {
        res.resume();
        if (maxRedirects <= 0) return reject(new Error('too many redirects'));
        const next = new URL(headers.location, url).toString();
        return resolve(download(next, dest, { get, maxRedirects: maxRedirects - 1 }));
      }
      if (statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${statusCode} for ${url}`));
      }
      const hash = crypto.createHash('sha256');
      const out = fs.createWriteStream(dest);
      res.on('data', (chunk) => hash.update(chunk));
      res.pipe(out);
      out.on('finish', () => out.close(() => resolve(hash.digest('hex'))));
      out.on('error', reject);
      res.on('error', reject);
    });
    req.on('error', reject);
    req.setTimeout(60000, () => req.destroy(new Error('download timed out')));
  });
}

function runAsync(cmd, args, opts = {}) {
  // Async so a cold start's archive extraction never blocks the event loop
  // that answers Claude Desktop.
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, args, { ...opts, windowsHide: true });
    } catch {
      resolve({ status: null });
      return;
    }
    child.on('error', () => resolve({ status: null }));
    child.on('exit', (code) => resolve({ status: code }));
  });
}

function findFile(dir, name, depth = 2) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isFile() && entry.name === name) return full;
    if (entry.isDirectory() && depth > 0) {
      const hit = findFile(full, name, depth - 1);
      if (hit) return hit;
    }
  }
  return null;
}

async function ensureUv({
  pins, platform = process.platform, arch = process.arch, env = process.env,
  home = os.homedir(), get = https.get, run = runAsync,
  base = env.PDF_MCP_UV_DOWNLOAD_BASE || UV_BASE,
}) {
  const key = platformKey(platform, arch);
  const asset = pins.assets[key];
  if (!asset) {
    throw new SetupError(
      `pdf-mcp does not support this computer yet (${key}). ` +
      'Install it with pip instead: see https://github.com/jztan/pdf-mcp#installation',
    );
  }
  const root = path.join(cacheRoot(env, home), 'uv');
  const finalDir = path.join(root, pins.version);
  const exe = uvExeName(platform);
  const finalPath = path.join(finalDir, exe);
  if (fs.existsSync(finalPath)) return finalPath;

  fs.mkdirSync(root, { recursive: true });
  const work = fs.mkdtempSync(path.join(root, '.work-'));
  try {
    const archive = path.join(work, asset.file);
    let digest;
    try {
      digest = await download(`${base}${pins.version}/${asset.file}`, archive, { get });
    } catch (err) {
      throw new SetupError(OFFLINE, `uv download failed: ${err.message}`);
    }
    if (digest !== asset.sha256) {
      throw new SetupError(
        'pdf-mcp downloaded a component that failed its safety check (it may ' +
        'be damaged). Quit and reopen Claude Desktop to try again.',
        `sha256 mismatch for ${asset.file}: got ${digest}`,
      );
    }
    const unpacked = path.join(work, 'x');
    fs.mkdirSync(unpacked);
    const res = await run(tarCommand(platform, env), ['-xf', archive, '-C', unpacked], { stdio: 'ignore' });
    const found = res.status === 0 ? findFile(unpacked, exe) : null;
    if (!found) {
      throw new SetupError(OFFLINE, `could not unpack ${asset.file} (tar exit ${res.status})`);
    }
    const staging = path.join(work, 'stage');
    fs.mkdirSync(staging);
    fs.renameSync(found, path.join(staging, exe));
    if (platform !== 'win32') fs.chmodSync(path.join(staging, exe), 0o755);
    try {
      fs.renameSync(staging, finalDir);
    } catch (err) {
      if (!fs.existsSync(finalPath)) throw err; // a concurrent start won the race
    }
    return finalPath;
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

function venvDir(env, home, version) {
  // Outside the extension folder, which Claude Desktop replaces on every
  // update or reinstall; per version, so a new bundle never runs on an old
  // bundle's pins.
  return path.join(cacheRoot(env, home), 'venv', version);
}

function bundleEnv(env, home, version) {
  // All uv state under the pdf-mcp cache: uv's defaults live under AppData,
  // which MSIX redirects to Claude Desktop's private storage on Windows,
  // and one folder is one thing to delete on uninstall. Python comes from
  // python-build-standalone via uv, never the machine's.
  const root = cacheRoot(env, home);
  // An activated virtualenv in the user's environment must not leak in:
  // uv would warn about it on every start and it is never the right target.
  const { VIRTUAL_ENV: _ignored, ...rest } = env;
  return {
    ...rest,
    UV_PROJECT_ENVIRONMENT: venvDir(env, home, version),
    UV_CACHE_DIR: path.join(root, 'uv-cache'),
    UV_PYTHON_INSTALL_DIR: path.join(root, 'python'),
    UV_PYTHON_PREFERENCE: 'only-managed',
  };
}

function uvRunCommand(bundleDir, home) {
  // --project, not --directory: --directory makes the extension folder the
  // working directory of uv and python, and Windows then refuses to let
  // Claude Desktop replace that folder on upgrade (EBUSY, measured). The
  // server runs from the user's home instead, which also gives any relative
  // path a sensible base.
  return {
    args: ['run', '--project', bundleDir, path.join(bundleDir, 'src', 'server.py')],
    cwd: home,
  };
}

async function chmodTree(target) {
  // Clears read-only files and folders (on Windows, chmod's write bit is the
  // read-only attribute), so a tree can be deleted.
  let st;
  try { st = await fs.promises.lstat(target); } catch { return; }
  if (st.isSymbolicLink()) return;
  if (!st.isDirectory()) {
    try { await fs.promises.chmod(target, 0o666); } catch { /* best effort */ }
    return;
  }
  try { await fs.promises.chmod(target, 0o777); } catch { /* best effort */ }
  let entries = [];
  try { entries = await fs.promises.readdir(target); } catch { /* best effort */ }
  for (const name of entries) await chmodTree(path.join(target, name));
}

async function rmTree(target, { rm = fs.promises.rm, chmod = chmodTree } = {}) {
  // uv hardlinks venv files from its cache, and those are read-only; on
  // Windows Node's rm then fails with EPERM (measured). Clear and retry,
  // as rimraf does.
  // Claude Desktop also starts two or three launchers at once, each of which
  // prunes: whoever loses the race sees EPERM on files the winner is
  // already deleting. A tree that is gone afterwards is a success.
  const opts = { recursive: true, force: true, maxRetries: 3 };
  const gone = () => !fs.existsSync(target);
  try {
    await rm(target, opts);
  } catch (err) {
    if (gone()) return;
    if (err.code !== 'EPERM' && err.code !== 'EACCES') throw err;
    await chmod(target);
    try {
      await rm(target, opts);
    } catch (again) {
      if (!gone()) throw again;
    }
  }
}

async function pruneVenvs(root, keep, { rm = rmTree, log = () => {} } = {}) {
  // Async: an old venv is ~250 MB of small files, and deleting it
  // synchronously blocked the event loop for 12 s (measured), long enough
  // to leave Claude Desktop's initialize unanswered.
  let entries;
  try { entries = await fs.promises.readdir(root, { withFileTypes: true }); } catch { return; }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name === keep) continue;
    try {
      await rm(path.join(root, entry.name), { recursive: true, force: true });
    } catch (err) {
      log(`could not remove old venv ${entry.name}: ${err.message}`);
    }
  }
}

function launcherLog(file, { echo = (m) => process.stderr.write(`${m}\n`), maxBytes = 1024 * 1024 } = {}) {
  // Claude Desktop does not show an extension's stderr, so the launcher
  // also keeps its own log beside the cache it manages.
  try {
    if (fs.statSync(file).size > maxBytes) fs.rmSync(file, { force: true });
  } catch { /* no log yet */ }
  let chain = Promise.resolve();
  const log = (message) => {
    const line = `${new Date().toISOString()} ${message}`;
    try { echo(`pdf-mcp launcher: ${message}`); } catch { /* stderr gone */ }
    chain = chain.then(() => fs.promises.mkdir(path.dirname(file), { recursive: true }))
      .then(() => fs.promises.appendFile(file, `${line}\n`))
      .catch(() => { /* logging must never break the launcher */ });
  };
  log.flush = () => chain;
  return log;
}

function lineReader(onLine) {
  let buf = '';
  const feed = (chunk) => {
    buf += chunk.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) onLine(line);
    }
  };
  const rest = () => { const r = buf; buf = ''; return r; };
  return { feed, rest };
}

function negotiateVersion(
  requested, supported = SUPPORTED_PROTOCOL_VERSIONS, latest = LATEST_PROTOCOL_VERSION,
) {
  // The Python SDK's rule, so the launcher's early answer matches what the
  // real server agrees to at hand-over.
  return supported.includes(requested) ? requested : latest;
}

const STARTING =
  'pdf-mcp is finishing first-time setup (it downloads its components once); ' +
  'try again in a minute.';

function earlyServe({ stdin, stdout, tools, version }) {
  const send = (msg) => stdout.write(JSON.stringify({ jsonrpc: '2.0', ...msg }) + '\n');
  let mode = 'early'; // early -> piped, or failed
  let initParams = null;
  let onInit = null; // resolves a handOver that is waiting for the client
  const handle = (line) => {
    // Placeholder answers continue until the real server has replied:
    // Claude Desktop cancels any request after 30 s (measured), and a
    // Python start can take longer, so nothing is ever queued.
    if (mode !== 'early') return;
    let req;
    try { req = JSON.parse(line); } catch { return; }
    if (req.id === undefined || req.id === null) return; // notification
    const { id, method, params = {} } = req;
    if (method === 'initialize') {
      initParams = params;
      send({ id, result: {
        protocolVersion: negotiateVersion(params.protocolVersion),
        capabilities: { tools: { listChanged: true } },
        serverInfo: { name: 'pdf-mcp', version },
      } });
      if (onInit) onInit();
      return undefined;
    }
    if (method === 'ping') return send({ id, result: {} });
    if (method === 'tools/list') {
      // Real names, so a client that ignores list_changed still reaches the
      // real tools after hand-over; only this description goes stale.
      return send({ id, result: { tools: tools.map((t) => ({
        name: t.name,
        description: `STARTING: ${STARTING} (When ready: ${t.description})`,
        inputSchema: { type: 'object', additionalProperties: true },
      })) } });
    }
    if (method === 'tools/call') {
      return send({ id, result: { content: [{ type: 'text', text: STARTING }], isError: true } });
    }
    return send({ id, error: { code: -32603, message: STARTING } });
  };
  const reader = lineReader(handle);
  stdin.on('data', reader.feed);

  async function handOver(child) {
    if (mode !== 'early') return;
    if (!initParams) await new Promise((resolve) => { onInit = resolve; });
    if (mode !== 'early') return; // failed while waiting
    const initId = 'pdf-mcp-launcher-handover';
    await new Promise((resolve) => {
      let buf = '';
      const onChild = (chunk) => {
        buf += chunk.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i);
          buf = buf.slice(i + 1);
          let msg = null;
          try { msg = JSON.parse(line); } catch { /* not ours */ }
          if (msg && msg.id === initId) {
            child.stdout.removeListener('data', onChild);
            child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
            if (mode !== 'early') return; // fail() won the race
            stdin.removeListener('data', reader.feed);
            const partial = reader.rest();
            if (partial) child.stdin.write(partial);
            mode = 'piped';
            // Steady state: bytes both ways, nothing parsed.
            stdin.on('data', (c) => child.stdin.write(c));
            if (buf) stdout.write(buf);
            child.stdout.on('data', (c) => stdout.write(c));
            send({ method: 'notifications/tools/list_changed' });
            resolve();
            return;
          }
        }
      };
      child.stdout.on('data', onChild);
      child.stdin.write(JSON.stringify({
        jsonrpc: '2.0', id: initId, method: 'initialize', params: initParams,
      }) + '\n');
    });
  }

  function fail(message) {
    if (mode === 'piped' || mode === 'failed') return;
    mode = 'failed';
    stdin.removeListener('data', reader.feed);
    fallbackServe(message, { stdin, stdout, tools, version, prefill: reader.rest() });
    send({ method: 'notifications/tools/list_changed' });
  }

  return { handOver, fail };
}

function fallbackServe(message, { stdin, stdout, tools, version, prefill = '' }) {
  const send = (msg) => stdout.write(JSON.stringify({ jsonrpc: '2.0', ...msg }) + '\n');
  const handle = (line) => {
    let req;
    try { req = JSON.parse(line); } catch { return; }
    if (req.id === undefined || req.id === null) return; // notification
    const { id, method, params = {} } = req;
    if (method === 'initialize') {
      return send({ id, result: {
        protocolVersion: params.protocolVersion || '2025-06-18',
        capabilities: { tools: {} },
        serverInfo: { name: 'pdf-mcp', version },
      } });
    }
    if (method === 'ping') return send({ id, result: {} });
    if (method === 'tools/list') {
      return send({ id, result: { tools: tools.map((t) => ({
        name: t.name,
        description: `UNAVAILABLE: pdf-mcp could not finish setting up. ${message} (When working: ${t.description})`,
        inputSchema: { type: 'object', additionalProperties: true },
      })) } });
    }
    if (method === 'tools/call') {
      return send({ id, result: { content: [{ type: 'text', text: message }], isError: true } });
    }
    return send({ id, error: { code: -32603, message } });
  };
  let buf = '';
  const feed = (chunk) => {
    buf += chunk.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) handle(line);
    }
  };
  if (prefill) feed(prefill);
  stdin.on('data', feed);
}

function runServer(cmd, args, { env, cwd, stdin, stdout, stderr, onEarlyExit, onExit = process.exit }) {
  const child = spawn(cmd, args, { env, cwd, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let started = false;
  let forwarded = '';
  let tail = '';
  const onInput = (chunk) => {
    if (!started) forwarded += chunk.toString();
    child.stdin.write(chunk);
  };
  stdin.on('data', onInput);
  // Shutdown is stdin EOF: works on every OS, and if the launcher is killed
  // outright the OS closes this pipe's write end, so the child still exits.
  stdin.on('end', () => child.stdin.end());
  child.stdout.on('data', (chunk) => { started = true; forwarded = ''; stdout.write(chunk); });
  child.stderr.on('data', (chunk) => { stderr.write(chunk); tail = (tail + chunk.toString()).slice(-2000); });
  if (process.platform !== 'win32') {
    // Courtesy only; Windows has no SIGTERM to forward.
    for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => child.kill(sig));
  }
  let reported = false; // 'error' and 'exit' can both fire for one failure
  const early = (why) => {
    if (reported) return;
    reported = true;
    stdin.removeListener('data', onInput);
    onEarlyExit(forwarded, `${tail}${why ? `\n${why}` : ''}`);
  };
  child.on('error', (err) => early(err.message));
  child.on('exit', (code) => {
    if (!started) return early(`exit ${code}`);
    onExit(code === null ? 1 : code);
  });
  return child;
}

async function mainEarly(manifest, pins) {
  const home = os.homedir();
  const log = launcherLog(path.join(cacheRoot(process.env, home), 'launcher.log'));
  const t0 = Date.now();
  const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;
  log(`start pid=${process.pid} version=${manifest.version} node=${process.version} early-init`);
  const early = earlyServe({
    stdin: process.stdin, stdout: process.stdout,
    tools: manifest.tools, version: manifest.version,
  });
  let uv;
  try {
    uv = await ensureUv({ pins });
  } catch (err) {
    log(`setup failed at ${since()}: ${err.stack || err}`);
    return early.fail(err.userMessage || OFFLINE);
  }
  log(`uv ready at ${since()}: ${uv}`);
  const venv = venvDir(process.env, home, manifest.version);
  const run = uvRunCommand(BUNDLE_DIR, home);
  const child = spawn(uv, run.args, {
    env: bundleEnv(process.env, os.homedir(), manifest.version),
    cwd: run.cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
  let handed = false;
  let ended = false;
  let tail = '';
  child.stderr.on('data', (c) => { process.stderr.write(c); tail = (tail + c.toString()).slice(-2000); });
  log(`spawned uv run (pid ${child.pid}) at ${since()}`);
  // Shutdown is stdin EOF, before and after hand-over alike.
  process.stdin.on('end', () => { ended = true; log(`stdin closed at ${since()}`); child.stdin.end(); });
  if (process.platform !== 'win32') {
    for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => child.kill(sig));
  }
  const setupFailed = (why) => {
    log(`server exited during setup at ${since()}: ${why}\n${tail}`);
    early.fail(OFFLINE);
  };
  child.on('error', (err) => { if (!handed) setupFailed(err.message); });
  child.on('exit', (code, signal) => {
    if (handed || ended) {
      log(`server exited at ${since()}: code=${code} signal=${signal}\n${tail}`);
      return log.flush().then(() => process.exit(code === null ? 1 : code));
    }
    return setupFailed(`exit ${code} signal ${signal}`);
  });
  await early.handOver(child);
  handed = true;
  log(`handed over to the Python server at ${since()}`);
  // Old venvs go only now, off the event loop, once the new server is up.
  pruneVenvs(path.dirname(venv), manifest.version, { log }).catch(() => {});
  return undefined;
}

async function main() {
  const manifest = JSON.parse(fs.readFileSync(path.join(BUNDLE_DIR, 'manifest.json'), 'utf8'));
  const pins = JSON.parse(fs.readFileSync(path.join(__dirname, 'uv-pins.json'), 'utf8'));
  if (EARLY_INIT) return mainEarly(manifest, pins);
  const io = { stdin: process.stdin, stdout: process.stdout, stderr: process.stderr };
  const fallback = (message, prefill = '') =>
    fallbackServe(message, { ...io, tools: manifest.tools, version: manifest.version, prefill });
  let uv;
  try {
    uv = await ensureUv({ pins });
  } catch (err) {
    process.stderr.write(`pdf-mcp launcher: ${err.stack || err}\n`);
    return fallback(err.userMessage || OFFLINE);
  }
  const venv = venvDir(process.env, os.homedir(), manifest.version);
  const run = uvRunCommand(BUNDLE_DIR, os.homedir());
  pruneVenvs(path.dirname(venv), manifest.version, {
    log: (m) => process.stderr.write(`${m}\n`),
  }).catch(() => {});
  runServer(uv, run.args, {
    ...io,
    cwd: run.cwd,
    env: bundleEnv(process.env, os.homedir(), manifest.version),
    onEarlyExit: (prefill, tail) => {
      process.stderr.write(`pdf-mcp launcher: server exited during setup:\n${tail}\n`);
      fallback(OFFLINE, prefill);
    },
  });
}

module.exports = {
  BUNDLE_DIR, SetupError, platformKey, cacheRoot, uvExeName, tarCommand,
  download, ensureUv, fallbackServe, venvDir, bundleEnv, pruneVenvs, runServer, main, uvRunCommand,
  launcherLog, runAsync, rmTree, chmodTree,
  EARLY_INIT, SUPPORTED_PROTOCOL_VERSIONS, LATEST_PROTOCOL_VERSION, negotiateVersion, earlyServe,
};

if (process.env.PDF_MCP_LAUNCHER_TEST !== '1') {
  main().catch((err) => {
    process.stderr.write(`pdf-mcp launcher: ${err.stack || err}\n`);
    process.exit(1);
  });
}
