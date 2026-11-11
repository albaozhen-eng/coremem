#!/usr/bin/env python3
"""知识库事实一致性检查（只读）。

为什么需要它：知识库里的同一事实可能在不同文档给出不同取值，而**同一个问题检索到哪份文档
就得到不同答案**。首次人工核对就在 8 条航线里抓到 2 条冲突（宁波→墨尔本 15/22 vs 16/23、
宁波→布里斯班 15/22 vs 18/25），且**冲突在源 md 里**，不是转换或索引引入的 ——
所以它跟具体检索系统无关，改源是唯一正确的修法。

本检查只覆盖**结构化可比的事实**（目前是航线时效与冷藏上浮比例）。
散文里的矛盾抓不到；「一致但都错」也抓不到。这两点写在报告里，不要以为上了它就万事大吉。

设计要点：
  * 只读源 md（默认 ~/projects/my-wiki），不碰 WeKnora / 不碰索引
  * 区分**明确标注**与**推断位置**：表格里带「快船/普船」标注的才做高置信比对，
    靠位置猜出来的标为低置信，避免误报
  * 已确认的冲突可登记进 accepted.json 抑制（与 memory-ops 的 retired 机制同一套路），
    但抑制要显式、可审计

用法：
  python3 kb_consistency_check.py                      # 人读报告
  python3 kb_consistency_check.py --json out.json      # 机器可读
  python3 kb_consistency_check.py --accepted a.json    # 抑制已确认冲突
退出码：0=无冲突，1=有冲突（可用于入库前门禁），2=配置/路径问题
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC = Path(os.environ.get("KB_SOURCE_DIR", Path.home() / "projects" / "my-wiki"))
SKIP_PARTS = {"node_modules", "dist", ".git", "--port", ".vitepress"}

PORTS = ["悉尼", "墨尔本", "布里斯班", "弗里曼特尔", "珀斯", "阿德莱德", "奥克兰"]
ROW = re.compile(r"^\s*\|?\s*([\u4e00-\u9fff]{2,4})\s*[→\-]>?\s*([\u4e00-\u9fff]{2,4})\s*\|")
HDR = re.compile(r"^\s*\|\s*(航线|项目|参数|起运港|港口)")
SEP = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def fact_class(header: str) -> str:
    """按**表头**判定事实类别。

    为什么必要：两张表的列名不同，比的是不同事实 ——
      冷冻柜操作流程  | 航线 | 20RF | 40RF | 上浮比例 | 时效 |   → 冷藏
      危化品运输要求  | 航线 | 20GP(DG) | 40GP(DG) | 系数 | 时效 | → 危险品
    首版没读表头，把「危险品系数 +100%」当成「冷藏上浮 +80%」比，产出了 3 条误报。
    """
    h = header or ""
    if "RF" in h or "上浮比例" in h or "冷藏" in h or "冷冻" in h:
        return "冷藏"
    if "DG" in h or "系数" in h or "危险品" in h or "危化" in h:
        return "危险品"
    return "普货"
DAYS = re.compile(r"(\d+)\s*天")
REEFFER_UPLIFT = re.compile(r"\+\s*(\d+)\s*%")
LABEL_FAST, LABEL_SLOW = "快船", "普船"


def md_files(root: Path):
    if not root.exists():
        return []
    out = []
    for p in root.rglob("*.md"):
        if any(s in p.parts for s in SKIP_PARTS):
            continue
        try:
            if p.stat().st_size < 200:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def classify_row(line: str, days: list[str]):
    """返回 [(kind, days, confidence)]。kind ∈ {快船, 普船}。

    置信度：high = 行内明确写了「快船/普船」；mid = 按列位置推断；
            low = 只有一个天数且无标注（不知道是快船还是普船）
    """
    explicit_fast = LABEL_FAST in line
    explicit_slow = LABEL_SLOW in line and line.count(LABEL_SLOW) >= 1
    if explicit_fast or explicit_slow:
        out = []
        # 逐个数找它后面最近的标签
        for m in DAYS.finditer(line):
            tail = line[m.end():m.end() + 12]
            if LABEL_SLOW in tail:
                out.append((LABEL_SLOW, m.group(1), "high"))
            elif LABEL_FAST in tail or LABEL_FAST in line[:m.start()][-12:]:
                out.append((LABEL_FAST, m.group(1), "high"))
            else:
                out.append((LABEL_FAST if not out else LABEL_SLOW, m.group(1), "mid"))
        return out
    if len(days) >= 2:
        # 表格惯用：第一个是快船、第二个是普船（同表内保持一致）
        return [(LABEL_FAST, days[0], "mid"), (LABEL_SLOW, days[1], "mid")]
    if len(days) == 1:
        return [(LABEL_FAST, days[0], "low")]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(SRC))
    ap.add_argument("--json", default=None)
    ap.add_argument("--accepted", default=str(Path(__file__).resolve().parent / "kb_consistency_accepted.json"))
    args = ap.parse_args()

    root = Path(args.source)
    files = md_files(root)
    if not files:
        print(f"❌ 找不到源 md：{root}（用 --source 或 KB_SOURCE_DIR 指定）", file=sys.stderr)
        return 2

    facts = defaultdict(list)      # (origin, dest, kind) -> [(days, doc, line, conf)]
    uplift = defaultdict(list)     # (origin, dest) -> [(pct, doc, line)]
    for p in files:
        header = ""
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if HDR.match(line):
                header = line
                continue
            m = ROW.match(line)
            if not m:
                continue
            origin, dest = m.group(1), m.group(2)
            cls = fact_class(header)
            if dest not in PORTS and origin not in ("宁波", "上海", "深圳", "广州", "青岛", "天津"):
                continue
            days = DAYS.findall(line)
            for kind, val, conf in classify_row(line, days):
                facts[(cls, origin, dest, kind)].append((val, p.name, i, conf))
            um = REEFFER_UPLIFT.search(line)
            if um and cls in ("冷藏", "危险品"):
                uplift[(cls, origin, dest)].append((um.group(1), p.name, i))

    try:
        accepted = set(json.loads(Path(args.accepted).read_text()).get("accepted", []))
    except (OSError, json.JSONDecodeError):
        accepted = set()

    conflicts, suppressed, ok = [], [], 0
    for (cls, origin, dest, kind), vals in sorted(facts.items()):
        uniq = sorted({v[0] for v in vals}, key=lambda x: int(x))
        key = f"[{cls}] {origin}→{dest}/{kind}"
        if len(uniq) <= 1:
            ok += 1
            continue
        rec = {"fact": key, "values": uniq,
               "confidences": sorted({v[3] for v in vals}),
               "sources": [{"doc": v[1], "line": v[2], "days": v[0], "conf": v[3]} for v in vals]}
        (suppressed if key in accepted else conflicts).append(rec)

    upl_conf = []
    for (cls, origin, dest), vals in sorted(uplift.items()):
        uniq = sorted({int(v[0]) for v in vals})
        if len(uniq) > 1:
            upl_conf.append({"fact": f"[{cls}] {origin}→{dest}/上浮或系数%", "values": uniq,
                             "sources": [{"doc": v[1], "line": v[2], "pct": v[0]} for v in vals]})

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    out = {"generated_at": stamp, "source": str(root), "files_scanned": len(files),
           "facts_compared": len(facts), "facts_consistent": ok,
           "conflicts": conflicts, "suppressed": suppressed, "uplift_conflicts": upl_conf,
           "limits": ["只覆盖结构化可比的事实（航线时效 / 冷藏上浮%）",
                      "散文里的矛盾抓不到",
                      "「一致但都错」抓不到",
                      "low 置信=只有一个天数且无标注，可能误判"]}

    print(f"扫描 {len(files)} 篇源 md；比对 {len(facts)} 条航线时效事实，一致 {ok} 条")
    print(f"\n=== 冲突 {len(conflicts)} 条 ===")
    for c in conflicts:
        print(f"  ⚠️ {c['fact']}：取值 {c['values']}（置信 {c['confidences']}）")
        for s in c["sources"]:
            print(f"       {s['days']}天  ← {s['doc']}:{s['line']}")
    if upl_conf:
        print(f"\n=== 冷藏上浮比例冲突 {len(upl_conf)} 条 ===")
        for c in upl_conf:
            print(f"  ⚠️ {c['fact']}：{c['values']}")
            for s in c["sources"]:
                print(f"       +{s['pct']}%  ← {s['doc']}:{s['line']}")
    if suppressed:
        print(f"\n（已登记抑制 {len(suppressed)} 条：{', '.join(c['fact'] for c in suppressed)}）")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"\n报告：{args.json}")
    return 1 if (conflicts or upl_conf) else 0


if __name__ == "__main__":
    raise SystemExit(main())