#!/bin/bash
# Install vLLM + torch for the B02 PoC.
#
# Targets the lab GPU driver (CUDA 12.8 on T4), so we install
# vllm 0.10.2 with torch 2.8 (cu128). The default vllm pin from
# PyPI (0.24.x) bundles torch 2.11 cu13 and would fail with
# "NVIDIA driver too old" against our 12.8 driver.
set -e

VENV_DIR="$HOME/B02/poc/.venv"
SCRIPT_DIR="$HOME/B02/poc/state_extraction"

cd "$SCRIPT_DIR"
mkdir -p logs

echo "[$(date +%T)] venv target: $VENV_DIR"
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "[$(date +%T)] creating venv ..."
    python3 -m venv "$VENV_DIR"
else
    echo "[$(date +%T)] venv already exists, reusing"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "[$(date +%T)] python: $(which python)"
echo "[$(date +%T)] pip:    $(which pip)"

echo "[$(date +%T)] upgrading pip / wheel / setuptools ..."
pip install --upgrade pip wheel setuptools 2>&1 | tee logs/install_pip.log | tail -5

echo "[$(date +%T)] installing torch 2.8 (cu128) for driver 12.8 ..."
pip install --index-url https://download.pytorch.org/whl/cu128 \
    torch==2.8.0+cu128 \
    torchaudio==2.8.0+cu128 \
    torchvision==0.23.0+cu128 2>&1 | tee logs/install_torch_cu128.log | tail -10

echo "[$(date +%T)] installing vllm==0.10.2 ..."
pip install "vllm==0.10.2" 2>&1 | tee logs/install_vllm.log | tail -10

echo "[$(date +%T)] installing extra helpers (modelscope, orjson, msgpack) ..."
pip install modelscope orjson msgpack zstandard 2>&1 | tee logs/install_helpers.log | tail -5

echo "[$(date +%T)] verifying ..."
python -c "import torch, vllm; print('torch:', torch.__version__, ' cuda:', torch.version.cuda, ' cuda_avail:', torch.cuda.is_available()); print('vllm:', vllm.__version__)" \
    | tee logs/install_verify.log

echo "[$(date +%T)] done"
