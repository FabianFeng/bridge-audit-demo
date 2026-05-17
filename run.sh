#!/usr/bin/env bash
# 一键启动脚本，支持两种 LLM 后端
#
# 用法：
#   ./run.sh                                    # 默认走本地 Claude Code（claude-sonnet-4-6）
#   LLM_BACKEND=openai ./run.sh                 # 走 OpenAI 兼容端点（默认 http://127.0.0.1:8000/v1）
#   LLM_BACKEND=openai \
#     LLM_BASE_URL=https://your-gpu:8000/v1 \
#     LLM_MODEL=nvidia/Gemma-4-31B-IT-NVFP4 \
#     ./run.sh
#
set -e
cd "$(dirname "$0")"

# 1. venv
if [ ! -d ".venv" ]; then
  echo "[1/3] 创建虚拟环境…"
  python3 -m venv .venv
fi
source .venv/bin/activate

# 2. 装依赖
echo "[2/3] 安装依赖…"
pip install -q -r requirements.txt
pip install -q openai >/dev/null 2>&1

# 3. 后端检查
LLM_BACKEND="${LLM_BACKEND:-claude}"
case "$LLM_BACKEND" in
  claude)
    if ! command -v claude >/dev/null 2>&1; then
      echo "⚠️  未检测到 claude CLI，请先安装 Claude Code（或改用 LLM_BACKEND=openai）"
      exit 1
    fi
    echo "[3/3] 启动 → http://127.0.0.1:8000  | 后端: Claude Code"
    ;;
  openai)
    LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8000/v1}"
    # 自检 endpoint
    if ! curl -sf -m 3 "${LLM_BASE_URL%/v1}/v1/models" >/dev/null 2>&1; then
      echo "⚠️  无法访问 LLM endpoint: $LLM_BASE_URL"
      echo "    检查 vLLM 是否已启动，或导出 LLM_BASE_URL 后重试。"
      exit 1
    fi
    echo "[3/3] 启动 → http://127.0.0.1:8000  | 后端: OpenAI 兼容 ($LLM_BASE_URL)"
    ;;
  *)
    echo "未知 LLM_BACKEND: $LLM_BACKEND（应为 claude / openai）"
    exit 1
    ;;
esac

echo ""
export LLM_BACKEND LLM_BASE_URL LLM_MODEL LLM_API_KEY
exec python app.py
