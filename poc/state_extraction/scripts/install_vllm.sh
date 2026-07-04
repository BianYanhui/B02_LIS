#!/bin/bash
# Install vLLM inside ~/B02/poc/.venv (canonical location)
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

echo "[$(date +%T)] installing vllm (this can take 5-15 min) ..."
pip install vllm 2>&1 | tee logs/install_vllm.log

echo "[$(date +%T)] verifying import ..."
python -c "import vllm; print('vllm version:', vllm.__version__)" 2>&1 | tee logs/install_verify.log

echo "[$(date +%T)] done"
