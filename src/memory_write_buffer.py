#!/usr/bin/env python3
"""
记忆写入缓冲区管理器 v2.0
============================================
当语义索引写入失败时，将内容降级写入缓冲区。
服务恢复后自动重试，保证不丢记忆。

用法：
  python3 src/memory_write_buffer.py write "<内容>"     # 追加到缓冲区
  python3 src/memory_write_buffer.py list                # 查看缓冲区（带统计）
  python3 src/memory_write_buffer.py flush               # 逐个重试写入
  python3 src/memory_write_buffer.py --check             # 检查缓冲区（exit 1=有待处理）

配置：
  所有路径和连接信息通过环境变量配置。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from plugmem_config import plugmem_cfg, session_status

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".openclaw"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))
MAX_L2_RETRY = 3  # L2 回查未落地时的最大重试次数；超过才判 missing
BUFFER_PATH = DATA_DIR / "memory_buffer.jsonl"
CODE_MEM_PATH = DATA_DIR / "codex_memory_buffer.jsonl"

_PC = plugmem_cfg()  # env > ~/.openclaw/openclaw.json > default
SEMANTIC_BASE_URL = _PC["base_url"]
SEMANTIC_API_KEY = _PC["api_key"]
SEMANTIC_GRAPH_ID = _PC["graph_id"]


def _http_write_memory(text: str, tags=None, source_line=None, sha=None) -> bool:
    """直连语义索引后端 API 写入"""
    url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/memories"
    # source 必须是 PlugMem 枚举值（failure_delta/correction/merged/repeated_lookup/explicit/business）
    # 曾用 "buffer_flush" → 422 且被静默吞掉，缓冲重试永远失败。降级重放按显式事实入库。
    payload = {
        "mode": "structured",
        "semantic": [{
            "semantic_memory": text,
            "tags": tags or [],
            "source": "explicit",
            "confidence": 0.7,
        }],
    }
    # P1：重放时带上 provenance（与 remember.py 同一格式），否则降级重试会把来源丢掉
    if source_line:
        payload["session_id"] = f"l1:{source_line}" + (f"#{sha}" if sha else "")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if SEMANTIC_API_KEY:
        headers["X-API-Key"] = SEMANTIC_API_KEY
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"  ↳ HTTP {e.code}: {e.read()[:200].decode(errors='replace')}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ↳ {type(e).__name__}: {e}", file=sys.stderr)
        return False


LEDGER = DATA_DIR / "write_ledger.jsonl"


def _ledger_has(ref, sha):
    """P1 账本里是否已有 (ref, sha) 且 L2 写入成功 —— 用于幂等判定。"""
    if not (ref and sha) or not LEDGER.exists():
        return False
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("ref") == ref and d.get("sha") == sha and d.get("l2_status") == "ok":
                    return True
    except OSError:
        return False
    return False


def _ledger_missing(entry) -> None:
    """把「终态缺失」写进账本，供 memory_health 的 dup_guard 探针告警。"""
    ref, sha = entry.get("source_line"), entry.get("sha")
    if not (ref and sha):
        return
    try:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),   # 带时区，与 remember.py 一致
                "ref": ref, "sha": sha, "l2_status": "missing",
                "l2_session_id": f"l1:{ref}#{sha}",
                "text_head": (entry.get("text") or "")[:80],
                "note": "buffer-flush: 200 但回查未落地，已重试 %d 次后判终态" % int(entry.get("l2_retry", 0)),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _already_in_l2(entry):
    """这条缓冲条目是否已经写进过 L2（防止重放重复写入）。

    优先级（可靠性从高到低）：
      1. session_id 回查 GET /sessions/{sid} → count>=1 —— **确定**
      2. 账本命中 (ref+sha+l2_status=ok) —— 只能说明「曾声明成功」
      3. 文本比对（search）—— **尽力而为，不可靠**
    返回 (是否已存在, 依据说明)。
    """
    ref, sha = entry.get("source_line"), entry.get("sha")
    if ref and sha:
        cnt, _err = session_status(f"l1:{ref}#{sha}")
        if cnt is not None and cnt >= 1:
            return True, "session 回查命中"
    # 账本只记「声明成功」，遇到「报失败但已生效」会漏判，故仅作次选
    if _ledger_has(ref, sha):
        return True, "账本命中"
    text = (entry.get("text") or "").strip()
    if not text:
        return False, ""
    # 注意：这是**尽力而为**的兜底。PlugMem 的 search 是按关键词/向量匹配的，
    # 单个探测词可能搜不到（实测：目标记忆就在 L2 里，但用其中一个短语搜却是 0 条）。
    # 所以多试几个词、放大 limit，降低漏检 → 降低重复写入的概率。
    # 有 provenance 的条目走账本（确定性），永远不会走到这里。
    import re as _re
    toks = _re.findall(r"[A-Za-z0-9_.\-]{4,}|[\u4e00-\u9fff]{2,}", text)
    if not toks:
        return False, ""
    # 探测词要「短到能被 search 匹配」。曾经取最长 token（12~14 字的整句），
    # PlugMem 的 search 不是子串匹配，于是搜不到 → 漏检 → 重复写入。
    # 实测那条记忆确实在 L2 里，但用整句当查询是 0 条。
    probes = [t for t in sorted(set(toks), key=len) if 3 <= len(t) <= 8][:4]
    if not probes:
        probes = sorted(set(toks), key=len)[:2]
    norm = lambda t: "".join((t or "").split())
    headers = {"X-API-Key": SEMANTIC_API_KEY} if SEMANTIC_API_KEY else {}
    for probe in probes:
        try:
            url = (f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/search"
                   f"?q={urllib.parse.quote(probe)}&limit=20")
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
                nodes = json.loads(r.read()).get("nodes", [])
        except Exception:  # noqa: BLE001
            continue
        for n in nodes:
            if norm(n.get("text")) == norm(text):
                return True, "文本命中"
    return False, ""


def write_to_buffer(text, buffer_path=None, source_line=None, sha=None):
    """追加一条记忆到缓冲区（P1：可带 provenance）"""
    if buffer_path is None:
        buffer_path = BUFFER_PATH
    buffer_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"text": text, "timestamp": datetime.now().isoformat()}
    if source_line:
        entry["source_line"] = source_line
        entry["sha"] = sha
    with open(buffer_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def list_buffer(buffer_path=None):
    """列出缓冲区内容（带统计）"""
    if buffer_path is None:
        buffer_path = BUFFER_PATH
    if not buffer_path.exists():
        print("缓冲区为空")
        return 0
    entries = []
    with open(buffer_path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not entries:
        print("缓冲区为空")
        return 0
    print(f"📦 {buffer_path.name}: {len(entries)} 条待处理")
    for i, e in enumerate(entries, 1):
        print(f"  {i}. [{e.get('timestamp', '?')[:19]}] {e.get('text', '')[:80]}")
    return len(entries)


def flush_buffer(buffer_path=None):
    """逐个重试，统计成功/失败"""
    if buffer_path is None:
        buffer_path = BUFFER_PATH
    if not buffer_path.exists():
        print("缓冲区为空，无需 flush")
        return True

    entries = []
    with open(buffer_path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not entries:
        buffer_path.unlink(missing_ok=True)
        return True

    success, fail, skipped = 0, 0, 0
    kept = []          # 只保留真正失败的条目
    for entry in entries:
        text = entry.get("text", "")
        if not text:
            continue
        # 幂等：已经写进过 L2 的条目直接算成功，不重复写
        try:
            dup, why = _already_in_l2(entry)
        except Exception:  # noqa: BLE001
            dup, why = False, ""
        if dup:
            skipped += 1
            success += 1
            continue
        try:
            if _http_write_memory(text, entry.get("tags") if isinstance(entry, dict) else None,
                                  entry.get("source_line"), entry.get("sha")):
                # P3：写完回查确认；只在确认落地时才移出缓冲区
                ref, sha = entry.get("source_line"), entry.get("sha")
                if ref and sha:
                    cnt, _err = session_status(f"l1:{ref}#{sha}")
                    if cnt == 0:
                        # 接口 200 但回查无节点。原实现判定「终态缺失」直接丢弃，理由是
                        # 「重试极可能无效」。该假设已被证伪：2026-10-25 对 4 条被判终态缺失的
                        # 条目按同样参数重放，4/4 全部落地（cnt=1）。改为有界重试：
                        # 前 MAX_L2_RETRY 次留在缓冲区等下一轮，超过才记 missing。
                        n_try = int(entry.get("l2_retry", 0)) + 1
                        entry["l2_retry"] = n_try
                        if n_try < MAX_L2_RETRY:
                            fail += 1
                            kept.append(entry)
                            print(f"  ↻ [{entry.get('timestamp','?')[:19]}] 接口 200 但未落地，"
                                  f"第 {n_try}/{MAX_L2_RETRY} 次，留待重试", file=sys.stderr)
                        else:
                            fail += 1
                            _ledger_missing(entry)
                            print(f"  ⚠️ [{entry.get('timestamp','?')[:19]}] 重试 {n_try} 次仍未落地"
                                  f" → 记为 missing 并告警", file=sys.stderr)
                        continue
                success += 1
            else:
                fail += 1
                kept.append(entry)
                print(f"  ❌ [{entry.get('timestamp', '?')[:19]}] 语义索引写入失败", file=sys.stderr)
        except Exception as e:
            fail += 1
            kept.append(entry)
            print(f"  ❌ [{entry.get('timestamp', '?')[:19]}] {e}", file=sys.stderr)

    print(f"✅ {success}/{len(entries)} 成功", end="")
    if skipped:
        print(f"（其中 {skipped} 条幂等跳过）", end="")
    # 关键修复：只把失败的写回缓冲区。
    # 旧实现在 fail>0 时保留全部条目 —— 成功的那几条下次会被重复写入 L2。
    if kept:
        tmp = buffer_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(buffer_path)
        print(f"，{fail} 失败保留在缓冲区（成功的已移出）")
    else:
        print("，清空缓冲区")
        buffer_path.unlink(missing_ok=True)

    return fail == 0


def flush_all():
    """清空所有缓冲区"""
    for p in [BUFFER_PATH, CODE_MEM_PATH]:
        if p.exists():
            print(f"📤 处理 {p.name}...")
            flush_buffer(p)


def check(buffer_path=None):
    """返回 0=无待处理，1=有待处理"""
    if buffer_path is None:
        buffer_path = BUFFER_PATH
    if not buffer_path.exists():
        sys.exit(0)
    with open(buffer_path) as f:
        count = sum(1 for _ in f)
    if count == 0:
        sys.exit(0)
    print(f"⚠️ {buffer_path.name}: {count} 条待处理")
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "write" and len(sys.argv) > 2:
        write_to_buffer(" ".join(sys.argv[2:]))
    elif cmd == "list":
        list_buffer()
    elif cmd == "flush":
        flush_all()
    elif cmd == "--check":
        check()
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)