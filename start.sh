#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3.10+。" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "提示：未检测到 ffmpeg。最佳画质的音视频合并将不可用。" >&2
  echo "      Debian/Ubuntu: 请安装 ffmpeg 软件包" >&2
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt

export DOWNLOAD_DIR="${DOWNLOAD_DIR:-$(pwd)/downloads}"
mkdir -p "$DOWNLOAD_DIR" data

echo "流影已启动 → http://127.0.0.1:8787"
exec uvicorn app.main:app --host 0.0.0.0 --port 8787
