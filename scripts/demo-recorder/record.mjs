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
const FPS = Number(process.env.FPS || 14);
const MAXCOLORS = Number(process.env.MAXCOLORS || 144);
const LOSSY = Number(process.env.LOSSY || 100);
const OUT_W = 760;                                 // README display width
// Capture at a viewport just under the demo stage's two-column breakpoint
// (960px) so the document/folder card fills the frame on its own, with the
// JSON panel stacked below it out of shot; then downscale to OUT_W.
const CW = Number(process.env.SCENE_W || 940);
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
const videoStart = Date.now();
// Warm the caches (web fonts, pdf.js) on a throwaway load, then reload so the
// take begins on a fully settled page. Video capture runs from page creation,
// so record when the settled page is ready and trim the webm there.
await page.goto(URL, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
await page.goto(URL, { waitUntil: "load" });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(120);
const trimMs = Date.now() - videoStart;
mark(`loaded (trimming first ${(trimMs / 1000).toFixed(2)}s)`);

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

// 1. Hero: let the search-scan animation play through once, then pan to
// the "2 pages read" line it lands on (below the fold at this viewport).
await page.waitForTimeout(2600);
await smoothTo("#heroStat", 0.5, 700);
await page.waitForTimeout(1200);
mark("hero shown");

// Pan down to the "2 pages read" line + CTAs first, so clicking a sample
// button doesn't trigger an instant auto-scroll jump.
await smoothTo("#ctaCorpus", 0.62);
await page.waitForTimeout(550);

// 2. Corpus first (the headline): load the 6-PDF sample corpus.
await page.click("#ctaCorpus");
await page.waitForSelector("#corpusWorkflow", { state: "visible", timeout: 60000 });
// Hold at the hero until the warm rows exist, so the camera arrives on a
// populated card instead of panning to an empty one while PDFs fetch.
await page.waitForFunction(
  () => document.querySelectorAll("#corpusWarmPanel .warm-state").length >= 6,
  null, { timeout: 60000 }
);
mark("corpus loading");
await smoothTo("#corpusCard", 0.05);

// 3. Warm sweep: six docs extract client-side; rows flip queued -> warmed.
await page.waitForFunction(
  () => document.querySelectorAll("#corpusWarmPanel .warm-state.done").length >= 6,
  null, { timeout: 120000 }
);
mark("corpus warmed");
await page.waitForTimeout(900);

// 4. Overview triage cards (unhidden automatically after the warm).
await page.waitForSelector("#corpusOverviewPanel:not(.hidden)", { timeout: 15000 });
await smoothTo("#corpusOverviewPanel", 0.14);
await page.waitForTimeout(1500);

// 5. Cross-document search with a suggested query (prefer the multi-doc one).
await page.waitForSelector("#corpusSearchPanel:not(.hidden)", { timeout: 15000 });
await smoothTo("#corpusSearchPanel", 0.16);
const chips = await page.$$eval("#corpusChips .chip", (els) => els.map((e) => e.dataset.q));
const cq = chips[1] || chips[0];
mark(`corpus query = "${cq}"`);
await page.locator("#corpusSearchInput").pressSequentially(cq, { delay: 80 });
await page.waitForTimeout(350);
await page.click("#corpusSearchBtn");
await page.waitForSelector("#corpusResults .result-card.on", { timeout: 20000 });
mark("corpus results shown");
await page.waitForTimeout(1600);

// 6. Open the top hit: the agent reads one page out of the whole corpus.
await smoothTo("#corpusResults", 0.22);
await page.locator("#corpusResults .result-card").first().click();
// Hold on the opened card so the page text the agent read is on screen.
await page.waitForSelector("#corpusResults .result-card.reading .inline-reader.open", { timeout: 5000 });
await smoothTo("#corpusResults .result-card.reading", 0.08, 600);
mark("corpus hit opened");
await page.waitForTimeout(2000);

// 7. The payoff: reading a hit reveals the "what just happened" receipt —
// pages across six documents, and the share that never entered the context.
await page.waitForSelector("#receipt:not(.hidden)", { timeout: 5000 });
await smoothTo("#receipt", 0.12);
mark("receipt shown");
await page.waitForTimeout(2800);

await context.close();
await browser.close();
server.close();

const webm = join(videoDir, readdirSync(videoDir).find((f) => f.endsWith(".webm")));
mark(`video: ${webm}`);

// ── Encode: palette-quantized GIF, then lossy LZW optimization ──────────────
const rawGif = join(videoDir, "raw.gif");
const vf = `fps=${FPS},scale=${OUT_W}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff:max_colors=${MAXCOLORS}[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`;
execFileSync("ffmpeg", ["-y", "-ss", (trimMs / 1000).toFixed(3), "-i", webm, "-vf", vf, "-loop", "0", rawGif], { stdio: "inherit" });
execFileSync(gifsicle, ["-O3", `--lossy=${LOSSY}`, rawGif, "-o", OUT], { stdio: "inherit" });
mark(`gif: ${OUT} (${(statSync(OUT).size / 1e6).toFixed(2)} MB)`);
console.log("done");
