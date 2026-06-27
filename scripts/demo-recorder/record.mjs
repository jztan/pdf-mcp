// Records the README demo GIF (docs/images/demo.gif) by driving the live
// browser demo (pages/index.html) and encoding an optimized GIF.
//
// Pipeline: static-serve pages/ -> Playwright/chromium runs the journey and
// records webm -> ffmpeg (palette-quantized GIF) -> gifsicle (lossy LZW).
//
// Usage:
//   cd scripts/demo-recorder
//   npm install
//   npx playwright install chromium
//   npm run record            # writes ../../docs/images/demo.gif
//
// Tunables via env: FPS, MAXCOLORS, LOSSY, OUT_GIF, PORT.
// Requires `ffmpeg` on PATH (brew install ffmpeg).

import { chromium } from "playwright";
import gifsicle from "gifsicle";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, extname, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGES_DIR = resolve(HERE, "../../pages");
const OUT = process.env.OUT_GIF || resolve(HERE, "../../docs/images/demo.gif");
const PORT = Number(process.env.PORT || 8011);
const FPS = Number(process.env.FPS || 16);
const MAXCOLORS = Number(process.env.MAXCOLORS || 144);
const LOSSY = Number(process.env.LOSSY || 120);
const OUT_W = 760;                                 // README display width
// Capture the full desktop layout at a larger viewport, then downscale to
// OUT_W — so each frame shows the whole scene (doc card, grid, search, legend)
// rather than a zoomed-in crop that needs scrolling.
const CW = Number(process.env.SCENE_W || 1056);
const CH = Number(process.env.SCENE_H || 760);
const DSF = 2;

const t0 = Date.now();
const mark = (s) => console.log(`  +${((Date.now() - t0) / 1000).toFixed(2)}s  ${s}`);

// ── Minimal static server for pages/ (no deps; pdf.js loads from CDN) ────────
const TYPES = { ".html": "text/html", ".pdf": "application/pdf", ".png": "image/png",
  ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml" };
const server = createServer((req, res) => {
  let path = decodeURIComponent(req.url.split("?")[0]);
  if (path === "/") path = "/index.html";
  const file = join(PAGES_DIR, path);
  try {
    if (!file.startsWith(PAGES_DIR) || !statSync(file).isFile()) throw 0;
    res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(readFileSync(file));
  } catch {
    res.writeHead(404).end("not found");
  }
});
await new Promise((r) => server.listen(PORT, r));
const URL = `http://localhost:${PORT}/`;
mark(`serving ${PAGES_DIR} at ${URL}`);

// ── Drive the journey and record ────────────────────────────────────────────
const videoDir = mkdtempSync(join(tmpdir(), "demo-vid-"));
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: CW, height: CH },
  deviceScaleFactor: DSF,
  colorScheme: "light",
  reducedMotion: "no-preference",
  recordVideo: { dir: videoDir, size: { width: CW, height: CH } },
});
const page = await context.newPage();
await page.goto(URL, { waitUntil: "load" });
mark("loaded");

// Pan the viewport to an element with an eased animation (instead of an instant
// jump or the browser's quick native smooth-scroll) so section-to-section
// transitions read as a deliberate camera move, not a cut.
const smoothTo = async (selector, ratio = 0.16, dur = 750) => {
  await page.evaluate(({ sel, r, d }) => new Promise((resolve) => {
    const el = document.querySelector(sel);
    const startY = window.scrollY;
    const targetY = Math.max(0, startY + el.getBoundingClientRect().top - window.innerHeight * r);
    const dist = targetY - startY;
    if (Math.abs(dist) < 2) return resolve();
    const t0 = performance.now();
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2); // easeInOutQuad
    const step = (now) => {
      const p = Math.min(1, (now - t0) / d);
      window.scrollTo(0, startY + dist * ease(p));
      p < 1 ? requestAnimationFrame(step) : resolve();
    };
    requestAnimationFrame(step);
  }), { sel: selector, r: ratio, d: dur });
  await page.waitForTimeout(150);
};

// 1. Hero: let the search-scan animation play through once.
await page.waitForTimeout(2600);
mark("hero shown");

// Pan down to the "2 pages read" line + CTAs first, so clicking the sample
// button doesn't trigger an instant auto-scroll jump.
await smoothTo("#ctaSample", 0.62);
await page.waitForTimeout(550);

// 2. Load the sample contract; wait for the live page-grid to build.
await page.click("#ctaSample");
await page.waitForSelector("#workflow", { state: "visible", timeout: 60000 });
await page.waitForFunction(() => document.querySelectorAll("#liveGrid .pgcell").length > 0, null, { timeout: 60000 });
mark("sample loaded + grid built");
// Pan down from the hero to the document card so the mapped 216-page grid shows.
await smoothTo("#docCard", 0.05);
await page.waitForTimeout(700);

// 3. Type the first suggested query (derived from the doc -> guaranteed match).
const query = await page.$eval("#suggestedChips .chip", (el) => el.dataset.q);
mark(`query = "${query}"`);
await page.waitForTimeout(300);
await page.locator("#searchInput").pressSequentially(query, { delay: 80 });
await page.waitForTimeout(350);

// 4. Search -> matches sweep the grid, result cards fade in.
await page.click("#searchBtn");
await page.waitForSelector("#searchResults .result-card.on", { timeout: 15000 });
mark("results shown");
await page.waitForTimeout(1200);

// 5. Open a matched page inline (the agent reads only that page).
await smoothTo("#searchResults", 0.22);
await page.locator("#searchResults .result-card").first().click();
await page.waitForSelector(".inline-reader.open", { timeout: 10000 });
mark("inline read open");
await page.waitForTimeout(1800);

// 6. The payoff: reading a page reveals the receipt — "You read 1 page of 216".
await page.waitForSelector("#receipt:not(.hidden)", { timeout: 5000 });
await smoothTo("#receipt", 0.12);
mark("receipt shown");
await page.waitForTimeout(2300);

await context.close();
await browser.close();
server.close();

const webm = join(videoDir, readdirSync(videoDir).find((f) => f.endsWith(".webm")));
mark(`video: ${webm}`);

// ── Encode: palette-quantized GIF, then lossy LZW optimization ──────────────
const rawGif = join(videoDir, "raw.gif");
const vf = `fps=${FPS},scale=${OUT_W}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff:max_colors=${MAXCOLORS}[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`;
execFileSync("ffmpeg", ["-y", "-i", webm, "-vf", vf, "-loop", "0", rawGif], { stdio: "inherit" });
execFileSync(gifsicle, ["-O3", `--lossy=${LOSSY}`, rawGif, "-o", OUT], { stdio: "inherit" });
mark(`gif: ${OUT} (${(statSync(OUT).size / 1e6).toFixed(2)} MB)`);
console.log("done");
