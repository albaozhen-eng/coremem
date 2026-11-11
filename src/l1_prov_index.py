#!/usr/bin/env python3
"""P2：L1 前缀索引 —— 让「这条 L2 记忆有没有事实源」变成 O(1) 判断。

背景：L2 里约 42% 的节点回溯不到 L1。大样本诊断（144 个抽样）显示它们**中位长度是
可回溯节点的 5 倍**（200 vs 43 字符），且 80% 是跨源汇总型文本（"System status
snapshot"、"自动化运维与系统状态汇总"）。**这是一层增值的合成层，不是垃圾**
—— 所以「重建 L2」的方案被否掉了：重建会毁掉它。

但合成层缺一件事：**没有 provenance，无法被核对**。本索引让检索端能实时标注
每条 L2 记忆属于哪一类：
    derived     文本能在 L1 里找到（有事实源，可核对）
    synthesized 找不到（是生成/汇总内容，不可核对）

实现：把 L1 每行归一化后取前 60 字的 sha1 存进一个集合文件（data/l1_prefix_index.json）。
按 L1 mtime 做失效判断，L1 变了才重建（8.7k 行 → 秒级）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

INDEX = mo_common.DATA_DIR / "l1_prefix_index.json"
PREFIX = 60


def _h(text: str) -> str:
    """归一化后取前 PREFIX 字做 sha。

    **必须先剥掉 L1 行的 '- <时间戳> [tags] ' 前缀**：L2 节点里存的是正文，
    如果索引按整行（含前缀）算 sha，两者永远不相等 → 所有节点都会被误判成 synthesized。
    这个 bug 在 P2 交付里真实存在过（实测把 114 个正常事实全判成 synthesized）。
    """
    return hashlib.sha1(mo_common.content_of(mo_common.norm(text))[:PREFIX]
                        .encode("utf-8")).hexdigest()[:16]


def build(force: bool = False):
    """重建索引；L1 未变化且非 force 时直接复用。"""
    files = mo_common.daily_files()
    sig = f"{len(files)}:{max((f.stat().st_mtime for f in files), default=0):.0f}"
    if not force and INDEX.exists():
        try:
            cur = json.loads(INDEX.read_text())
            if cur.get("sig") == sig:
                cur["_hashes"] = set(cur.get("hashes", []))
                cur["reused"] = True
                return cur
        except (OSError, ValueError):
            pass
    hashes, n_lines = set(), 0
    for f in files:
        for line in mo_common.read_lines(f):
            n = mo_common.norm(line)
            if len(n) >= 12:
                hashes.add(_h(n))
                n_lines += 1
    payload = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sig": sig,
               "n_files": len(files), "n_lines": n_lines, "prefix": PREFIX,
               "hashes": sorted(hashes)}
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(payload, ensure_ascii=False))
    payload["_hashes"] = hashes
    payload["reused"] = False
    return payload


def classify(text: str, idx) -> str:
    """derived / synthesized / unknown（文本过短时不判）。"""
    n = mo_common.norm(text or "")
    if len(n) < 12:
        return "unknown"
    return "derived" if _h(n) in idx["_hashes"] else "synthesized"


if __name__ == "__main__":
    d = build(force="--force" in sys.argv)
    print(f"L1 前缀索引：{d['n_lines']} 行 / {d['n_files']} 文件；"
          f"{'复用' if d.get('reused') else '重建'}（sig={d['sig']}）")
    print(f"  文件: {INDEX}")
