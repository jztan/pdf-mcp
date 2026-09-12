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
const { spawn, spawnSync } = require('child_process');

const BUNDLE_DIR = path.resolve(__dirname, '..');
const UV_BASE = 'https://github.com/astral-sh/uv/releases/download/';

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
  home = os.homedir(), get = https.get, run = spawnSync,
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
    const res = run(tarCommand(platform, env), ['-xf', archive, '-C', unpacked], { stdio: 'ignore' });
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
  return {
    ...env,
    UV_PROJECT_ENVIRONMENT: venvDir(env, home, version),
    UV_CACHE_DIR: path.join(root, 'uv-cache'),
    UV_PYTHON_INSTALL_DIR: path.join(root, 'python'),
    UV_PYTHON_PREFERENCE: 'only-managed',
  };
}

function pruneVenvs(root, keep, { rm = fs.rmSync, log = () => {} } = {}) {
  let entries;
  try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name === keep) continue;
    try {
      rm(path.join(root, entry.name), { recursive: true, force: true });
    } catch (err) {
      log(`pdf-mcp launcher: could not remove old venv ${entry.name}: ${err.message}`);
    }
  }
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

function runServer(cmd, args, { env, stdin, stdout, stderr, onEarlyExit, onExit = process.exit }) {
  const child = spawn(cmd, args, { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
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

async function main() {
  const manifest = JSON.parse(fs.readFileSync(path.join(BUNDLE_DIR, 'manifest.json'), 'utf8'));
  const pins = JSON.parse(fs.readFileSync(path.join(__dirname, 'uv-pins.json'), 'utf8'));
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
  pruneVenvs(path.dirname(venv), manifest.version, { log: (m) => process.stderr.write(`${m}\n`) });
  runServer(uv, ['run', '--directory', BUNDLE_DIR, 'src/server.py'], {
    ...io,
    env: bundleEnv(process.env, os.homedir(), manifest.version),
    onEarlyExit: (prefill, tail) => {
      process.stderr.write(`pdf-mcp launcher: server exited during setup:\n${tail}\n`);
      fallback(OFFLINE, prefill);
    },
  });
}

module.exports = {
  BUNDLE_DIR, SetupError, platformKey, cacheRoot, uvExeName, tarCommand,
  download, ensureUv, fallbackServe, venvDir, bundleEnv, pruneVenvs, runServer, main,
};

if (process.env.PDF_MCP_LAUNCHER_TEST !== '1') {
  main().catch((err) => {
    process.stderr.write(`pdf-mcp launcher: ${err.stack || err}\n`);
    process.exit(1);
  });
}
