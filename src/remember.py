#!/usr/bin/env python3
"""
remember.py — 记忆统一写入入口 v1.0

用法：
  python3 src/remember.py "今天完成了项目迁移" --tags 决策,工程
  python3 src/remember.py "修复了语义索引写入问题" --tags 运维,记忆系统

三层写入：
  ① 同步写 daily log（必成，失败报错）
  ② 尽力写语义索引（失败→buffer 落盘，30min cron 自动重试）
  ③ 追加 unified-facts.jsonl（夜间 cron 提取到 MEMORY.md AUTO 区）

设计原则：
  - agent 只需调一次，不再手动记三层
  - 任一层失败不阻断主流程（除 daily log，那是 SoT）
  - buffer 机制复用已有的 memory_write_buffer.py

配置：
  所有路径和连接信息通过环境变量配置，默认值如下：
  - MEMORY_HOME: 记忆系统根目录（默认 ~/.coremem）
  - MEM_SEMANTIC_BASE_URL: 语义索引后端地址（默认 http://127.0.0.1:8089）
  - MEM_SEMANTIC_API_KEY: 语义索引 API Key（默认空）
  - MEM_SEMANTIC_GRAPH_ID: 语义索引图 ID（默认 default）
  - MEM_DATA_DIR: 数据目录（默认 $MEMORY_HOME/data）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from plugmem_config import plugmem_cfg

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".coremem"))
MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", MEMORY_HOME / "memory"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))
BUFFER_FILE = DATA_DIR / "memory_buffer.jsonl"
UNIFIED_FACTS_FILE = MEMORY_DIR / "unified-facts.jsonl"

_PC = plugmem_cfg()  # env > ~/.openclaw/openclaw.json > default
SEMANTIC_BASE_URL = _PC["base_url"]
SEMANTIC_API_KEY = _PC["api_key"]
SEMANTIC_GRAPH_ID = _PC["graph_id"]

PLUGMEM_TIMEOUT = 15  # seconds

TZ = timezone(timedelta(hours=8))


def today_str() -> str:
    """返回今天的日期字符串 YYYY-MM-DD"""
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_iso() -> str:
    """返回当前 ISO 时间戳"""
    return datetime.now(TZ).isoformat(timespec="seconds")


def daily_log_path(date_str: str = None) -> Path:
    """返回指定日期的 daily log 文件路径"""
    if date_str is None:
        date_str = today_str()
    return MEMORY_DIR / f"{date_str}.md"


# ============================================================
# 步骤 ①：同步写 daily log（SoT，必成）
# ============================================================

def append_daily_log(content: str, tags: list[str] = None) -> bool:
    """追加内容到今日 daily log
    
    Returns:
        True 成功，False 失败（磁盘故障）
    """
    ts = now_iso()
    log_path = daily_log_path()
    
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    entry = f"\n- {ts}{tag_str} {content}\n"
    
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"[remember] ✅ daily log 已写入: {log_path}")
        return True
    except Exception as e:
        print(f"[remember] ❌ daily log 写入失败（磁盘/权限问题）: {e}", file=sys.stderr)
        return False


# ============================================================
# 步骤 ②：尽力写语义索引（失败→buffer）
# ============================================================

def write_to_semantic_index(content: str, tags: list[str] = None,
                            event_time: str = None, event_time_precision: str = None,
                            confidence: float = 0.8) -> bool:
    """直接 HTTP 调用语义索引后端 API（兼容 PlugMem 格式）

    API 格式：
      POST /api/v1/graphs/{graph_id}/memories
      body: {"mode": "structured", "semantic": [{"semantic_memory": ..., "tags": [...], "source": "explicit", "confidence": 0.8}]}

    P0 双时钟：event_time 为事实所指时间（ISO 日期），未提供时服务端回退 created_at。
    P2 写入分级：confidence 按写入源分级（显式 0.8 / 自动捕获 0.6 / 更新 0.5）。

    Returns:
        True 成功，False 失败
    """
    url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/memories"
    sem = {
        "semantic_memory": content,
        "tags": tags or [],
        "source": "explicit",
        "confidence": confidence,
    }
    if event_time:
        sem["event_time"] = event_time
        sem["event_time_precision"] = event_time_precision or "day"
    payload = {"mode": "structured", "semantic": [sem]}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if SEMANTIC_API_KEY:
        headers["X-API-Key"] = SEMANTIC_API_KEY
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=PLUGMEM_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                print(f"[remember] ✅ 语义索引已写入")
                return True
            print(f"[remember] ⚠️ 语义索引返回 {resp.status}: {body[:200]}", file=sys.stderr)
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"[remember] ⚠️ 语义索引 HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[remember] ⚠️ 语义索引连接失败: {e}", file=sys.stderr)
        return False


def fallback_to_buffer(content: str, tags: list[str] = None) -> bool:
    """语义索引写入失败时降级到 buffer
    
    Returns:
        True 成功写入 buffer，False 失败
    """
    entry = {
        "timestamp": now_iso(),
        "text": content,
        "tags": tags or [],
        "source": "remember.py",
    }
    try:
        BUFFER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUFFER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[remember] ⚠️ 已降级到 buffer: {BUFFER_FILE}（30min cron 自动重试）")
        return True
    except Exception as e:
        print(f"[remember] ❌ buffer 写入也失败了: {e}", file=sys.stderr)
        return False


# ============================================================
# 步骤 ③：追加 unified-facts.jsonl（夜间提取用）
# ============================================================

def append_unified_facts(content: str, tags: list[str] = None) -> bool:
    """追加事实到统一事实池，供夜间 cron 提取到 MEMORY.md AUTO 区"""
    entry = {
        "timestamp": now_iso(),
        "content": content,
        "tags": tags or [],
    }
    try:
        UNIFIED_FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(UNIFIED_FACTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[remember] ✅ 已入统一事实池: {UNIFIED_FACTS_FILE}")
        return True
    except Exception as e:
        print(f"[remember] ⚠️ 统一事实池写入失败: {e}", file=sys.stderr)
        return False


# ============================================================
# 主入口：remember()
# ============================================================

def remember(content: str, tags: list[str] = None, event_time: str = None,
             event_time_precision: str = None, confidence: float = 0.8) -> dict:
    """统一记忆写入——只调这一个"""
    if tags is None:
        tags = []
    
    results = {
        "content": content,
        "tags": tags,
        "daily_log": False,
        "semantic_index": False,
        "buffer": False,
        "unified_facts": False,
    }
    
    # ① daily log（必成，失败即报错）
    results["daily_log"] = append_daily_log(content, tags)
    if not results["daily_log"]:
        print("[remember] ❌ SoT 写入失败，终止", file=sys.stderr)
        return results
    
    # ② 语义索引（尽力，失败→buffer）
    results["semantic_index"] = write_to_semantic_index(
        content, tags, event_time, event_time_precision, confidence)
    if not results["semantic_index"]:
        results["buffer"] = fallback_to_buffer(content, tags)
    
    # ③ unified-facts
    results["unified_facts"] = append_unified_facts(content, tags)
    
    return results


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="remember — 记忆统一写入入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 src/remember.py "已完成项目迁移" --tags 决策,工程
  python3 src/remember.py "修复了语义索引写入问题" --tags 运维,记忆系统
  python3 src/remember.py "确认了配置变更" --tags 配置,部署
        """,
    )
    parser.add_argument("content", type=str, nargs="?", help="要记忆的内容")
    parser.add_argument("--tags", type=str, default="", help="逗号分隔的标签")
    parser.add_argument("--event-time", type=str, default="",
                        help="事实所指时间（ISO 日期，如 2024-03-15；缺省=今天）")
    parser.add_argument("--confidence", type=float, default=0.8,
                        help="写入置信度（显式 0.8 / 自动 0.6 / 更新 0.5），默认 0.8")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--status", action="store_true", help="检查各层状态")
    
    args = parser.parse_args()
    
    if args.status:
        print_status()
        return
    
    if not args.content:
        parser.print_help()
        sys.exit(1)
    
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    event_time = args.event_time or None
    event_time_precision = "day" if event_time else None
    
    if args.dry_run:
        print(f"[remember] DRY RUN — 内容: {args.content}")
        print(f"[remember] DRY RUN — 标签: {tags}")
        if event_time:
            print(f"[remember] DRY RUN — event_time: {event_time}")
        print(f"[remember] DRY RUN — 将写入: {daily_log_path()} / 语义索引 / unified-facts")
        return
    
    results = remember(args.content, tags, event_time, event_time_precision, args.confidence)
    
    # 汇总
    ok = sum([results["daily_log"],
              results["semantic_index"] or results["buffer"],
              results["unified_facts"]])
    print(f"\n[remember] 完成: daily_log={'✅' if results['daily_log'] else '❌'}  "
          f"semantic_index={'✅' if results['semantic_index'] else '⚠️buffer' if results['buffer'] else '❌'}  "
          f"unified_facts={'✅' if results['unified_facts'] else '❌'}")


def print_status():
    """检查各层状态"""
    print("📊 记忆系统状态")
    print(f"   时区: {TZ}")
    print(f"   日期: {today_str()}")
    
    # daily log
    log_path = daily_log_path()
    print(f"   daily log: {'✅ ' + str(log_path) if log_path.exists() else '❌ 不存在'}")
    
    # 语义索引
    try:
        import subprocess
        r = subprocess.run(
            ["curl", "-s", f"{SEMANTIC_BASE_URL}/api/v1/health"],
            capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout)
        print(f"   语义索引: ✅ {data.get('status', 'unknown')}")
    except Exception as e:
        print(f"   语义索引: ❌ {e}")
    
    # buffer
    if BUFFER_FILE.exists():
        lines = sum(1 for _ in open(BUFFER_FILE))
        print(f"   buffer: ⚠️ {lines} 条待重试")
    else:
        print(f"   buffer: ✅ 空")
    
    # unified facts
    if UNIFIED_FACTS_FILE.exists():
        lines = sum(1 for _ in open(UNIFIED_FACTS_FILE))
        print(f"   unified-facts: {lines} 条")
    else:
        print(f"   unified-facts: ❌ 不存在")


if __name__ == "__main__":
    main()