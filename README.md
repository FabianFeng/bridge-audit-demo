# 桥梁工程文件智能审校系统 Demo

基于大模型的桥梁工程文档自动审校工具，支持 PDF / Word / Markdown 文件上传，实时流式输出审校问题。

## 功能特点

- 上传工程文档（PDF、Word、TXT、Markdown）
- PDF 文件通过 [MinerU](https://github.com/opendatalab/MinerU) OCR 转为 Markdown
- 调用大模型按 12 条专业规则逐条审查
- 发现问题实时 SSE 推流，前端卡片逐条展示
- 底部计时器实时记录各阶段耗时

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI + SSE |
| OCR | MinerU v3 (pipeline + OCR 模式) |
| LLM | vLLM (Gemma-4-31B) 或 Claude Agent SDK |
| 前端 | 原生 HTML / CSS / JS（无框架） |

## 快速启动

```bash
# 安装依赖
pip install -r requirements.txt

# Claude 后端（本地 Claude Code）
LLM_BACKEND=claude uvicorn app:app --host 0.0.0.0 --port 8088

# OpenAI 兼容后端（vLLM / OpenRouter 等）
LLM_BACKEND=openai \
OPENAI_BASE_URL=http://127.0.0.1:8000/v1 \
OPENAI_MODEL=gemma-4-31b \
uvicorn app:app --host 0.0.0.0 --port 8088
```

或使用封装好的启动脚本：

```bash
bash run.sh
```

## 审校规则

见 `rules.py`，涵盖：标准规范引用、图表编号、单位符号、设计参数、施工工艺等 12 个维度。

## 目录结构

```
.
├── app.py          # FastAPI 主服务
├── index.html      # 前端页面
├── rules.py        # 审校规则定义
├── run.sh          # 启动脚本
└── requirements.txt
```
