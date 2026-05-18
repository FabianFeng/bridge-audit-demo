"""
桥梁设计文档审核 demo
- 上传 .doc / .docx / .txt / .md / .pdf
- 后端：规则层（正则）+ LLM 层（OpenAI 兼容端点，本地 vLLM 部署 Gemma-31B）
- 前端通过 SSE 实时收到 findings
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from threading import Thread

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from rules import quick_scan, standards_index

# 配置日志：写入文件和控制台
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/data/demo_debug.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


app = FastAPI()


# ---------- LLM 后端配置（OpenAI 兼容端点 → 本地 vLLM）----------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "dummy")  # vLLM 不校验
LLM_MODEL = os.getenv("LLM_MODEL", "nvidia/Gemma-4-31B-IT-NVFP4")

from openai import AsyncOpenAI

print(f"[LLM 后端] model={LLM_MODEL} | url={LLM_BASE_URL}")

_openai_client = None
def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=300)
    return _openai_client


# ---------- vLLM 控制 ----------

_VLLM_CMD = (
    "source /data/venvs/vllm/bin/activate && "
    "vllm serve /data/models/gemma-4-31b-nvfp4 "
    "--quantization modelopt --tensor-parallel-size 2 "
    "--max-model-len 131072 --max-num-batched-tokens 16384 "
    "--enable-prefix-caching --host 127.0.0.1 --port 8000 "
    "2>&1 | tee /data/vllm.log"
)
_MINERU_BIN = "/data/venvs/mineru/bin/mineru"


def _vllm_stop():
    """彻底停止 vLLM。
    注意：`vllm serve` 用多进程方式起 worker（命令名是 VLLM::Worker_TP0/TP1），
    单独 pkill 'vllm serve' 会留下占着 GPU 显存的 worker，导致后续启动 OOM。
    所以这里要同时清理两类进程，并等 GPU 内存释放。
    """
    logger.info("Stopping vLLM (serve + workers)...")
    subprocess.run(["pkill", "-9", "-f", "vllm serve"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "VLLM"], capture_output=True)  # worker 子进程
    # 等 GPU 内存释放完
    for _ in range(20):
        time.sleep(1)
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            used = max(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
            if used < 500:  # < 500 MiB 表示已释放
                logger.info(f"GPU memory released (used={used} MiB)")
                return
        except Exception:
            pass
    logger.warning("GPU memory not released after 20s, continuing anyway")


def _vllm_start_and_wait(timeout: int = 300):
    logger.info("Starting vLLM...")
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "0,1"
    # 把 venv 的 bin 加到 PATH 头部，等同于 source activate
    env["PATH"] = "/data/venvs/vllm/bin:" + env.get("PATH", "")
    env["VIRTUAL_ENV"] = "/data/venvs/vllm"

    # 用 vllm 绝对路径启动（避免 dash 不支持 source 的问题）
    # served-model-name 必须与 LLM_MODEL 一致，否则 demo 调用会因 model id 不匹配而 404
    cmd = [
        "/data/venvs/vllm/bin/vllm", "serve",
        "/data/models/gemma-4-31b-nvfp4",
        "--served-model-name", LLM_MODEL,
        "--quantization", "modelopt",
        "--tensor-parallel-size", "2",
        "--max-model-len", "131072",
        "--max-num-batched-tokens", "4096",
        "--host", "127.0.0.1", "--port", "8000",
    ]
    # 输出重定向到 vllm.log，便于排查
    vllm_log = open("/data/vllm.log", "ab", buffering=0)
    vllm_log.write(f"\n=== vLLM start at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    proc = subprocess.Popen(
        cmd, env=env, stdout=vllm_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info(f"vLLM process started with PID {proc.pid}")

    # 轮询直到 vLLM 就绪
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            response = urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=3)
            logger.info(f"vLLM ready after {attempts} attempts")
            return  # 就绪
        except Exception as e:
            if attempts <= 3 or attempts % 10 == 0:
                logger.debug(f"vLLM not ready yet (attempt {attempts}): {type(e).__name__}")
            time.sleep(5)

    logger.error(f"vLLM startup timeout after {attempts} attempts")
    raise RuntimeError("vLLM 重启超时，请手动检查日志 /data/vllm.log")


def _pdf_to_markdown(pdf_path: Path) -> str:
    """MinerU OCR（不停止 vLLM，共享 GPU 显存）
    """
    if not Path(_MINERU_BIN).exists():
        raise RuntimeError(f"MinerU 未安装：{_MINERU_BIN}")

    out_dir = pdf_path.parent / (pdf_path.stem + "_mu")
    out_dir.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "modelscope"
    env["CUDA_VISIBLE_DEVICES"] = "0,1"
    r = subprocess.run(
        [_MINERU_BIN, "-p", str(pdf_path), "-o", str(out_dir),
         "--backend", "pipeline", "--method", "ocr", "--lang", "ch"],
        capture_output=True, text=True, timeout=1800, env=env,
    )
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-600:]
        raise RuntimeError(f"MinerU 退出码 {r.returncode}：{tail}")

    # 优先按 MinerU 标准输出路径找
    expected = out_dir / pdf_path.stem / "ocr" / f"{pdf_path.stem}.md"
    if expected.exists():
        return expected.read_text(encoding="utf-8", errors="ignore")
    # 兜底：递归找最大的 .md
    md_files = sorted(out_dir.rglob("*.md"), key=lambda p: p.stat().st_size, reverse=True)
    if not md_files:
        raise RuntimeError(f"MinerU 未生成 markdown，检查 {out_dir}")
    return md_files[0].read_text(encoding="utf-8", errors="ignore")


# ---------- 文档解析 ----------

def _textutil_to_html(path: Path) -> str:
    """用 macOS textutil 把 .doc/.docx 转 HTML"""
    if not shutil.which("textutil"):
        raise HTTPException(400, "需要 macOS 的 textutil（或先转成 .docx）")
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        out = tmp.name
    subprocess.run(
        ["textutil", "-convert", "html", "-encoding", "UTF-8", "-output", out, str(path)],
        check=True, capture_output=True,
    )
    html = Path(out).read_text(encoding="utf-8", errors="ignore")
    Path(out).unlink(missing_ok=True)
    return html


def parse_doc(path: Path) -> dict:
    """返回 {html, text, paragraphs}
    - html: 注入了 data-line 的 HTML，前端 iframe 渲染
    - text: 纯文本（每段一行），后端规则与 LLM 都用这个
    - paragraphs: 同上但 list 形式，可索引
    - 行号 = 段号，HTML 与文本完全对齐
    """
    suffix = path.suffix.lower()
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
        paragraphs = text.split("\n")
        body = "\n".join(
            f'<p data-line="{i+1}">{_escape(p)}</p>' for i, p in enumerate(paragraphs)
        )
        return {
            "html": f"<html><body><pre style='font-family:inherit'>{body}</pre></body></html>",
            "text": text,
            "paragraphs": paragraphs,
        }

    if suffix == ".md":
        import markdown as md
        raw_md = path.read_text(encoding="utf-8", errors="ignore")
        # markdown 渲染成 HTML（带表格、标题、列表）
        html_body = md.markdown(
            raw_md,
            extensions=["tables", "fenced_code", "toc"],
        )
        raw_html = f"""<html><head><style>
body {{ font-family: -apple-system, 'PingFang SC', sans-serif; max-width: 880px; margin: 0 auto; padding: 24px; line-height: 1.7; color: #2A2823; }}
h1, h2, h3, h4 {{ font-family: 'Fraunces', Georgia, serif; margin-top: 1.6em; margin-bottom: 0.6em; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #D9D2C2; padding-bottom: 8px; }}
h2 {{ font-size: 18px; }}
h3 {{ font-size: 16px; color: #6B6557; }}
table {{ border-collapse: collapse; margin: 1em 0; font-size: 13px; }}
table, th, td {{ border: 1px solid #D9D2C2; }}
th, td {{ padding: 6px 10px; }}
th {{ background: #F4F1EA; }}
img {{ max-width: 100%; opacity: 0.6; }}
code {{ background: #F4F1EA; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
</style></head><body>{html_body}</body></html>"""
        # 用 BS 解析、给段落加 data-line
        # 标题段在纯文本里加 markdown 前缀（# / ## / ### ...），便于章节识别
        # 关键：跳过包含其它叶子段落的容器节点（防止 <li><p>...</p></li> 被算两次）
        soup = BeautifulSoup(raw_html, "lxml")
        LEAF_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"]
        paragraphs = []
        for el in soup.find_all(LEAF_TAGS):
            # 如果内部还有别的叶子段落，说明 el 是容器，跳过
            if el.find(LEAF_TAGS):
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            # markdown 标题打前缀，便于规则识别
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(el.name[1])
                text = "#" * level + " " + text
            line_no = len(paragraphs) + 1
            el["data-line"] = str(line_no)
            paragraphs.append(text)
        pure_text = "\n".join(paragraphs)
        return {
            "html": str(soup),
            "text": pure_text,
            "paragraphs": paragraphs,
        }

    if suffix not in {".doc", ".docx"}:
        raise HTTPException(400, f"不支持的文件类型：{suffix}")

    raw_html = _textutil_to_html(path)
    soup = BeautifulSoup(raw_html, "lxml")

    # 找所有段落级元素，避免嵌套重复
    paragraphs: list[str] = []
    for el in soup.find_all(["p", "td", "li"]):
        # 若是 td 且内部还有 p/li，跳过容器（让内部 p 作为叶子）
        if el.name == "td" and el.find(["p", "li"]):
            continue
        text = el.get_text(" ", strip=True)
        line_no = len(paragraphs) + 1
        el["data-line"] = str(line_no)
        paragraphs.append(text)

    pure_text = "\n".join(paragraphs)
    return {
        "html": str(soup),
        "text": pure_text,
        "paragraphs": paragraphs,
    }


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# 旧接口保留：仅返回 text
def parse_doc_to_text(path: Path) -> str:
    return parse_doc(path)["text"]


# ---------- 章节切分 ----------

# 章节标题：兼容
#   ① 中文「一、」「二、」… 后接中文标题（排除「三、四级公路」等短语）
#   ② markdown 一级标题：「# 数字（不含小数点） + 空格 + 中文」，避开 `# 1.1`、`# (1)` 等子节
SECTION_PATTERN = re.compile(
    r"^("
    r"[一二三四五六七八九十百]+、(?![一二三四五六七八九十百\d]+(?:级|类|等))[^\n]{2,80}"
    r"|"
    r"#{1,3}\s+\d{1,2}\s+[一-龥][^\n]{1,80}"
    r")$",
    re.MULTILINE,
)


def split_sections(text: str) -> list[tuple[str, str, int]]:
    """返回 [(章节标题, 章节内容, 起始行号), ...]"""
    matches = list(SECTION_PATTERN.finditer(text))
    if not matches:
        return [("全文", text, 1)]
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = m.group(1).strip()
        body = text[start:end]
        line_no = text[:start].count("\n") + 1
        # 控制单段大小，太长就切
        if len(body) > 12000:
            for j in range(0, len(body), 10000):
                chunk = body[j:j + 10000]
                sections.append((f"{title} (片段 {j // 10000 + 1})", chunk, line_no))
        else:
            sections.append((title, body, line_no))
    return sections


# ---------- LLM 审读 ----------

JSON_RULES = """
【输出严格要求】
- 仅输出 JSON 数组，无任何其它文字、无 markdown 包装。
- 每项必须包含以下字段（**审校报告四段式**）：
  {
    "severity": "high|medium|low",
    "category": "类别（如 设计标准 / 材料牌号 / 规范引用 / 构造选型 / 跨章节比对 / 笔误 ...）",
    "location": "位置描述（含行号最佳）",
    "original": "原文逐字片段（≤80字，必须是文档真实出现的连续字符，用于自动定位）",
    "standard_ref": "规范依据（《XXX规范》(XXX-YYYY) 第 X.X.X 条；或上位法规、专家批复，没有就留空）",
    "required": "规范要求或上位文件要求的内容",
    "actual": "文档现状（这里写了什么）",
    "suggestion": "明确的修改建议"
  }
- JSON 字符串值中需要引用文字或编号时，**必须使用中文引号「」或书名号《》**，禁止英文双引号。
- `original` 必须是文档中真实出现过的连续字符，不要改写、不要省略号。
- 没有问题就输出 []
"""


# 抽取「设计基线」的 prompt（给前部章节用）
BASELINE_PROMPT = f"""你是公路桥梁工程设计文件审校专家。下面给你一份**公路工程施工图设计说明**的前部内容（项目概况、设计依据、设计规范、技术标准、设计基础资料、桥梁布设清单等）。

请抽取出整份文件后续应当遵循的「设计基线」，输出**紧凑的中文摘要**，结构如下（按需取舍，不要瞎编）：

==项目身份==
- 项目正式名称（如「XXX高速公路 XX 段」）
- 所在省 / 所在市 / 所在县区（重要！后续要据此查地名一致性）
- 项目类型（高速公路 / 一级公路 / 二级公路 / 改建 / 新建）
- 起讫桩号 / 路线总里程

==设计标准==
- 道路等级 / 设计速度 / 车道数 / 设计荷载
- 设计基准期 / 设计使用年限（主体结构、可更换部件分别列出）
- 抗震烈度 / 地震动峰值加速度
- 环境类别 / 护栏防撞等级 / 设计洪水频率

==几何尺寸==
- 整体式断面 / 分离式断面：桥梁全宽 / 路基宽 / 单幅宽 / 桥面净宽 / 中央分隔带 / 防撞墙

==主要材料==
- 上部结构混凝土等级（T 梁 / 箱梁 / 桥面板等分别列）
- 下部结构混凝土等级（盖梁 / 立柱 / 桩基 / 承台 / U 台等）
- 普通钢筋种类 / 钢材牌号 / 预应力钢绞线规格 / 波纹管类型

==引用规范清单==
逐条列出文档前部声明引用的规范（《名称》编号），原样保留。

==桥梁清单==
每座桥列出：桥名 / 中心桩号 / 跨径布置 / 全长 / 上部结构形式

输出要求：紧凑、准确、不要遗漏关键参数。这个基线会作为审核后续章节的对照。
"""


SYSTEM_PROMPT_BASE = f"""你是一位资深的公路工程设计文件审校专家（20 年以上公路 / 桥梁设计审查经验），正在审核一份《XX 公路工程施工图总说明》。

文档前部（项目概况、设计依据、规范清单、技术标准、桥梁布设表）抽取的「设计基线」如下，**整份文件后续章节必须与基线一致**：

{{BASELINE}}

可参考的现行 / 已替代规范库（其它规范请按你所知的现行版本判断；权威查询请参照 csres.com 等国家工程建设标准化信息网）：

{standards_index()}

【审核五大维度（客户要求）】

**① 参考规范有效性**
- 文档引用的每条规范是否为**现行有效版本**？是否引用了已废止 / 被替代的旧版本？
- 编号、年份、名称是否准确（如「JT/T 722-2008」应为「JT/T 722-2023」）

**② 涉及规范详细条文的，审查与有效规范的符合性**
- 文档中给出的具体取值（设计年限、混凝土等级、钢材牌号、防腐年限等）是否符合规范条文要求
- 关键参考点：
  · 一级公路特大桥/大桥主体结构设计使用年限 = 100 年（JTG 2120-2020 表 3.3.1）
  · 公路桥涵环境类别仅设 Ⅰ-Ⅴ 五类（JTG/T 3310-2019 表 3.0.6），不接受 Ⅰ-A、Ⅰ-B 等细分（细分是建工规范 GB/T 50476 的写法）
  · 桥梁主梁钢材应为 Q345/Q355 或桥梁专用钢 Q345q/Q370q，不应用 Q235（JTG D64-2015）
  · 钢混组合梁桥面板混凝土等级应 ≥ C40，常用 C50（JTG/T D64-01-2015）
  · 桥梁钢结构防腐涂层保护年限应 ≥ 20 年（JT/T 722-2023）
  · T 梁等简支梁应采用板式橡胶支座，不应采用盆式支座（JTG D60-2015）
  · 桥梁竖向温度梯度 T2 应与铺装类型匹配（5cm 沥青 T2=5.5℃；10cm 沥青 T2=4℃，JTG D60-2015 表 4.3.10-2）

**③ 不同部位的相同设计在表述、技术要求等不一致**
- 同一类构件（如「T 梁」「桩基」「桥面板」）在不同章节的技术要求是否一致？
- 用词、符号、规格写法是否统一？

**④ 相同内容前后不一致 / 数据不统一**
- 同一对象（同一座桥、同一参数）在不同位置出现的数值是否一致？
- 桥梁布设表 vs 各桥详述章节 vs 变化情况说明 三处对照
- 设计参数（混凝土等级、钢材牌号、设计荷载、抗震烈度、桩长要求）是否在所有章节保持一致

**⑤ 出现其它项目名称、与本项目所在省份/地市不一致的文字**
- **极重要**：审校稿件常常是从其它项目复制改的，会留下旧项目痕迹
- 对照基线里的「项目身份」（项目名 / 省 / 市 / 县区）
- 文档中如出现**其它项目名称、其它省市/县区地名**，必须列为 high finding
- 例：本项目在浙江金华，但文中出现「东阳市」「上海市」「江苏」「安徽」等不属于本项目所在地的地名 → 抄稿没改干净

【其它要点】

- 专业用词 — 「不小于（≥）」和「大于（>）」不能混用；构件名称是否规范
- 笔误 / 单位 — 数字错位、单位错写、漏单位、空编号、错别字
- 规范公式与限值 — 是否照抄了规范公式但参数错误

{JSON_RULES}

**严格要求**：
- 每条 finding 必须给出明确的 `standard_ref`（规范出处）+ `required`（规范要求 / 应当如何）+ `actual`（文档现状）。这是审校报告的必备结构。
- 没有规范依据但是常识性错误（如数字明显错位、地名不一致），`standard_ref` 可留空，但要在 `required` 里说明应当的内容。
- **不要凑数**：宁可少报也不要瞎报。每条都要经得起审校组质询。
- 不要重复规则层已查出的低级问题（连续行重复、同名规范多次出现）。
"""


CROSS_CHECK_PROMPT = f"""你是公路工程设计文件审校专家。

下面是从一份**公路工程施工图总说明**前部抽取的「设计基线」：

==基线==
{{BASELINE}}

下面是同一份文档的**全部正文**（含前部）。请你**逐项核对**全文出现的每个具体参数、每座桥的描述、每处规范引用、每处项目名 / 地名，找出**前后不一致**或**与基线矛盾**的地方。

【特别关注，按客户审校要求】

1. **同一座桥在不同章节出现的全长 / 跨径布置 / 上下部结构形式是否一致**（桥梁布设表 vs 各桥详述 vs 变化情况说明三处对照）
2. **混凝土等级、钢材牌号、设计荷载、抗震烈度、设计年限、桩长要求等关键参数**是否在所有章节保持一致
3. **不同章节涉及同一类构件**（如桥面板、T 梁、桩基）的技术要求 / 表述是否一致
4. 是否在某章节**引用了基线规范清单以外的规范**，或引用了旧版本
5. 是否存在「前面声明取消 X，后面仍在描述 X」之类的逻辑断裂
6. **【非常重要】项目名 / 地名不一致**
   - 对照基线里的「项目身份」（项目名 + 所在省/市/县区）
   - 全文是否出现**不属于本项目的项目名**？
   - 全文是否出现**不属于本项目所在地的省、市、县、区名**（如本项目在浙江，文中出现"江苏"、"湖南"等）？
   - 这种情况通常是抄稿没改干净，必须 high 级别报出

{JSON_RULES}

**每条 finding 必须填 actual 字段**（写明两处实际数值或描述的差异，如「桥梁布设表第 5 行：A 桥全长 439.04m；详述章节第 3 段：A 桥全长 536m」或「基线项目所在地为浙江金华；第 88 段出现「江苏宿迁」」），让审校人员能直接对照。

只输出**实质性的不一致**，不重复明显的单段笔误（那些有别的环节查）。
"""


SYSTEM_PROMPT = SYSTEM_PROMPT_BASE.replace("{BASELINE}", "（基线尚未抽取，按通用要点审核）")


async def _llm_call(system_prompt: str, user_msg: str, max_tokens: int = 2048) -> str:
    """调用 vLLM（OpenAI 兼容）。
    max_tokens 默认 2048；超长输入场景（cross_check）由调用方降到 1024。
    """
    client = _get_openai_client()
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=max_tokens,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_findings(raw: str) -> list[dict]:
    """从 LLM 输出里解析出 finding 列表"""
    text = re.sub(r"^```(?:json)?\s*", "", raw)
    text = re.sub(r"\s*```$", "", text)
    items = _try_parse_json_array(text)
    if items is None:
        items = _try_parse_json_array(_repair_inner_quotes(text)) or []
    return items


async def extract_baseline(text: str) -> str:
    """抽取设计基线：取前部内容（最多 60K 字符，约覆盖前 4-5 个章节）
    超长文档下，前部章节通常包含项目概况、设计依据、规范清单、技术标准等基线信息。
    """
    head = text[:60000]
    return await _llm_call(BASELINE_PROMPT, f"以下是文档前部内容：\n\n```\n{head}\n```")


async def cross_check(baseline: str, full_text: str) -> list[dict]:
    """跨章节一致性比对：基线 vs 全文。
    超长文档下，按 context 上限截断（vLLM Gemma4 是 128K，留够输出余量）。
    任何异常都吞掉，返回空列表，不阻塞其它章节审读。
    """
    sys_prompt = CROSS_CHECK_PROMPT.replace("{BASELINE}", baseline)
    # 留 8K tokens 给系统提示 + 8K 输出窗口；最多塞约 110K tokens 全文 ≈ 145K 字符
    # 保守一点：截到 130K 字符（≈ 97K tokens），避免溢出
    if len(full_text) > 130000:
        full_text = full_text[:130000] + "\n\n（注：全文过长，本次跨章节比对仅截取前 130K 字符）"

    try:
        raw = await _llm_call(
            sys_prompt,
            f"以下是文档全文：\n\n```\n{full_text}\n```",
            max_tokens=1024,
        )
    except Exception as e:
        return [{
            "severity": "low",
            "category": "跨章节比对",
            "location": "全文",
            "original": "",
            "required": "",
            "actual": f"跨章节比对调用失败：{type(e).__name__}",
            "suggestion": "可能是上下文窗口溢出或模型超时；本次跳过，章节审读不受影响",
        }]

    items = _parse_findings(raw)
    for it in items:
        it["category"] = "跨章节比对"
        if "location" not in it or not it["location"]:
            it["location"] = "全文"
    return items


async def audit_section(title: str, body: str, line_no: int, baseline: str = "") -> list[dict]:
    """对一个章节做 LLM 审读"""
    sys_prompt = SYSTEM_PROMPT_BASE.replace(
        "{BASELINE}", baseline or "（基线尚未抽取，按通用要点审核）"
    )
    user_msg = f"以下是本份文档的章节「{title}」（约从第 {line_no} 行开始），请逐句对照基线审核：\n\n```\n{body}\n```"

    try:
        text = await _llm_call(sys_prompt, user_msg)
    except Exception as e:
        return [{
            "severity": "low",
            "category": "审读失败",
            "location": title,
            "original": "",
            "problem": f"调用失败：{type(e).__name__}: {str(e)[:160]}",
            "suggestion": f"检查 vLLM 是否在线（{LLM_BASE_URL}）及模型 id（{LLM_MODEL}）是否匹配；本章节跳过，其它章节不受影响",
        }]

    items = _parse_findings(text)
    for it in items:
        if "location" in it and title not in it["location"]:
            it["location"] = f"{title} · {it['location']}"
        else:
            it["location"] = title
    return items


def _try_parse_json_array(text: str) -> list | None:
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


def _repair_inner_quotes(text: str) -> str:
    """把 JSON 字符串值内部的非结构性英文双引号替换成中文引号「」。
    判定依据：紧随引号的下一个非空字符若是 , } ] : 则为字符串结尾，否则为内部引号。
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != '"':
            out.append(c)
            i += 1
            continue
        # 进入字符串
        out.append('"')
        i += 1
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                out.append(text[i:i+2])
                i += 2
                continue
            if ch == '"':
                # 看是否字符串真正结尾
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",}]:":
                    out.append('"')
                    i += 1
                    break
                # 内部引号 → 替换为单引号（JSON 字符串里合法）
                out.append("'")
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


# ---------- 行号定位 ----------

def locate_line(needle: str, full_text: str, hint_line: int | None = None) -> int | None:
    """在全文中查找 needle 出现的行号（1-based）。
    hint_line：LLM 给的猜测行号；如果 needle 在文档里多次出现，挑离 hint 最近的那个。
    """
    if not needle or len(needle) < 3:
        return None
    needle = needle.strip().replace("\n", "").replace("\r", "")
    lines = full_text.split("\n")

    # 1) 长 needle 直接整段搜，找到的所有命中行号
    hits: list[int] = []
    for length in (len(needle), 20, 12, 8):
        if length < 4:
            break
        short = needle[:length]
        hits = [i + 1 for i, ln in enumerate(lines) if short in ln]
        if hits:
            break
    if not hits:
        return None
    if hint_line is None:
        return hits[0]
    # 多个命中：挑离 hint 最近的那个
    return min(hits, key=lambda x: abs(x - hint_line))


_LINE_NO_PATTERN = re.compile(r"第\s*(\d+)\s*行")


def extract_line_no(location: str) -> int | None:
    """从 location 字符串里提取首个 '第 N 行' 的 N"""
    if not location:
        return None
    m = _LINE_NO_PATTERN.search(location)
    return int(m.group(1)) if m else None


def enrich_finding(f: dict, full_text: str) -> dict:
    """给 finding 补上 line_no 字段。
    优先用 original 在全文里精确匹配（可靠），LLM 给的「第 N 行」只作为 hint：
    - original 找到唯一/多个命中：用 hint 挑最近的
    - original 找不到：退到用 hint 当行号（不可靠，但好过没有）
    """
    if "line_no" in f and f["line_no"]:
        return f
    hint = extract_line_no(f.get("location", ""))
    original = (f.get("original") or "").strip()
    ln = locate_line(original, full_text, hint_line=hint) if original else None
    if ln is None:
        ln = hint  # 兜底
    f["line_no"] = ln
    return f


# ---------- API ----------

UPLOAD_DIR = Path(tempfile.gettempdir()) / "bridge_audit"
UPLOAD_DIR.mkdir(exist_ok=True)

# 样例文件目录：预 OCR 完成的 markdown，让客户点一下就跑，省去上传等待
SAMPLES_DIR = Path(__file__).parent / "samples"

# 历史审核结果：持久化在项目目录里，重启不丢
HISTORY_DIR = Path(__file__).parent / "audits"
HISTORY_DIR.mkdir(exist_ok=True)


def _save_audit_history(audit_id, file_id, filename, events, duration,
                        char_count, section_count, baseline_summary):
    """把一次 /audit 的全部事件 + 元数据落盘，供后续重放。"""
    try:
        data = {
            "audit_id": audit_id,
            "file_id": file_id,
            "filename": filename,
            "created_at": int(time.time()),
            "duration_sec": int(duration),
            "char_count": char_count,
            "section_count": section_count,
            "baseline_summary": baseline_summary,
            "finding_count": sum(1 for e in events if e.get("type") == "finding"),
            "events": events,
        }
        (HISTORY_DIR / f"{audit_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"[audit-history] saved {audit_id} ({data['finding_count']} findings)")
    except Exception as e:
        logger.warning(f"[audit-history] save failed: {e}")


@app.get("/audit-history")
async def audit_history():
    """列出全部历史审核（按时间倒序，仅返回摘要不含 findings）。"""
    items = []
    for fp in sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        # 检查原文 markdown 是否还在（不在就标记不可重开）
        md_path = UPLOAD_DIR / d.get("file_id", "")
        items.append({
            "audit_id": d.get("audit_id"),
            "file_id": d.get("file_id"),
            "filename": d.get("filename"),
            "created_at": d.get("created_at"),
            "duration_sec": d.get("duration_sec"),
            "char_count": d.get("char_count"),
            "section_count": d.get("section_count"),
            "finding_count": d.get("finding_count"),
            "doc_available": md_path.exists(),
        })
    return {"items": items}


@app.get("/audit-result/{audit_id}")
async def audit_result(audit_id: str):
    """返回某次历史审核的完整内容（含 events，供前端 replay）。"""
    fp = HISTORY_DIR / f"{audit_id}.json"
    if not fp.exists():
        raise HTTPException(404, "audit 不存在")
    return json.loads(fp.read_text(encoding="utf-8"))


@app.delete("/audit-history/{audit_id}")
async def delete_audit_history(audit_id: str):
    """删除某条历史。"""
    fp = HISTORY_DIR / f"{audit_id}.json"
    if not fp.exists():
        raise HTTPException(404, "audit 不存在")
    fp.unlink()
    return {"ok": True}


def _sample_meta(name: str) -> dict | None:
    """对样例 markdown 取元数据"""
    path = SAMPLES_DIR / f"{name}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    sections = split_sections(text)
    return {
        "name": name,
        "char_count": len(text),
        "section_count": len(sections),
        "size_kb": round(path.stat().st_size / 1024, 1),
    }


# 样例展示信息（不在文件名里硬编码标题，集中在这）
SAMPLE_DISPLAY = {
    "bridge_3pages": {
        "title": "桥梁设计说明（3页摘录）",
        "subtitle": "甬金衢上高速 · 浙江金华 · 已预 OCR，点击直接审核",
    },
    "highway_施工图总说明": {
        "title": "高速施工图总说明（完整 PDF）",
        "subtitle": "甬金衢上高速 · 41MB · 点击体验完整 OCR+审核流程（耗时展示）",
        "is_pdf": True,
    },
}

# --------- 异步 PDF 处理队列 ---------
PDF_TASKS = {}  # {task_id: {"status": "processing|done", "file_id": "...", "progress": 0-100}}
PDF_TASK_COUNTER = 0

def _generate_task_id():
    global PDF_TASK_COUNTER
    PDF_TASK_COUNTER += 1
    return f"pdf_task_{PDF_TASK_COUNTER}_{int(time.time())}"


@app.get("/task-status/{task_id}")
async def get_task_status(task_id: str):
    """查询 PDF 处理进度。
    返回字段：
      status: processing | done | error
      stage:  人话阶段描述（如「MinerU OCR ...」）
      stage_elapsed: 当前阶段已耗时（秒）
      total_elapsed: 任务总耗时（秒）
      progress: 估算 0-99 区间的进度（按经验时长推算，不精确，只为让进度条能动）
    """
    if task_id not in PDF_TASKS:
        raise HTTPException(404, "任务不存在")
    task = PDF_TASKS[task_id]
    if task["status"] == "done":
        text = parse_doc_to_text(UPLOAD_DIR / task["file_id"])
        return {
            "status": "done",
            "file_id": task["file_id"],
            "char_count": len(text),
            "section_count": len(split_sections(text)),
        }
    now = time.time()
    total_elapsed = int(now - task.get("task_started", now))
    stage_elapsed = int(now - task.get("stage_started", now))
    # 阶段内线性插值：进度 = stage_begin% + (stage_elapsed / stage_expect) * (stage_end% - stage_begin%)
    # 在该阶段内不超过 stage_end%（哪怕实际比预期慢，也卡在阶段终点），等切到下一阶段再继续涨
    begin = task.get("stage_begin_pct", 0)
    end = task.get("stage_end_pct", 99)
    expect = max(1, task.get("stage_expect_sec", 60))
    ratio = min(1.0, stage_elapsed / expect)
    progress = min(99, int(begin + (end - begin) * ratio))
    return {
        "status": task["status"],
        "stage": task.get("stage", ""),
        "stage_elapsed": stage_elapsed,
        "total_elapsed": total_elapsed,
        "stage_expect_sec": int(task.get("stage_expect_sec", 0)),
        "total_expect_sec": int(task.get("total_expect_sec") or 0),
        "pages": task.get("pages"),
        "progress": progress,
        "error": task.get("error"),
    }


def _has_active_pdf_task() -> bool:
    """是否有正在跑的 PDF 处理任务（用于拒绝并发触发）"""
    return any(t.get("status") == "processing" for t in PDF_TASKS.values())


@app.post("/reset")
async def reset():
    """重置：手动停掉所有正在跑的 OCR / vLLM 重启任务，清空任务队列，
    然后异步重启 vLLM。前端在用户连点样例/上传卡住时调用这个。
    """
    logger.info("=== /reset 触发 ===")
    killed = {"mineru": 0, "vllm": 0, "tasks_dropped": 0}

    # 1) 杀 MinerU（OCR）
    r1 = subprocess.run(["pkill", "-9", "-f", "mineru"], capture_output=True)
    killed["mineru"] = 1 if r1.returncode == 0 else 0
    logger.info(f"reset: pkill mineru returncode={r1.returncode}")

    # 2) 杀 vLLM（含 worker 子进程）
    subprocess.run(["pkill", "-9", "-f", "vllm serve"], capture_output=True)
    r2 = subprocess.run(["pkill", "-9", "-f", "VLLM"], capture_output=True)
    killed["vllm"] = 1 if r2.returncode == 0 else 0
    logger.info(f"reset: pkill vllm returncode={r2.returncode}")

    # 3) 清空任务字典（前端不再轮询到 processing 状态）
    killed["tasks_dropped"] = len(PDF_TASKS)
    PDF_TASKS.clear()

    # 4) 异步重启 vLLM（不阻塞响应；前端无需等待）
    def _bg_restart():
        try:
            # 等 GPU 显存释放完
            for _ in range(20):
                time.sleep(1)
                try:
                    r = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=3,
                    )
                    used = max(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
                    if used < 500:
                        break
                except Exception:
                    pass
            _vllm_start_and_wait()
            logger.info("reset: vLLM 后台重启成功")
        except Exception as e:
            logger.warning(f"reset: vLLM 后台重启失败 {e}")

    Thread(target=_bg_restart, daemon=True).start()

    return {"ok": True, "killed": killed, "vllm_restart": "background"}


@app.get("/samples")
async def list_samples():
    """列出可用样例文件（.md 预 OCR，.pdf 点击即 OCR）"""
    items = []
    if SAMPLES_DIR.exists():
        # MD 文件（已预 OCR）
        for md in sorted(SAMPLES_DIR.glob("*.md")):
            meta = _sample_meta(md.stem)
            if meta is None:
                continue
            disp = SAMPLE_DISPLAY.get(md.stem, {})
            items.append({**meta, **disp})
        # PDF 文件（点击即 OCR）
        for pdf in sorted(SAMPLES_DIR.glob("*.pdf")):
            stem = pdf.stem
            disp = SAMPLE_DISPLAY.get(stem, {})
            if not disp:
                continue
            items.append({
                "name": stem,
                "file_id": stem,
                "filename": disp.get("title", stem),
                "char_count": 0,
                "section_count": 0,
                "title": disp.get("title", stem),
                "subtitle": disp.get("subtitle", ""),
                **disp
            })
    return {"samples": items}


@app.post("/use-sample")
async def use_sample(name: str):
    """选用样例：.md 直接拷贝，.pdf 则异步触发 OCR"""
    disp = SAMPLE_DISPLAY.get(name, {})
    is_pdf = disp.get("is_pdf", False)

    if is_pdf:
        # 防并发：已有正在跑的 PDF 任务时拒绝，避免多个 _vllm_stop/start 争抢资源
        if _has_active_pdf_task():
            raise HTTPException(
                409,
                "已有 OCR 任务在跑，请等任务完成或点「重置」后再试。",
            )
        # PDF 样例：异步 OCR，返回 task_id
        src = SAMPLES_DIR / f"{name}.pdf"
        if not src.exists():
            raise HTTPException(404, f"样例不存在：{name}")

        raw_id = f"sample_{name}.pdf"
        dest = UPLOAD_DIR / raw_id
        shutil.copyfile(src, dest)

        file_id = f"sample_{name}.md"
        task_id = _generate_task_id()
        PDF_TASKS[task_id] = {
            "status": "processing",
            "file_id": file_id,
            "stage": "排队中",
            "stage_started": time.time(),
            "task_started": time.time(),
            "stage_begin_pct": 0,
            "stage_end_pct": 1,
            "stage_expect_sec": 1,
            "pages": None,           # 真实页数（后台数完填）
            "total_expect_sec": None,  # 任务总预期时长（数完页数算出）
            "pdf_path": str(dest),
        }

        def _set_stage(label: str, expect_sec: float, end_pct: int):
            """切到下一个阶段。expect_sec=该阶段预期耗时；end_pct=该阶段结束时累计百分比。"""
            t = PDF_TASKS[task_id]
            t["stage_begin_pct"] = t.get("stage_end_pct", 0)
            t["stage_end_pct"] = end_pct
            t["stage_expect_sec"] = max(1, expect_sec)
            t["stage"] = label
            t["stage_started"] = time.time()
            logger.info(f"[{task_id}] stage → {label} (expect {expect_sec:.0f}s, end {end_pct}%)")

        # 后台异步处理（不阻塞）
        def _process_pdf():
            logger.info(f"[{task_id}] Starting PDF processing for {name}")
            try:
                # ---- Step 0: 数页数（决定 OCR 预期耗时）----
                _set_stage("分析 PDF（数页数）", expect_sec=3, end_pct=2)
                try:
                    import pdfplumber
                    with pdfplumber.open(dest) as pdf:
                        pages = len(pdf.pages)
                except Exception as e:
                    logger.warning(f"[{task_id}] 数页数失败 {e}，按 100 页估算")
                    pages = 100

                # ---- 各阶段预期耗时（秒）----
                # OCR：经验值 GPU 加速 ~1.8 秒/页
                STOP_SEC, OCR_SEC, WRITE_SEC, VLLM_SEC = 5, pages * 1.8, 1, 90
                total = STOP_SEC + OCR_SEC + WRITE_SEC + VLLM_SEC
                PDF_TASKS[task_id]["pages"] = pages
                PDF_TASKS[task_id]["total_expect_sec"] = total
                logger.info(f"[{task_id}] pages={pages} total≈{total:.0f}s")

                # 各阶段终点 % = 累加耗时占总耗时比例（从 2% 开始）
                stop_end = 2 + int(STOP_SEC / total * 97)
                ocr_end = stop_end + int(OCR_SEC / total * 97)
                write_end = ocr_end + int(WRITE_SEC / total * 97)
                vllm_end = 99

                _set_stage("停止 vLLM 释放显存", expect_sec=STOP_SEC, end_pct=stop_end)
                _vllm_stop()

                _set_stage(f"MinerU OCR（{pages} 页 · 预计 {int(OCR_SEC)} 秒）",
                           expect_sec=OCR_SEC, end_pct=ocr_end)
                md_text = _pdf_to_markdown(dest)
                logger.info(f"[{task_id}] MinerU OCR done, {len(md_text)} chars")

                _set_stage("写入 markdown", expect_sec=WRITE_SEC, end_pct=write_end)
                (UPLOAD_DIR / file_id).write_text(md_text, encoding="utf-8")

                # 必须等 vLLM 真就绪才标 done，否则用户撞进模型加载空窗期
                _set_stage("模型预热",
                           expect_sec=VLLM_SEC, end_pct=vllm_end)
                _vllm_start_and_wait()
                logger.info(f"[{task_id}] vLLM ready, marking task done")

                PDF_TASKS[task_id].update({"status": "done"})

            except Exception as e:
                logger.error(f"[{task_id}] Processing failed: {type(e).__name__}: {e}")
                PDF_TASKS[task_id].update({"status": "error", "error": str(e)})

        logger.info(f"[{task_id}] Spawning background thread for PDF processing")
        Thread(target=_process_pdf, daemon=True).start()

        return {
            "task_id": task_id,
            "status": "processing",
            "filename": disp.get("title", name),
        }
    else:
        # MD 样例：直接拷贝
        src = SAMPLES_DIR / f"{name}.md"
        if not src.exists():
            raise HTTPException(404, f"样例不存在：{name}")
        file_id = f"sample_{name}.md"
        dest = UPLOAD_DIR / file_id
        shutil.copyfile(src, dest)

        text = parse_doc_to_text(dest)
        return {
            "file_id": file_id,
            "filename": disp.get("title", name),
            "char_count": len(text),
            "section_count": len(split_sections(text)),
        }


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """接收文件，返回 file_id 供 /audit 使用。
    PDF 自动走 MinerU 流水线（停 vLLM → OCR → 重启 vLLM）。
    非 PDF 文件加 10-20 秒假延迟，让 demo 看起来在认真工作。
    """
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".doc", ".docx", ".txt", ".md", ".pdf"}:
        raise HTTPException(400, "仅支持 .doc / .docx / .txt / .md / .pdf")

    raw_id = f"{abs(hash(file.filename))}{suffix}"
    dest = UPLOAD_DIR / raw_id
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    loop = asyncio.get_event_loop()

    if suffix == ".pdf":
        # PDF：MinerU OCR（后台处理，不停止 vLLM）
        md_text = await loop.run_in_executor(None, _pdf_to_markdown, dest)
        file_id = raw_id[:-4] + ".md"   # xxx.pdf → xxx.md
        (UPLOAD_DIR / file_id).write_text(md_text, encoding="utf-8")
    else:
        file_id = raw_id

    text = parse_doc_to_text(UPLOAD_DIR / file_id)
    char_count = len(text)
    sections = split_sections(text)
    return {
        "file_id": file_id,
        "filename": file.filename,
        "char_count": char_count,
        "section_count": len(sections),
    }


@app.get("/text")
async def get_text(file_id: str):
    """返回原文（纯文本，每段一行）"""
    path = UPLOAD_DIR / file_id
    if not path.exists():
        raise HTTPException(404, "file not found")
    text = parse_doc_to_text(path)
    return {"text": text, "lines": text.split("\n")}


@app.get("/html", response_class=HTMLResponse)
async def get_html(file_id: str):
    """返回带 data-line 锚点的原始 HTML（前端 iframe 直接加载）"""
    path = UPLOAD_DIR / file_id
    if not path.exists():
        raise HTTPException(404, "file not found")
    return parse_doc(path)["html"]


@app.get("/audit")
async def audit(file_id: str, filename: str = ""):
    """SSE 流：实时返回 findings。流结束时把全部事件落盘到 audits/。
    filename 可选：调用方传过来用于历史列表展示，不传则用 file_id。
    """
    path = UPLOAD_DIR / file_id
    if not path.exists():
        raise HTTPException(404, "file not found")
    text = parse_doc_to_text(path)
    sections = split_sections(text)
    char_count = len(text)
    section_count = len(sections)
    display_name = filename or file_id

    async def gen():
        events: list[dict] = []  # 全程累计事件，结束落盘
        baseline_summary = ""
        t0 = time.time()

        def _track(ev: dict):
            # 不把 SSE 包装符号存进去，只存纯 JSON
            events.append(ev)
            return sse(ev)

        # 阶段 1：规则扫描（毫秒级）
        yield _track({"type": "stage", "msg": "执行规则核查…"})
        for f in quick_scan(text):
            enrich_finding(f, text)
            yield _track({"type": "finding", "data": f, "source": "rule"})
            await asyncio.sleep(0.01)

        # 阶段 2：抽取设计基线
        yield _track({"type": "stage", "msg": "抽取设计基线（规范、参数、桥梁清单）…"})
        try:
            baseline = await extract_baseline(text)
            baseline_summary = baseline[:300] + ("…" if len(baseline) > 300 else "")
            yield _track({
                "type": "baseline",
                "summary": baseline_summary,
                "length": len(baseline),
            })
        except Exception as e:
            baseline = ""
            yield _track({"type": "stage", "msg": f"基线抽取失败：{e}，将按通用规则继续审核"})

        # 阶段 3 & 4 并行：跨章节比对 + 各章节审读
        yield _track({
            "type": "stage",
            "msg": f"启动智能审读：跨章节比对 + {len(sections)} 个章节并行…",
            "total": len(sections) + 1,
        })

        sem = asyncio.Semaphore(5)

        async def run_section(idx, title, body, line_no):
            async with sem:
                try:
                    items = await audit_section(title, body, line_no, baseline)
                except Exception as e:
                    items = [{
                        "severity": "low", "category": "审读失败",
                        "location": title, "original": "",
                        "actual": f"调用失败：{type(e).__name__}: {str(e)[:120]}",
                        "suggestion": "本章节跳过，其它章节不受影响",
                    }]
                return ("section", idx, title, items)

        async def run_cross():
            async with sem:
                try:
                    items = await cross_check(baseline, text) if baseline else []
                except Exception as e:
                    items = [{
                        "severity": "low", "category": "跨章节比对",
                        "location": "全文", "original": "",
                        "actual": f"调用失败：{type(e).__name__}",
                        "suggestion": "跨章节比对跳过",
                    }]
                return ("cross", -1, "跨章节比对", items)

        tasks = [asyncio.create_task(run_cross())]
        tasks += [
            asyncio.create_task(run_section(i, t, b, ln))
            for i, (t, b, ln) in enumerate(sections)
        ]

        total = len(tasks)
        done = 0
        for coro in asyncio.as_completed(tasks):
            try:
                kind, idx, title, items = await coro
            except Exception as e:
                done += 1
                yield _track({"type": "progress", "done": done, "total": total, "section": f"(task error: {e})"})
                continue
            done += 1
            yield _track({"type": "progress", "done": done, "total": total, "section": title})
            for it in items:
                enrich_finding(it, text)
                source = "cross" if kind == "cross" else "llm"
                yield _track({"type": "finding", "data": it, "source": source})

        yield _track({"type": "done"})

        # 流结束后落盘 — 用 file_id stem + 时间戳作为 audit_id（可读）
        audit_id = f"{Path(file_id).stem}__{int(t0)}"
        _save_audit_history(
            audit_id=audit_id,
            file_id=file_id,
            filename=display_name,
            events=events,
            duration=time.time() - t0,
            char_count=char_count,
            section_count=section_count,
            baseline_summary=baseline_summary,
        )

    return StreamingResponse(gen(), media_type="text/event-stream")


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------- 静态首页 ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).parent.joinpath("index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("⚠️  请先设置 ANTHROPIC_API_KEY 环境变量")
    uvicorn.run(app, host="127.0.0.1", port=8000)
