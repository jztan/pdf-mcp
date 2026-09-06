# CUDA embedding: clean-machine validation of the documented path

Date: 2026-09-06. Cost: one g5.xlarge on-demand for ~30 min (~$0.50).
Validates PR #39 (`PDF_MCP_CUDA`, merged as 5417de7) and the recipe in
`docs/configuration.md` "GPU embedding (NVIDIA CUDA)" on a machine
nobody had tuned first.

## Setup

| | |
|---|---|
| instance | AWS g5.xlarge, us-east-1b (4 vCPU = 2 physical cores, NVIDIA A10G 24 GB) |
| AMI | Deep Learning Base OSS Nvidia Driver GPU AMI, Ubuntu 24.04 (`ami-0f04f99c6ee42ec72`) |
| driver | 595.91.07, CUDA 13.2 capable |
| Python / pdf-mcp | 3.12.3 / `pip install git+...@develop` at 19bc109 (reports 3.1.0) |
| onnxruntime | 1.29.0 (CPU wheel), then onnxruntime-gpu 1.29.0 per the recipe |
| fastembed | 0.8.0, model `BAAI/bge-small-en-v1.5` |
| workload | 790 sub-page units (`page_embedding_units`) from the 6 PDFs in `pages/corpus/`, plus `gao-cloud.pdf` alone (12 pages) for parity with the PR's own table |
| method | `scripts/run.sh` walks the docs verbatim; `scripts/measure.py` times `pdf_mcp.embedder.encode`, records the providers the session reports, every warning, and cosine against the CPU vectors |

## Timings

| phase | env | providers | corpus 790 units | gao-cloud 12 pp | model load | cosine vs CPU (min) |
|---|---|---|---|---|---|---|
| 1. CPU baseline, plain install | unset | CPU | 630.9 s (1.3 u/s) | 34.5 s | 5.1 s | ref |
| 2. CUDA asked, plain install | 1 | CPU, **warned** | 630.8 s | 34.5 s | 3.9 s | 1.000000 |
| 3a. CUDA 13 recipe | 1 | CUDA | **1.00 s** (789 u/s) | **0.05 s** | 12.4 s | 0.999992 |
| 3b. after recipe, env unset | unset | CUDA | 1.07 s | 0.05 s | 15.0 s | 0.999992 |
| 4a. CUDA 12 recipe on top | 1 | CUDA | 1.00 s | 0.05 s | 1.3 s | 0.999992 |
| 5. cuDNN wheel removed | 1 | CUDA | 1.00 s | 0.05 s | 1.1 s | 0.999992 |
| 6. second GPU run | 1 | CUDA | 1.00 s | 0.05 s | 1.0 s | 0.999992 |

Raw per-phase JSON in `raw/*.json`; phase timestamps in `raw/monitor-phases.log`.

Reference points from elsewhere: the same 790 units on the maintainer's
Apple Silicon Mac take 24.7 s on CPU (CoreML re-measure, same day); the PR
author reported gao-cloud 39.4 s CPU -> 1.8 s GPU on an RTX 3090 (Windows,
CUDA 13). The 0.05 s here is a warmed second pass over 12 pages; the
author's 1.8 s includes the tool's whole warm path.

**Read the multipliers carefully.** 630x is against this instance's own
CPU, which is 25x slower than the Mac on this workload (2 physical
Xeon cores, onnxruntime's thread pool sized to physical cores). The
number a Mac user would see from moving to an A10G is ~25x on the whole
corpus. The PR's "one to two orders of magnitude" claim holds on both
readings.

Vectors match the CPU path (min cosine 0.999992 over 790 units, mean
0.999999): the GPU is a speed choice, the cache is shared.

Model load is the one cost: 12.4 s on the first CUDA session of the
process (phase 3a) and 15.0 s (3b) versus 5.1 s on CPU. Later CUDA
sessions load in about 1 s (4a, 5, 6), so the 12 to 15 s is a first-use
kernel/JIT cost, not a per-session cost. A server that embeds a handful
of pages pays this once and never earns it back.

## Findings beyond the timings

1. **The recipe works on a clean machine as written.** Both CUDA-series
   recipes installed without resolver conflict in under a minute each
   and produced a CUDA session on the first try. (Phase 4 layers the
   CUDA 12 recipe on top of 13, which is not what a user would do, but
   it did not break either.)

2. **The loud-fallback contract works on a plain install.** Phase 2:
   `PDF_MCP_CUDA=1` with the CPU wheel warns with onnxruntime's own
   reason ("Provider CUDAExecutionProvider is not available") and runs
   on CPU.

3. **"Unset means CPU" is not always true, and the docs said it was.**
   Phase 3b: env unset after the recipe still ran on CUDA. fastembed's
   constructor defaults to `cuda=Device.AUTO`, and this AMI ships a
   system-wide CUDA toolkit on `LD_LIBRARY_PATH`, so onnxruntime-gpu
   found the runtime by itself. Experiment D in `raw/clean.log` shows
   the other side: with the system CUDA hidden and only the pip wheels
   present, unset gives CPU (fastembed's own RuntimeWarning fires,
   because AUTO tried and failed). So on the docs' own recipe (pip
   wheels only) the statement holds; on any box with a system CUDA
   install it does not. The PR's tests mock the constructor and cannot
   see this. Docs need the qualification; a `PDF_MCP_CUDA=0` "force
   CPU" spelling would make it enforceable.

4. **The pip-wheel preload path is real and needed.** Experiment B
   (system CUDA hidden, `LD_LIBRARY_PATH` unset): `PDF_MCP_CUDA=1` still
   got CUDA, and `/proc/self/maps` shows every CUDA library mapped from
   `site-packages/nvidia/`, none from the system. Experiment D (same
   box, env unset, so no preload): CUDA provider creation fails. The
   `_preload_cuda_runtime` dlopen chain is what makes the wheels-only
   install work on Linux.

5. **A broken runtime crashes instead of warning.** Experiment C: cuDNN
   wheel removed, system CUDA hidden, `PDF_MCP_CUDA=1`. The session
   was created and reported CUDA, so the provider check passed; the
   first `encode` then raised `NotImplemented` from the Attention
   kernel. The "warn and fall back" contract covers provider creation
   only, not kernel execution. Phase 5 in the main run looked fine only
   because the AMI's system cuDNN was still reachable. Fix candidate: a
   one-string probe encode inside `_cuda_model` after session creation,
   catching any exception into the same warn-and-fallback.

6. **Noise on stderr.** onnxruntime 1.29 prints "No registered plugin
   EP device found for 'CUDAExecutionProvider' with device_id=0" and a
   "some nodes were not assigned" warning on every CUDA session. Both
   are benign here (the session reports CUDA and runs at GPU speed).
   Worth a line in the docs so users do not read them as failure.

## What was lost

`run.log` (the full stdout of `run.sh`, including pip's install output)
was not copied off the instance before termination. The per-phase JSON
files, the phase timeline, the final `pip freeze`, and the second
experiment's complete log were. The exact package set the CUDA 13
recipe installed was read during the run and is reproduced here from
that reading: onnxruntime-gpu 1.29.0, fastembed-gpu 0.8.0,
nvidia-cublas 13.6.1.10, nvidia-cuda-runtime 13.3.29, nvidia-cufft
12.3.0.29, nvidia-curand 10.4.3.29, nvidia-cudnn-cu13 9.25.1.1 (plus
nvrtc and nvjitlink pulled as dependencies). The CUDA 12 set is in
`raw/pip-freeze-final.txt`.

## Follow-ups

- Docs: qualify "unset = CPU"; mention the two benign onnxruntime
  warnings; state the first-session load cost.
- Code: probe encode in `_cuda_model` (finding 5); `PDF_MCP_CUDA=0`
  force-CPU spelling (finding 3). Both small, both need the normal
  CI flow.
