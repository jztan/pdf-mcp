# Demo GIF recorder

Regenerates `docs/images/demo.gif` (shown at the top of the README) by driving
the live browser demo in `pages/index.html` and encoding an optimized GIF.

The journey: hero search-scan animation → load the 216-page sample contract →
type the first suggested query → matches sweep the page grid → open a matched
page inline with its token count.

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
so each frame shows the whole scene (doc card, grid, search, legend) instead of
a zoomed-in crop. Section-to-section moves are eased camera pans (not instant
scroll jumps). Output is 760 px wide, 16 fps, ~3.7 MB.

## Tunables (env vars)

| var | default | effect |
| --- | --- | --- |
| `FPS` | `16` | frame rate (higher = smoother, larger) |
| `MAXCOLORS` | `144` | GIF palette size |
| `LOSSY` | `120` | gifsicle lossy level (higher = smaller, more artifacts) |
| `SCENE_W` / `SCENE_H` | `1056` / `760` | capture viewport (downscaled to 760 px wide) |
| `OUT_GIF` | `../../docs/images/demo.gif` | output path |
| `PORT` | `8011` | local server port |

This directory has its own `node_modules`; it is isolated from the Python
package and not part of the published distribution.
