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

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".coremem"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))
BUFFER_PATH = DATA_DIR / "memory_buffer.jsonl"
CODE_MEM_PATH = DATA_DIR / "codex_memory_buffer.jsonl"

SEMANTIC_BASE_URL = os.environ.get("MEM_SEMANTIC_BASE_URL", "http://127.0.0.1:8089")
SEMANTIC_API_KEY = os.environ.get("MEM_SEMANTIC_API_KEY", "")
SEMANTIC_GRAPH_ID = os.environ.get("MEM_SEMANTIC_GRAPH_ID", "default")


def _http_write_memory(text: str) -> bool:
    """直连语义索引后端 API 写入"""
    url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/memories"
    payload = {
        "mode": "structured",
        "semantic": [{
            "semantic_memory": text,
            "tags": [],
            "source": "buffer_flush",
            "confidence": 0.7,
        }],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if SEMANTIC_API_KEY:
        headers["X-API-Key"] = SEMANTIC_API_KEY
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception:
        return False


def write_to_buffer(text, buffer_path=None):
    """追加一条记忆到缓冲区"""
    if buffer_path is None:
        buffer_path = BUFFER_PATH
    buffer_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"text": text, "timestamp": datetime.now().isoformat()}
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

    success, fail = 0, 0
    for entry in entries:
        text = entry.get("text", "")
        if not text:
            continue
        try:
            if _http_write_memory(text):
                success += 1
            else:
                fail += 1
                print(f"  ❌ [{entry.get('timestamp', '?')[:19]}] 语义索引写入失败", file=sys.stderr)
        except Exception as e:
            fail += 1
            print(f"  ❌ [{entry.get('timestamp', '?')[:19]}] {e}", file=sys.stderr)

    print(f"✅ {success}/{len(entries)} 成功", end="")
    if fail:
        print(f"，{fail} 失败保留在缓冲区")
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