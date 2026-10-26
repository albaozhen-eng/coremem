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
import hashlib
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from plugmem_config import plugmem_cfg, session_status

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".coremem"))
MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", Path.home() / ".openclaw" / "workspace-main" / "memory"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))
L2_INLINE_RETRY = 2      # 首写回查为 0 时的内联重试次数（之后转 buffer）
L2_INLINE_BACKOFF = 0.5  # 秒，线性递增
BUFFER_FILE = DATA_DIR / "memory_buffer.jsonl"
UNIFIED_FACTS_FILE = MEMORY_DIR / "unified-facts.jsonl"

_PC = plugmem_cfg()  # env > ~/.openclaw/openclaw.json > default
SEMANTIC_BASE_URL = _PC["base_url"]
SEMANTIC_API_KEY = _PC["api_key"]
SEMANTIC_GRAPH_ID = _PC["graph_id"]

PLUGMEM_TIMEOUT = 15  # seconds

# P1：写入账本。每条成功写入 L1 的事实落一行，供 memory-ops 的 write_fidelity 探针核对。
WRITE_LEDGER = DATA_DIR / "write_ledger.jsonl"


def norm_line(text: str) -> str:
    """NFKC 归一 + 折叠空白。

    必须与 memory-ops 的 mo_common.norm() 保持一致，否则 sha 对不上。
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def _line_sha(text: str) -> str:
    """L1 行指纹（16 位十六进制）。与 memory-ops 的 mo_common.line_sha() 同算法。"""
    return hashlib.sha256(norm_line(text).encode("utf-8")).hexdigest()[:16]

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

def append_daily_log_ex(content: str, tags: list[str] = None) -> dict | None:
    """追加到今日 daily log 并返回可回溯坐标。

    Returns:
        {"ref": "2026-10-22.md:123", "line": 123, "sha": "<16hex>", "text": "<整行>"}
        失败返回 None
    """
    ts = now_iso()
    log_path = daily_log_path()
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    entry = f"\n- {ts}{tag_str} {content}\n"          # 与 append_daily_log 完全一致
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        print(f"[remember] ❌ daily log 写入失败（磁盘/权限问题）: {e}", file=sys.stderr)
        return None

    prefix = f"- {ts}{tag_str} "
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    line_no = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(prefix):
            line_no = i + 1
            break
    if line_no is None:
        return None
    line_text = lines[line_no - 1]
    return {"ref": f"{log_path.name}:{line_no}", "line": line_no,
            "file": log_path.name, "sha": _line_sha(line_text), "text": line_text}


def near_dup_candidates(text: str, limit: int = 8) -> int:
    """诊断线索：估计 L2 里有多少条与本文本高度重叠的内容。

    **这不是"被去重"的证明** —— PlugMem 的去重判据（同 tag 最近 5 个、cosine > 0.92）
    在服务端内部，客户端拿不到相似度。这里只用词面重叠做粗估，
    目的是给人工判断提供线索，不得用于自动豁免告警。
    """
    try:
        import re as _re
        toks = _re.findall(r"[A-Za-z0-9_.\-]{4,}|[\u4e00-\u9fff]{3,}", text)
        if not toks:
            return 0
        probes = [t for t in sorted(set(toks), key=len, reverse=True) if 3 <= len(t) <= 10][:2]
        if not probes:
            return 0
        headers = {"X-API-Key": SEMANTIC_API_KEY} if SEMANTIC_API_KEY else {}
        seen, hits = set(), 0
        norm = lambda t: "".join((t or "").split()).lower()
        target = norm(text)
        for probe in probes:
            url = (f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/search"
                   f"?q={urllib.parse.quote(probe)}&limit=20")
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
                nodes = json.loads(r.read()).get("nodes", [])
            for n in nodes:
                nid = n.get("semantic_id")
                if nid in seen:
                    continue
                seen.add(nid)
                if norm(n.get("text")) == target:
                    return -1        # 完全同文本已存在（与"异常"区分开）
                # 词面重叠率粗估
                a = set(_re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_.\-]{3,}", target))
                b = set(_re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_.\-]{3,}", norm(n.get("text"))))
                if a and b and len(a & b) / len(a) >= 0.8:
                    hits += 1
        return hits
    except Exception:  # noqa: BLE001
        return None          # 查询失败 → 不提供线索（此前与"完全同文本"共用 -1，
                             # 等于把"我查不到"说成"最可能是重复"，正是要避免的那种"替异常找理由"）


def append_ledger(ref: str, sha: str, l2_status: str, l2_session_id: str = "",
                  content: str = "", dup_candidates=None) -> bool:
    """P1：写一条写入账本（append-only，best-effort，不阻断主流程）。"""
    entry = {
        "ts": now_iso(),
        "ref": ref,
        "sha": sha,
        "l2_status": l2_status,          # ok / buffered / failed
        "l2_session_id": l2_session_id,  # 实际发给 PlugMem 的 session_id
        "text_head": (content or "")[:80],
    }
    if dup_candidates is not None:
        entry["dup_candidates"] = dup_candidates
    try:
        WRITE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(WRITE_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[remember] ⚠️ 账本写入失败: {e}", file=sys.stderr)
        return False


def append_daily_log(content: str, tags: list[str] = None) -> bool:
    """追加内容到今日 daily log（保留原 bool 契约）

    Returns:
        True 成功，False 失败（磁盘故障）
    """
    ok = append_daily_log_ex(content, tags) is not None
    print(f"[remember] {'✅ daily log 已写入' if ok else '❌ daily log 写入失败'}: {daily_log_path()}")
    return ok


def source_line_ok(results: dict) -> bool:
    """账本辅助：L1 已拿到坐标即视为可回溯。"""
    return bool(results.get("source_line"))


# ============================================================
# 步骤 ②：尽力写语义索引（失败→buffer）
# ============================================================

def write_to_semantic_index(content: str, tags: list[str] = None,
                            event_time: str = None, event_time_precision: str = None,
                            confidence: float = 0.8,
                            source_line: str = None, sha: str = None) -> bool:
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
    # P1：provenance 走 session_id。PlugMem 没有 metadata 字段，但 session_id 是
    # 自由字符串且会盖在该次插入的所有节点上、并在 /search 里返回（实测可完整往返）。
    # 格式：l1:<file>:<line>#<sha16>。tags 保持语义纯净，不塞 ref（否则每条记忆
    # 会多出 2 个 tag 节点）。
    session_id = None
    if source_line:
        session_id = f"l1:{source_line}" + (f"#{sha}" if sha else "")
    payload = {"mode": "structured", "semantic": [sem]}
    if session_id:
        payload["session_id"] = session_id
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


def fallback_to_buffer(content: str, tags: list[str] = None,
                       source_line: str = None, sha: str = None) -> bool:
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
    if source_line:
        entry["source_line"] = source_line
        entry["sha"] = sha
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
        "source_line": None,
        "sha": None,
        "l2_status": None,
        "dup_candidates": None,
    }
    
    # ① daily log（必成，失败即报错）—— P1：同时拿到 (ref, line, sha)
    loc = append_daily_log_ex(content, tags)
    results["daily_log"] = loc is not None
    if not results["daily_log"]:
        print("[remember] ❌ SoT 写入失败，终止", file=sys.stderr)
        return results
    results["source_line"] = loc["ref"]
    results["sha"] = loc["sha"]
    
    # ② 语义索引 + P3 回查
    #   关键顺序：**先回查、再决定是否入 buffer**。
    #   反过来写（先 buffer 再撤销）会在磁盘上留下条目 —— 第一版就是这么错的，
    #   测试用例 B1 抓到：内存标志翻转了，但 buffer 文件里仍有该条目，重试照样重复写入。
    attempted = write_to_semantic_index(
        content, tags, event_time, event_time_precision, confidence,
        source_line=loc["ref"], sha=loc["sha"])
    sid = f"l1:{loc['ref']}#{loc['sha']}"
    cnt, perr = session_status(sid)

    if cnt is not None and cnt >= 1:
        # 已确认落地（不管接口当初报什么）—— 唯一不写 buffer 的成功态
        l2_status = "ok"
        results["semantic_index"] = True
    elif cnt == 0:
        if attempted:
            # 接口报成功但回查查不到。这是**必须让人看到**的状态。
            #
            # 曾把这里一律判成 dedup_skipped（"预期去重"）——**那是错的**：
            # 复核指出客户端拿不出相似度证据（PlugMem 的 0.92 判定在服务端内部，
            # /retrieve 只返回推理提示词、不给相似度），于是 2026-10-22.md:204 那类
            # 真实静默丢失会被这个标签永久掩盖。现在默认判 missing，会告警。
            #
            # 2026-10-26 修正：原理由「重试对确实被丢弃的条目无效，会变成永久重试循环」
            # 已被两次实测推翻：10-25 四条被判 missing 的重放 4/4 落地；
            # 10-26 六条同批失败（压测把服务端打满）重放 6/6 落地。
            # 所以 missing 多数是**瞬时失败**，不是去重。改为内联有界重试；
            # 仍不成功才转 buffer（标 failed，由 buffer 的 MAX_L2_RETRY 兜底），
            # 只有 buffer 也放弃时才由它写 missing —— 那时才算终态。
            for _i in range(L2_INLINE_RETRY):
                time.sleep(L2_INLINE_BACKOFF * (_i + 1))
                write_to_semantic_index(
                    content, tags, event_time, event_time_precision, confidence,
                    source_line=loc["ref"], sha=loc["sha"])
                _c, _ = session_status(sid)
                if _c is not None and _c >= 1:
                    cnt = _c
                    break
            if cnt is not None and cnt >= 1:
                l2_status = "ok"
                results["semantic_index"] = True
                print("[remember] 重试后落地（首写回查为 0）", file=sys.stderr)
            else:
                l2_status = "failed"
                results["semantic_index"] = False
                results["dup_candidates"] = near_dup_candidates(content)
                results["buffer"] = fallback_to_buffer(
                    content, tags, source_line=loc["ref"], sha=loc["sha"])
                print("[remember] 重试 %d 次仍未落地，转缓冲区继续重试（近似候选 %s 个）" % (L2_INLINE_RETRY, results["dup_candidates"]), file=sys.stderr)
        else:
            l2_status = "failed"
            results["semantic_index"] = False
            results["buffer"] = fallback_to_buffer(content, tags,
                                                   source_line=loc["ref"], sha=loc["sha"])
    else:
        # 回查本身不确定：报错时不入 buffer（入则可能重复），但如实标注待事后追查
        l2_status = "ok_unverified" if attempted else "failed_unverified"
        results["semantic_index"] = bool(attempted)
        if not attempted:
            results["buffer"] = fallback_to_buffer(content, tags,
                                                   source_line=loc["ref"], sha=loc["sha"])
        else:
            print(f"[remember] ℹ️ 回查失败（{perr}），记为 ok_unverified，待 dup_guard 追查",
                  file=sys.stderr)

    results["l2_status"] = l2_status
    append_ledger(loc["ref"], loc["sha"], l2_status, l2_session_id=sid, content=content,
                  dup_candidates=results.get("dup_candidates"))
    
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
    parser.add_argument("--event-time-precision", type=str, default="",
                        dest="event_time_precision",
                        help="事实时间精度：day / month / year（缺省按 day）")
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
    # P3：支持调用方指定精度（原先硬编码 day，月份级事实会被记成日级）
    event_time_precision = (args.event_time_precision or "day") if event_time else None
    
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
