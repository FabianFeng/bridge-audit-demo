"""解析 /tmp/audit_stream.log 的 SSE 输出，按维度统计 findings"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

log_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audit_stream.log")
text = log_path.read_text(encoding="utf-8")

events = []
for line in text.split("\n"):
    m = re.match(r"data: (.+)$", line)
    if m:
        try:
            events.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass

findings = [e["data"] for e in events if e.get("type") == "finding"]
progress = [e for e in events if e.get("type") == "progress"]
stage = [e for e in events if e.get("type") == "stage"]
baseline = [e for e in events if e.get("type") == "baseline"]
done = [e for e in events if e.get("type") == "done"]

print(f"\n{'='*60}")
print(f"事件总数: {len(events)}")
print(f"  stage: {len(stage)} | baseline: {len(baseline)} | progress: {len(progress)}")
print(f"  finding: {len(findings)} | done: {len(done)}")

if progress:
    last = progress[-1]
    print(f"  最后进度: {last.get('done')}/{last.get('total')}")

print(f"\n{'='*60}")
print(f"按 source 统计 finding：")
by_source = Counter(f.get("__source") or "?" for f in findings)
# source 字段在 SSE 是顶层；finding.data 里没有
by_source_from_outer = Counter(
    e.get("source", "?") for e in events if e.get("type") == "finding"
)
for k, v in by_source_from_outer.most_common():
    print(f"  {k}: {v}")

print(f"\n按 severity 统计：")
for k, v in Counter(f.get("severity") for f in findings).most_common():
    print(f"  {k}: {v}")

print(f"\n按 category 统计：")
for k, v in Counter(f.get("category") for f in findings).most_common():
    print(f"  {k}: {v}")

# 按 source + severity 矩阵
print(f"\n来源 × 严重度 矩阵：")
src_sev = defaultdict(lambda: Counter())
for e in events:
    if e.get("type") == "finding":
        src_sev[e.get("source", "?")][e["data"].get("severity", "?")] += 1
print(f"  {'source':<10} {'high':>6} {'medium':>8} {'low':>6}")
for src, c in src_sev.items():
    print(f"  {src:<10} {c['high']:>6} {c['medium']:>8} {c['low']:>6}")

# 全部 HIGH severity 详细打印
print(f"\n{'='*60}")
high_findings = [
    (e.get("source", "?"), e["data"])
    for e in events
    if e.get("type") == "finding" and e["data"].get("severity") == "high"
]
print(f"全部 HIGH severity findings ({len(high_findings)} 条)：\n")
for i, (src, f) in enumerate(high_findings, 1):
    line_no = f.get("line_no") or f.get("location", "?")
    print(f'{i:3}. [{src}] L{line_no} | {f.get("category", "")}')
    if f.get("original"):
        print(f'      原文: {f["original"][:100]}')
    if f.get("standard_ref"):
        print(f'      依据: {f["standard_ref"][:100]}')
    if f.get("required"):
        print(f'      要求: {f["required"][:120]}')
    if f.get("actual"):
        print(f'      现状: {f["actual"][:120]}')
    if f.get("suggestion"):
        print(f'      建议: {f["suggestion"][:120]}')
    print()
