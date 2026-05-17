# 桥梁工程文件智能审校系统 — 使用与部署说明

> 面向团队内部，版本 2025-05

---

## 一、这是什么

一套针对**桥梁工程设计文件**的 AI 自动审校工具。

上传施工图设计说明、设计总说明等文件（PDF / Word / Markdown），系统会逐条输出问题，指出：

- 引用了哪条**已废止/被替代**的规范
- 设计参数（设计使用年限、混凝土标号、钢材牌号、桩长、支座选型…）是否合规
- 文档格式问题（重复行、数字粘连、用词不规范等）

结果**实时流式**输出，每发现一个问题就立即显示，不用等全文处理完。

**演示地址（GPU 服务器）：** http://218.22.75.175:8088

**代码仓库：** https://github.com/FabianFeng/bridge-audit-demo

---

## 二、审校规则（12 条）

| # | 类别 | 检查内容 | 规范依据 |
|---|------|---------|---------|
| 1 | 重复条目 | 连续出现的完全相同行 | 文件自洽性 |
| 2 | 规范引用 | 已被替代或废止的标准（如 JTG D63-2005、JT/T 722-2008 等） | 各规范版本公告 |
| 3 | 设计标准 | 主体结构设计使用年限：大桥/特大桥须≥100年 | JTG 2120-2020 表3.3.1 |
| 4 | 取值合规 | 环境类别只有Ⅰ～Ⅴ五类，不含A/B细分级 | JTG/T 3310-2019 表3.0.6 |
| 5 | 材料牌号 | 主梁不应使用Q235，应用Q345/Q355/Q370q等 | JTG D64-2015 |
| 6 | 材料等级 | 钢混组合梁桥面板混凝土≥C40 | JTG/T D64-01-2015 |
| 7 | 耐久性 | 钢结构防腐涂层保护年限≥20年 | JT/T 722-2023 |
| 8 | 构造选型 | T梁不应配盆式/球型支座，应用板式橡胶支座 | JTG D60-2015 |
| 9 | 用词规范 | "大于"与"不小于"的准确区分 | GB/T 1.1-2020 |
| 10 | 格式问题 | 8位以上连续数字（OCR 表格串行） | 文件自洽性 |
| 11 | 参数取值 | 竖向温度梯度 T2 与铺装厚度的对应关系 | JTG D60-2015 表4.3.10-2 |
| 12 | 自洽性 | 不同章节"最小有效桩长"表述是否一致 | JTG 3363-2019 |

**规则之外**，LLM 会做全文语义审查，发现规则未覆盖的其它问题（笔误、桥名不一致、章节逻辑矛盾等）。

---

## 三、快速使用（演示站）

### 方式 A：使用样例文件（推荐，秒出结果）

1. 打开 http://218.22.75.175:8088
2. 页面中部"或直接体验样例文件"区域，点击任一样例卡片
3. 自动加载，点击**开始审校**

### 方式 B：上传自己的文件

1. 点击上传区，选择文件（支持 PDF / Word / TXT / Markdown）
2. 等待上传完成
3. 点击**开始审校**

> **注意**：服务器带宽有限，大 PDF（>10MB）上传可能较慢，建议先压缩或使用 Word/Markdown 格式。
> PDF 会自动经过 MinerU OCR，额外耗时约 30–60 秒（处理期间 GPU 卸载 vLLM，OCR 结束后重新加载）。

### 查看结果

- 每个审校发现以卡片形式实时展示
- 卡片颜色：红色（严重）/ 橙色（一般）/ 蓝色（轻微）
- 每张卡片包含：问题类别、位置、原文、规范依据、修改建议
- 页面底部计时条可展开查看各阶段耗时

---

## 四、本地部署

### 环境要求

- Python 3.10+
- Claude Code CLI（`claude` 命令可用）**或** vLLM / 任意 OpenAI 兼容接口

### 步骤

```bash
git clone https://github.com/FabianFeng/bridge-audit-demo.git
cd bridge-audit-demo

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动（默认走本地 Claude Code）
./run.sh
```

浏览器打开 http://127.0.0.1:8088

### 切换 LLM 后端

**使用 vLLM / OpenRouter 等 OpenAI 兼容接口：**

```bash
LLM_BACKEND=openai \
LLM_BASE_URL=http://127.0.0.1:8000/v1 \
LLM_MODEL=你的模型名 \
./run.sh
```

---

## 五、GPU 服务器部署说明

服务器：218.22.75.175，系统 Ubuntu，双 RTX 5090

### 当前运行状态

| 服务 | 位置 | 端口 |
|------|------|------|
| FastAPI 主服务 | tmux: `demo` | 8088（公网可访问） |
| vLLM（Gemma-4-31B） | tmux: `vllm` | 8000（仅本机） |
| MinerU | 独立 venv：`/data/venvs/mineru` | — |

### 查看/重启服务

```bash
# 查看正在运行的 tmux 会话
tmux ls

# 进入主服务日志
tmux attach -t demo

# 进入 vLLM 日志
tmux attach -t vllm

# 退出 tmux 但不关闭（重要！）
Ctrl+B 然后按 D
```

### 重启主服务

```bash
tmux kill-session -t demo
tmux new-session -d -s demo -x 220 -y 50
tmux send-keys -t demo "cd /data/demo && LLM_BACKEND=openai LLM_BASE_URL=http://127.0.0.1:8000/v1 LLM_MODEL=Qwen/Qwen2.5-72B-Instruct python app.py" Enter
```

### 重启 vLLM

```bash
tmux kill-session -t vllm
bash /data/demo/start_vllm_remote.sh
```

### 添加样例文件

将预处理好的 `.md` 文件放入 `/data/demo/samples/`，在 `app.py` 的 `SAMPLES_DISPLAY` 字典中添加对应的中文标题：

```python
SAMPLES_DISPLAY = {
    "文件名（不含.md）": "显示给用户的标题",
}
```

重启服务后前端自动加载。

---

## 六、项目结构

```
bridge-audit-demo/
├── app.py            # FastAPI 后端，含 OCR pipeline、SSE 流式输出、样例管理
├── index.html        # 前端（纯 HTML/CSS/JS，无框架）
├── rules.py          # 12 条领域规则 + 规范库（含现行/废止版本列表）
├── run.sh            # 一键启动脚本，支持 Claude / OpenAI 两种后端
├── start_vllm_remote.sh  # GPU 服务器上启动 vLLM 的脚本
├── requirements.txt  # Python 依赖
└── samples/          # 样例 Markdown 文件（服务器本地，不入库）
```

### 关键流程

```
用户上传文件
    │
    ├─ .md / .txt    → 直接送 LLM
    ├─ .docx / .doc  → python-docx 提取文本 → 送 LLM
    └─ .pdf
           │
           ├─ 停止 vLLM（释放 GPU 显存）
           ├─ MinerU OCR（MINERU_MODEL_SOURCE=modelscope，pipeline 模式）
           ├─ 重启 vLLM（等待就绪）
           └─ 读取 {outdir}/{stem}/ocr/{stem}.md → 送 LLM

LLM 审校
    │
    ├─ 规则扫描（rules.py quick_scan）
    └─ LLM 全文审查（按 prompt 中的规范要求）
           │
           └─ SSE 流式推送 → 前端实时显示卡片
```

---

## 七、常见问题

**Q：PDF 上传后一直转圈不动？**
A：PDF 需要先做 OCR（MinerU），同时要切换 GPU 显存（卸载 vLLM → OCR → 重载 vLLM），整个过程约 2–4 分钟。底部计时条展开后可以看到当前在哪一步。

**Q：MinerU 报 LocalEntryNotFoundError？**
A：HuggingFace 在中国无法访问。需要先用 ModelScope 下载模型：
```bash
source /data/venvs/mineru/bin/activate
mineru-models-download -s modelscope -m pipeline
```

**Q：vLLM 报 CUDA out of memory？**
A：OCR 前必须停掉 vLLM。`app.py` 中的 `_vllm_stop()` 会处理，正常流程不会有这个问题。如果手动启动 MinerU，需要先 `tmux kill-session -t vllm`。

**Q：想换一个大模型？**
A：修改 `start_vllm_remote.sh` 中的模型路径，重启 vLLM 即可。`LLM_MODEL` 环境变量跟着改。

**Q：想增加审校规则？**
A：在 `rules.py` 中参照现有规则写一个函数，返回 `list[Finding]`，然后加入 `ALL_RULES` 列表。也可以只修改 `app.py` 中的 LLM prompt，让模型关注更多维度。

---

## 八、联系

有问题找 Fabian。代码在 GitHub：https://github.com/FabianFeng/bridge-audit-demo
