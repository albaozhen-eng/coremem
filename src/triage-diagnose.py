#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""triage-diagnose.py — 捕获输入提纯诊断（只读，不改生产）

背景（2026-11-20 实测）：gate_stats total=4683 / gated=0 —— 闸门从未拦下任何东西。
根因三条：
  ① GATE_REGEX 按裸消息写（^好的$），但 extract_conversation 每行带「用户: /助手: 」前缀 → 永不 fullmatch
  ② 阈值要求 >60% 行命中才丢整段，真实会话夹杂实质内容 → 永达不到
  ③ 代码块围栏(```)、表格分隔线(|---|)、空内容行混进"对话"，污染判断且浪费 token

本脚本对真实会话做「过滤前 vs 过滤后」对比，只报数字与样例，不写任何文件。
"""
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "mc", os.path.expanduser("~/.coremem/scripts/memory_capture.py"))
MC = importlib.util.module_from_spec(SPEC)
sys.modules["mc"] = MC
try:
    SPEC.loader.exec_module(MC)
except SystemExit:
    pass

ROLE_RE = re.compile(r"^(用户|助手):\s*")
# 明确的非对话行（不含任何实质文字）
FENCE_RE = re.compile(r"^`{3,}\S*$")            # ``` 或 ```bash
TABLE_SEP_RE = re.compile(r"^\|[\s\-:|]+\|$")   # |---|---|
EMPTY_ROLE_RE = re.compile(r"^(用户|助手):\s*$")  # 助手:（空内容）


def classify_line(line: str):
    """返回 (是否非对话行, 剥前缀后是否命中噪声正则, 剥前缀后的正文)"""
    body = ROLE_RE.sub("", line).strip()
    non_conv = bool(FENCE_RE.match(body) or TABLE_SEP_RE.match(body)
                    or EMPTY_ROLE_RE.match(line) or not body)
    noise = bool(MC.GATE_REGEX.fullmatch(body) or MC.GATE_REGEX.match(body))
    return non_conv, noise, body


def analyze(path: Path, label: str):
    conv = MC.extract_conversation({"kind": "jsonl", "path": path},
                                   datetime(2020, 1, 1, tzinfo=timezone.utc), None)
    lines = [l.strip() for l in conv.split("\n") if l.strip()]
    if not lines:
        return None
    non_conv, noise, keep = [], [], []
    for l in lines:
        nc, ns, body = classify_line(l)
        if nc:
            non_conv.append(l)
        elif ns:
            noise.append(body)
        else:
            keep.append(l)
    chars_before = sum(len(l) for l in lines)
    chars_after = sum(len(l) for l in keep)
    return {
        "label": label, "total": len(lines),
        "non_conv": len(non_conv), "noise": len(noise), "keep": len(keep),
        "chars_before": chars_before, "chars_after": chars_after,
        "gate_now": MC.triage_gate(conv)[0],
        "noise_samples": noise[:5], "nonconv_samples": non_conv[:3],
    }


def main():
    root = Path(os.path.expanduser("~/.pi/agent/sessions"))
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    rows = []
    for p in files:
        r = analyze(p, p.stem[:28])
        if r:
            rows.append(r)
    print("=" * 78)
    print("捕获输入提纯诊断（只读）")
    print("=" * 78)
    print(f"{'会话':30} {'总行':>6} {'非对话':>7} {'噪声':>6} {'保留':>6} {'字符降幅':>9} {'现闸门':>7}")
    tb = ta = tt = tnc = tn = tk = 0
    for r in rows:
        drop = (1 - r["chars_after"] / r["chars_before"]) * 100 if r["chars_before"] else 0
        print(f"{r['label']:30} {r['total']:6} {r['non_conv']:7} {r['noise']:6} "
              f"{r['keep']:6} {drop:8.1f}% {'PASS' if r['gate_now'] else 'GATE':>7}")
        tt += r["total"]; tnc += r["non_conv"]; tn += r["noise"]; tk += r["keep"]
        tb += r["chars_before"]; ta += r["chars_after"]
    print("-" * 78)
    if tt:
        print(f"{'合计':30} {tt:6} {tnc:7} {tn:6} {tk:6} {(1-ta/tb)*100:8.1f}%")
        print(f"\n非对话行占比 {tnc/tt*100:.1f}%｜噪声行占比 {tn/tt*100:.1f}%"
              f"｜两者合计 {(tnc+tn)/tt*100:.1f}%")
        print(f"送 LLM 的字符量：{tb} → {ta}（降 {(1-ta/tb)*100:.1f}%）")
    print("\n=== 被判为非对话行的样例（应全部无实质文字）===")
    for r in rows[:2]:
        for s in r["nonconv_samples"]:
            print(f"  {s[:70]!r}")
    print("\n=== 剥前缀后命中噪声正则的样例（这些是该拦的）===")
    for r in rows:
        for s in r["noise_samples"][:3]:
            print(f"  {s[:70]!r}")
    print("\n注：本脚本不写任何文件；过滤规则仅剔除空行/代码围栏/表格分隔线/纯确认词，"
          "不触碰任何含实质文字的行。")


if __name__ == "__main__":
    main()
