# Demo GIF recorder

Regenerates `docs/images/demo.gif` (shown at the top of the README) by driving
the live browser demo in `pages/index.html` and encoding an optimized GIF.

The journey: hero search-scan animation and its "2 pages read" line → load the 6-PDF sample corpus →
warm sweep (rows flip queued → warmed) → overview triage cards → type a
suggested cross-document query → matches sweep the per-doc page grids →
open the top hit and hold on the page text it read → the "what just happened" receipt (pages across six
documents, share of tokens that never entered the context window).

## Prerequisites

- Node.js 18+
- `ffmpeg` on your PATH (`brew install ffmpeg`)

## Run

```bash
cd scripts/demo-recorder
npm install
npx playwright install chromium   # one-time browser download
npm run record                    # writes ../../docs/images/demo.gif
```

The script serves `pages/` itself (no separate server needed) and tears it down
when finished.

## Pipeline

static-serve `pages/` → Playwright/chromium records the journey to webm →
ffmpeg palette-quantizes and downscales to GIF → gifsicle applies lossy LZW.

The browser captures at `SCENE_W`×`SCENE_H` (default 940×760, just under the
demo stage's 960 px two-column breakpoint, so the corpus card fills the frame
on its own and the JSON panel stacks below it out of shot) and ffmpeg
downscales to 760 px wide. Section-to-section moves are eased camera pans
(not instant scroll jumps). Output is 760 px wide, 14 fps, ~7.5 MB.

## Tunables (env vars)

| var | default | effect |
| --- | --- | --- |
| `FPS` | `14` | frame rate (higher = smoother, larger) |
| `MAXCOLORS` | `144` | GIF palette size |
| `LOSSY` | `100` | gifsicle lossy level (higher = smaller, more artifacts) |
| `SCENE_W` / `SCENE_H` | `940` / `760` | capture viewport (downscaled to 760 px wide) |
| `OUT_GIF` | `../../docs/images/demo.gif` | output path |
| `PORT` | `8011` | local server port |

This directory has its own `node_modules`; it is isolated from the Python
package and not part of the published distribution.
