#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT}/.venv"
JETSON_TORCH_WHEEL="https://pypi.jetson-ai-lab.io/jp6/cu126/+f/62a/1beee9f2f1470/torch-2.8.0-cp310-cp310-linux_aarch64.whl"
JETSON_TORCHVISION_WHEEL="https://pypi.jetson-ai-lab.io/jp6/cu126/+f/907/c4c1933789645/torchvision-0.23.0-cp310-cp310-linux_aarch64.whl"

export PATH="${HOME}/.local/bin:${PATH}"

if ! python3 -m venv --system-site-packages "${VENV_DIR}" 2>/dev/null; then
  if ! python3 -m virtualenv --system-site-packages -p python3 "${VENV_DIR}" 2>/dev/null; then
    TMP_GET_PIP="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "${TMP_GET_PIP}"
    elif command -v wget >/dev/null 2>&1; then
      wget -qO "${TMP_GET_PIP}" https://bootstrap.pypa.io/get-pip.py
    else
      echo "Need curl or wget to bootstrap pip/virtualenv." >&2
      exit 1
    fi
    python3 "${TMP_GET_PIP}" --user
    rm -f "${TMP_GET_PIP}"
    python3 -m pip install --user virtualenv
    python3 -m virtualenv --system-site-packages -p python3 "${VENV_DIR}"
  fi
fi

source "${VENV_DIR}/bin/activate"
export PYTHONNOUSERSITE=1

python -m pip install --upgrade pip wheel setuptools
if [[ -f /etc/nv_tegra_release ]]; then
  python -m pip install --force-reinstall --no-cache-dir --no-deps \
    "${JETSON_TORCH_WHEEL}" \
    "${JETSON_TORCHVISION_WHEEL}"
else
  python -m pip install torch==2.5.1 torchvision==0.20.1
fi
python -m pip install \
  typing-extensions filelock fsspec sympy networkx jinja2 \
  psutil polars ultralytics-thop rich scipy==1.11.4 \
  exceptiongroup threadpoolctl
python -m pip install -r "${ROOT}/requirements.txt"

echo
echo "Python environment created at ${VENV_DIR}"
echo "System ffmpeg/ffprobe is recommended when available."
echo "Without ffprobe, the bundle falls back to OpenCV timing; without ffmpeg, fisheye output stays video-only."
