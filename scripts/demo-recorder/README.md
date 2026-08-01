# Demo GIF recorder

Regenerates `docs/images/demo.gif` (shown at the top of the README) by driving
the live browser demo in `pages/index.html` and encoding an optimized GIF.

The journey: hero search-scan animation → load the 6-PDF sample corpus →
warm sweep (rows flip queued → warmed) → overview triage cards → type a
suggested cross-document query → matches sweep the per-doc page grids →
open the top hit → the "what just happened" receipt (pages across six
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

The browser captures the full desktop layout at a larger viewport
(`SCENE_W`×`SCENE_H`, default 1056×760) and ffmpeg downscales to 760 px wide,
so each frame shows the whole scene (corpus card, grids, search, receipt)
instead of a zoomed-in crop. Section-to-section moves are eased camera pans
(not instant scroll jumps). Output is 760 px wide, 16 fps, ~6 MB.

## Tunables (env vars)

| var | default | effect |
| --- | --- | --- |
| `FPS` | `16` | frame rate (higher = smoother, larger) |
| `MAXCOLORS` | `144` | GIF palette size |
| `LOSSY` | `80` | gifsicle lossy level (higher = smaller, more artifacts) |
| `SCENE_W` / `SCENE_H` | `1056` / `760` | capture viewport (downscaled to 760 px wide) |
| `OUT_GIF` | `../../docs/images/demo.gif` | output path |
| `PORT` | `8011` | local server port |

This directory has its own `node_modules`; it is isolated from the Python
package and not part of the published distribution.
