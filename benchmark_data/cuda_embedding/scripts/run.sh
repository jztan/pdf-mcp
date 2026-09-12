#!/bin/bash
# Clean-machine test of docs/configuration.md "GPU embedding (NVIDIA CUDA)".
set -u
cd ~ && mkdir -p bench && cd bench
log(){ echo; echo "=== $(date +%T) $*"; }
log "driver"; nvidia-smi --query-gpu=name,driver_version --format=csv,noheader; python3 --version
log "clone corpus (pages/corpus)"; git clone -q --depth 1 -b develop https://github.com/jztan/pdf-mcp.git repo && ls repo/pages/corpus
log "fresh venv + install develop"; python3 -m venv venv && . venv/bin/activate && pip install -q -U pip && pip install -q "git+https://github.com/jztan/pdf-mcp.git@develop" && pip list 2>/dev/null | grep -iE "^onnxruntime|^fastembed|^pdf-mcp"
M=~/measure.py; C=repo/pages/corpus
log "1. CPU baseline (PDF_MCP_CUDA unset)"; env -u PDF_MCP_CUDA python $M cpu $C cpu.json
log "2. PDF_MCP_CUDA=1 BEFORE the recipe (expect warning, CPU)"; PDF_MCP_CUDA=1 python $M cuda_before_recipe $C before.json cpu.npy
log "3. recipe, CUDA 13 (verbatim from docs/configuration.md)"
pip uninstall -y onnxruntime
pip install fastembed-gpu
pip install nvidia-cublas nvidia-cuda-runtime nvidia-cufft nvidia-curand nvidia-cudnn-cu13
pip list 2>/dev/null | grep -iE "^onnxruntime|^fastembed|^nvidia"
log "3a. PDF_MCP_CUDA=1 after CUDA 13 recipe"; PDF_MCP_CUDA=1 python $M cuda13 $C cuda13.json cpu.npy
log "3b. PDF_MCP_CUDA unset after recipe (must still be CPU, no warning)"; env -u PDF_MCP_CUDA python $M unset_after_recipe $C unset13.json cpu.npy
log "4. recipe, CUDA 12 (verbatim), on top"
pip uninstall -y onnxruntime
pip install fastembed-gpu
pip install --force-reinstall --no-deps onnxruntime-gpu --index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cudnn-cu12
pip list 2>/dev/null | grep -iE "^onnxruntime|^nvidia"
log "4a. PDF_MCP_CUDA=1 after CUDA 12 recipe"; PDF_MCP_CUDA=1 python $M cuda12 $C cuda12.json cpu.npy
log "5. negative: runtime removed, PDF_MCP_CUDA=1 (expect warning + CPU)"
pip uninstall -y -q nvidia-cudnn-cu12 nvidia-cudnn-cu13 2>/dev/null
PDF_MCP_CUDA=1 python $M cuda_runtime_removed $C removed.json cpu.npy
log "6. warm-clock check: second GPU run, cache-cold model already downloaded"
pip install -q nvidia-cudnn-cu12; PDF_MCP_CUDA=1 python $M cuda12_rerun $C cuda12b.json cpu.npy
log DONE; cat *.json
