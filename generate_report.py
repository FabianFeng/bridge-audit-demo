"""把 audit_stream.log + 原 markdown 渲染成单页 HTML 报告
用法：python generate_report.py <log_path> <markdown_path> [out_html]
"""
import json
import re
import sys
from pathlib import Path

import markdown as md
from bs4 import BeautifulSoup


def parse_findings(log_path: Path) -> tuple[list[dict], str, int]:
    findings = []
    baseline = ""
    total = 0
    for line in log_path.read_text(encoding="utf-8").split("\n"):
        m = re.match(r"data: (.+)$", line)
        if not m:
            continue
        try:
            ev = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "finding":
            f = ev["data"]
            f["__source"] = ev.get("source", "llm")
            findings.append(f)
        elif ev.get("type") == "baseline":
            baseline = ev.get("summary", "")
        elif ev.get("type") == "progress":
            total = ev.get("total", total)
    return findings, baseline, total


def render_doc_html(md_path: Path) -> str:
    """复用 app.py 的渲染逻辑：markdown → HTML，给每段加 data-line"""
    raw_md = md_path.read_text(encoding="utf-8")
    html_body = md.markdown(raw_md, extensions=["tables", "fenced_code", "toc"])
    soup = BeautifulSoup(html_body, "lxml")
    LEAF_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"]
    line_no = 0
    for el in soup.find_all(LEAF_TAGS):
        if el.find(LEAF_TAGS):
            continue
        if not el.get_text(strip=True):
            continue
        line_no += 1
        el["data-line"] = str(line_no)
    # 返回 body 内 HTML，不带 <html><body>
    body = soup.body or soup
    return str(body).replace("<body>", "").replace("</body>", "")


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>桥梁设计文档审校 - 静态报告</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,400;0,500;0,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg:#F4F1EA; --bg-soft:#EDE8DC; --ink:#2A2823; --ink-soft:#6B6557;
    --accent:#C76A3A; --line:#D9D2C2; --card:#FBF9F3;
    --high:#C0392B; --medium:#B7791F; --low:#6B6557;
  }
  body { font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--ink); height:100vh; overflow:hidden; }
  .serif { font-family:'Fraunces',Georgia,serif; }
  .card { background:var(--card); border:1px solid var(--line); }
  .pill { background:var(--bg-soft); color:var(--ink-soft); border:1px solid var(--line); }
  .accent { color:var(--accent); } .bg-accent { background:var(--accent); }
  .severity-high { color:var(--high); border-color:#E8B5AF; background:#FBE9E5; }
  .severity-medium { color:var(--medium); border-color:#E8D4A8; background:#FAF1DE; }
  .severity-low { color:var(--low); border-color:var(--line); background:var(--bg-soft); }
  .finding-card { transition:all .15s; cursor:pointer; }
  .finding-card:hover { border-color:var(--accent); transform:translateX(-2px); }
  .finding-card.active { border-color:var(--accent); box-shadow:-2px 0 0 var(--accent); }
  .doc-pane { font-family:-apple-system,'PingFang SC',sans-serif; padding:24px; max-width:880px; margin:0 auto; line-height:1.7; }
  .doc-pane h1 { font-family:'Fraunces',serif; font-size:22px; border-bottom:2px solid var(--line); padding-bottom:8px; margin-top:1.6em; margin-bottom:0.6em; }
  .doc-pane h2 { font-family:'Fraunces',serif; font-size:18px; margin-top:1.2em; }
  .doc-pane h3 { font-family:'Fraunces',serif; font-size:16px; color:var(--ink-soft); }
  .doc-pane table { border-collapse:collapse; margin:1em 0; font-size:13px; }
  .doc-pane th, .doc-pane td { border:1px solid var(--line); padding:6px 10px; }
  .doc-pane th { background:var(--bg-soft); }
  .doc-pane img { max-width:100%; opacity:0.4; }
  [data-line].hl { outline:2px solid var(--accent) !important; outline-offset:2px; background:#FAF1DE !important; }
  [data-line].flash { animation:flash 1.2s ease; }
  @keyframes flash { 0% { background:rgba(199,106,58,.45)!important; } 100% { background:#FAF1DE!important; } }
  .scrollbar::-webkit-scrollbar { width:8px; }
  .scrollbar::-webkit-scrollbar-thumb { background:var(--line); border-radius:4px; }
</style>
</head>
<body>
<header class="border-b shrink-0" style="border-color:var(--line);">
  <div class="px-6 py-3 flex items-baseline justify-between">
    <div class="flex items-baseline gap-4">
      <h1 class="serif text-xl">桥梁设计文档审校 · 静态报告</h1>
      <span class="text-xs" style="color:var(--ink-soft);">{{FILENAME}}</span>
    </div>
    <div class="text-xs" style="color:var(--ink-soft);">
      <span>后端: <span class="font-medium accent">Gemma 4 31B NVFP4 · 双 5090</span> · 耗时 ~10 分钟</span>
    </div>
  </div>
</header>

<main class="grid h-[calc(100vh-49px)]" style="grid-template-columns:1fr 540px;">

  <section class="border-r overflow-auto scrollbar" style="border-color:var(--line); background:white;">
    <div class="doc-pane" id="doc-pane">
      {{DOC_HTML}}
    </div>
  </section>

  <section class="flex flex-col min-h-0" style="background:var(--bg);">
    <div class="border-b px-4 py-3 shrink-0" style="border-color:var(--line);">
      <details class="mb-3">
        <summary class="text-xs cursor-pointer accent font-medium">📋 设计基线（点击展开）</summary>
        <pre class="mt-2 text-[11px] p-2 rounded overflow-auto max-h-48 scrollbar" style="background:var(--bg-soft); color:var(--ink-soft); white-space:pre-wrap;">{{BASELINE}}</pre>
      </details>
      <div class="grid grid-cols-4 gap-1.5 mb-2">
        <div class="card rounded p-2 text-center"><div class="text-[10px]" style="color:var(--ink-soft);">总数</div><div class="serif text-lg font-medium">{{TOTAL}}</div></div>
        <div class="card rounded p-2 text-center"><div class="text-[10px]" style="color:var(--high);">严重</div><div class="serif text-lg font-medium" style="color:var(--high);">{{HIGH}}</div></div>
        <div class="card rounded p-2 text-center"><div class="text-[10px]" style="color:var(--medium);">中等</div><div class="serif text-lg font-medium" style="color:var(--medium);">{{MEDIUM}}</div></div>
        <div class="card rounded p-2 text-center"><div class="text-[10px]" style="color:var(--low);">轻微</div><div class="serif text-lg font-medium">{{LOW}}</div></div>
      </div>
      <div class="flex items-center gap-1.5 text-xs">
        <button class="filter-btn pill px-2.5 py-1 rounded-full text-[11px]" data-filter="all">全部</button>
        <button class="filter-btn pill px-2.5 py-1 rounded-full text-[11px]" data-filter="high">严重</button>
        <button class="filter-btn pill px-2.5 py-1 rounded-full text-[11px]" data-filter="medium">中等</button>
        <button class="filter-btn pill px-2.5 py-1 rounded-full text-[11px]" data-filter="low">轻微</button>
        <span class="ml-auto text-[11px]" style="color:var(--ink-soft);">规则 {{RULE_CNT}} · 智能 {{LLM_CNT}} · 跨章节 {{CROSS_CNT}}</span>
      </div>
    </div>
    <div id="findings" class="flex-1 overflow-auto scrollbar p-3 space-y-2"></div>
  </section>

</main>

<script>
const FINDINGS = {{FINDINGS_JSON}};
const findingsEl = document.getElementById('findings');
const docPane = document.getElementById('doc-pane');
let currentFilter = 'all';

function severityLabel(s) { return {high:'严重', medium:'中等', low:'轻微'}[s] || s; }
function sourceLabel(s) { return {rule:'规则核查', llm:'智能审读', cross:'跨章节比对'}[s] || s; }
function esc(s) { return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function trunc(s, n) { return s.length>n ? s.slice(0,n)+'…' : s; }

function fields(f) {
  const rows = [];
  if (f.standard_ref) rows.push(['依据', f.standard_ref, 'serif']);
  if (f.required) rows.push(['要求', f.required, '']);
  if (f.actual) rows.push(['现状', f.actual, '']);
  if (!f.required && !f.actual && f.problem) rows.push(['问题', f.problem, '']);
  if (f.suggestion) rows.push(['建议', f.suggestion, 'accent']);
  return rows.map(([l,v,c])=>`
    <div class="text-[12px] leading-relaxed mb-1 flex gap-1.5">
      <span class="shrink-0 ${c==='accent'?'accent font-medium':''}" style="color:${c==='accent'?'':'var(--ink-soft)'}; min-width:30px;">${l}</span>
      <span class="${c==='serif'?'serif':''}" style="color:var(--ink);">${esc(v)}</span>
    </div>`).join('');
}

function render() {
  const filt = currentFilter==='all' ? FINDINGS : FINDINGS.filter(f=>f.severity===currentFilter);
  const order = {high:0, medium:1, low:2};
  const sorted = [...filt].sort((a,b)=>{
    const d = (order[a.severity]??9)-(order[b.severity]??9);
    return d!==0 ? d : (a.line_no??99999)-(b.line_no??99999);
  });
  findingsEl.innerHTML = sorted.map((f,i)=>`
    <div class="card finding-card rounded-lg p-3" data-fid="${f.__idx}">
      <div class="flex items-center gap-1.5 mb-2 text-[10px]">
        <span class="severity-${f.severity} px-1.5 py-0.5 rounded border font-medium">${severityLabel(f.severity)}</span>
        <span class="pill px-1.5 py-0.5 rounded">${esc(f.category||'')}</span>
        <span class="pill px-1.5 py-0.5 rounded">${sourceLabel(f.__source)}</span>
        ${f.line_no?`<span class="ml-auto accent font-medium">第 ${f.line_no} 行</span>`:`<span class="ml-auto opacity-50 truncate" style="max-width:160px;">${esc(f.location||'')}</span>`}
      </div>
      ${f.original?`<div class="text-[11px] p-1.5 mb-2 rounded leading-relaxed" style="background:var(--bg-soft); color:var(--ink-soft); border-left:2px solid var(--line); font-family:'JetBrains Mono',monospace;">${esc(trunc(f.original,120))}</div>`:''}
      ${fields(f)}
    </div>`).join('');

  findingsEl.querySelectorAll('.finding-card').forEach(el=>{
    el.addEventListener('click',()=>{
      findingsEl.querySelectorAll('.finding-card').forEach(e=>e.classList.toggle('active',e===el));
      const f = FINDINGS[+el.dataset.fid];
      if (f.line_no) jumpTo(f.line_no);
    });
  });
}

function jumpTo(n) {
  docPane.querySelectorAll('[data-line].hl, [data-line].flash').forEach(e=>e.classList.remove('hl','flash'));
  const el = docPane.querySelector(`[data-line="${n}"]`);
  if (!el) return;
  el.classList.add('hl','flash');
  el.scrollIntoView({behavior:'smooth', block:'center'});
}

document.querySelectorAll('.filter-btn').forEach(b=>{
  b.addEventListener('click',()=>{
    currentFilter = b.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach(x=>{
      const on = x===b;
      x.classList.toggle('bg-accent',on);
      x.classList.toggle('text-white',on);
      x.classList.toggle('pill',!on);
    });
    render();
  });
});
document.querySelector('.filter-btn[data-filter="all"]').click();
FINDINGS.forEach((f,i)=>f.__idx=i);
render();
</script>
</body>
</html>
"""


def main():
    if len(sys.argv) < 3:
        print("用法: generate_report.py <log_path> <markdown_path> [out_html]")
        sys.exit(1)
    log_path = Path(sys.argv[1])
    md_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3] if len(sys.argv) > 3 else "/tmp/audit_report.html")

    findings, baseline, total = parse_findings(log_path)
    doc_html = render_doc_html(md_path)

    sev_count = {"high": 0, "medium": 0, "low": 0}
    src_count = {"rule": 0, "llm": 0, "cross": 0}
    for f in findings:
        sev_count[f.get("severity", "low")] = sev_count.get(f.get("severity", "low"), 0) + 1
        src_count[f.get("__source", "llm")] = src_count.get(f.get("__source", "llm"), 0) + 1
    # 给 findings 加序号供前端引用
    for i, f in enumerate(findings):
        f["__idx"] = i

    html = (HTML_TEMPLATE
        .replace("{{FILENAME}}", md_path.name)
        .replace("{{DOC_HTML}}", doc_html)
        .replace("{{BASELINE}}", baseline.replace("<", "&lt;").replace(">", "&gt;"))
        .replace("{{TOTAL}}", str(len(findings)))
        .replace("{{HIGH}}", str(sev_count["high"]))
        .replace("{{MEDIUM}}", str(sev_count["medium"]))
        .replace("{{LOW}}", str(sev_count["low"]))
        .replace("{{RULE_CNT}}", str(src_count.get("rule", 0)))
        .replace("{{LLM_CNT}}", str(src_count.get("llm", 0)))
        .replace("{{CROSS_CNT}}", str(src_count.get("cross", 0)))
        .replace("{{FINDINGS_JSON}}", json.dumps(findings, ensure_ascii=False))
    )
    out_path.write_text(html, encoding="utf-8")
    print(f"✓ 报告已生成: {out_path}")
    print(f"  findings: {len(findings)} (high={sev_count['high']}, medium={sev_count['medium']}, low={sev_count['low']})")
    print(f"  来源: rule={src_count.get('rule',0)} llm={src_count.get('llm',0)} cross={src_count.get('cross',0)}")
    print(f"  双击打开: open {out_path}")


if __name__ == "__main__":
    main()
