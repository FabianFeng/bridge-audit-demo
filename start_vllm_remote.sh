#!/usr/bin/env bash
# 在 GPU 服务器上启动 vLLM serve（用 tmux 持久化）
# 用法：./start_vllm_remote.sh
set -e

ssh gpu "tmux kill-session -t vllm 2>/dev/null || true"

ssh gpu "tmux new-session -d -s vllm 'source /data/venvs/vllm/bin/activate && \
  vllm serve /data/models/gemma-4-31b-nvfp4 \
    --quantization modelopt \
    --tensor-parallel-size 2 \
    --max-model-len 131072 \
    --enable-prefix-caching \
    --host 0.0.0.0 \
    --port 8000 \
    2>&1 | tee /data/vllm.log'"

echo "vLLM 已在 GPU 端 tmux:vllm 启动"
echo "查看日志: ssh gpu 'tail -f /data/vllm.log'"
echo "查看 tmux: ssh gpu 'tmux attach -t vllm'   (Ctrl+B D 退出但保留)"
echo ""
echo "等待模型加载（约 1-2 分钟）..."
echo "看到 'Uvicorn running on http://0.0.0.0:8000' 就算成功"
