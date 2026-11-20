#!/usr/bin/env python3
"""
memory_rebuild_from_logs.py v1.0 — PlugMem 从 daily log 全量重建
=================================================================
当 PlugMem 数据损坏或需要从零恢复时，从 daily log 文件重建所有语义记忆。

用法：
  python3 scripts/memory_rebuild_from_logs.py               # 预览模式
  python3 scripts/memory_rebuild_from_logs.py --rebuild      # 执行重建
  python3 scripts/memory_rebuild_from_logs.py --days 30      # 只处理最近 30 天
  python3 scripts/memory_rebuild_from_logs.py --dry-run      # 预览要写入的事实

设计：
  - P2-1 容错底牌：PlugMem 彻底挂了，从 L1 重建 L2
  - 扫描 memory/YYYY-MM-DD.md → 提取日志行 → memory_add
  - 每行作为独立事实写入 PlugMem
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

MEMORY_DIR = Path.home() / ".coremem" / "memory"
OPENCLAW_BIN = str(Path.home() / ".npm-global" / "bin" / "openclaw")
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ)


def get_log_files(days: int = None) -> list[Path]:
    """获取 daily log 文件，从新到旧"""
    files = sorted(MEMORY_DIR.glob("????-??-??.md"), reverse=True)
    if days:
        cutoff = (now() - timedelta(days=days)).strftime("%Y-%m-%d")
        files = [f for f in files if f.stem >= cutoff]
    return files


def extract_facts(filepath: Path) -> list[str]:
    """从 daily log 提取记事项（- 开头，带时间戳的行）"""
    facts = []
    try:
        content = filepath.read_text(encoding="utf-8")
        for line in content.split("\n"):
            line = line.strip()
            # 匹配 "- 2026-08-09T09:25:00 [tag1, tag2] 内容" 或 "- 2026-08-09T09:25:00 内容"
            if line.startswith("- ") and re.match(r"\d{4}-\d{2}-\d{2}T", line[2:]):
                # 提取纯文本内容（去掉时间戳和标签）
                fact = line[2:]  # 去掉 "- "
                # 去掉时间戳部分 "2026-08-09T09:25:00+08:00 "
                fact = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:+-]+\s*", "", fact)
                # 去掉标签 "[tag, tag] "
                fact = re.sub(r"^\[.*?\]\s*", "", fact)
                if len(fact) > 10:
                    facts.append(fact)
    except Exception:
        pass
    return facts


def rebuild(days: int = None, dry_run: bool = False):
    """从 daily log 重建 PlugMem"""
    files = get_log_files(days=days)
    if not files:
        print("❌ 没有 daily log 文件")
        return

    print(f"📂 找到 {len(files)} 个 daily log 文件")
    if days:
        print(f"   范围: {files[-1].stem} ~ {files[0].stem}")

    total_facts = 0
    total_written = 0
    errors = 0

    for f in files:
        facts = extract_facts(f)
        if not facts:
            continue
        
        print(f"\n📄 {f.stem}: {len(facts)} 条事实")
        
        for fact in facts:
            total_facts += 1
            if dry_run:
                print(f"  DRY RUN: {fact[:80]}")
                continue
            
            try:
                r = subprocess.run(
                    ["python3", str(Path.home() / ".boss-agent" / "scripts" / "remember.py"),
                     fact, "--tags", "重建,历史"],
                    capture_output=True, text=True, timeout=15,
                )
                if "plugmem=✅" in (r.stdout + r.stderr):
                    total_written += 1
                else:
                    errors += 1
                    print(f"  ⚠️ {fact[:60]}...", file=sys.stderr)
            except Exception as e:
                errors += 1
                print(f"  ❌ {fact[:60]}... {e}", file=sys.stderr)

    print(f"\n{'DRY RUN — ' if dry_run else ''}完成:")
    print(f"  总事实: {total_facts}")
    if not dry_run:
        print(f"  已写入: {total_written}")
        print(f"  失败: {errors}")


def main():
    parser = argparse.ArgumentParser(description="从 daily log 重建 PlugMem")
    parser.add_argument("--rebuild", action="store_true", help="执行重建（默认预览）")
    parser.add_argument("--days", type=int, default=None, help="只处理最近 N 天（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    dry_run = args.dry_run or not args.rebuild
    rebuild(days=args.days, dry_run=dry_run)


if __name__ == "__main__":
    main()
