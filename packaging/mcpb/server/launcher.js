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

module.exports = {
  BUNDLE_DIR, SetupError, platformKey, cacheRoot, uvExeName, tarCommand,
  download, ensureUv,
};
