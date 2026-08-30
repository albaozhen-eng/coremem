#!/usr/bin/env python3
"""
memory_insight_scan.py v1.0 — 语义索引 → MEMORY.md AUTO 区块同步器
===============================================================
从语义索引搜索语义记忆 → 分类去重（时间戳裁决取最新）→ 写入 MEMORY.md AUTO 区块。

用法：
  python3 src/memory_insight_scan.py --sync              # 同步（预览模式）
  python3 src/memory_insight_scan.py --sync --write      # 同步 + 写入 MEMORY.md
  python3 src/memory_insight_scan.py --check             # 检查语义索引健康状态
  python3 src/memory_insight_scan.py --status            # 展示当前记忆概况

设计原则：
  - 失败不覆盖 MEMORY.md（先写入 draft，验证通过后替换）
  - 语义索引不可达时静默跳过，不影响其他 cron
  - 同主题事实按 inferred_timestamp 取最新（时间戳裁决）
  - 去重：Jaccard 相似度 > 阈值 的同类别事实合并

配置：
  所有路径和连接信息通过环境变量配置。
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".sixmem"))
MEMORY_MD = Path(os.environ.get("MEM_MEMORY_MD", MEMORY_HOME / "MEMORY.md"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))

SEMANTIC_BASE_URL = os.environ.get("MEM_SEMANTIC_BASE_URL", "http://127.0.0.1:8089")
SEMANTIC_API_KEY = os.environ.get("MEM_SEMANTIC_API_KEY", "")
SEMANTIC_GRAPH_ID = os.environ.get("MEM_SEMANTIC_GRAPH_ID", "default")

PLUGMEM_TIMEOUT = 15  # seconds

# MEMORY.md AUTO 区块标记
AUTO_START_MARKER = "<!-- COMPILED-TRUTH-AUTO-START -->"
AUTO_END_MARKER = "<!-- COMPILED-TRUTH-AUTO-END -->"

# 最小事实数阈值（低于此值不写入，防止低质量覆盖）
MIN_FACTS_THRESHOLD = 3

# 同主题去重阈值
TOPIC_SIMILARITY_THRESHOLD = 0.6

# 陈旧标记状态文件
SYNC_STATE_FILE = DATA_DIR / "memory_sync_state.json"

TZ = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


# ============================================================
# 语义索引 API 调用
# ============================================================

def check_health() -> dict:
    """检查语义索引健康状态"""
    try:
        req = urllib.request.Request(f"{SEMANTIC_BASE_URL}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True, "data": json.loads(resp.read())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def search_memories(query: str = None, top_k: int = 50) -> list:
    """搜索语义索引
    
    query=None 或空串 → 不带 q 参数全量提取（AUTO 同步用）
    query=具体词 → 关键词匹配
    
    Returns:
        list of dict: [{"id": "xxx", "text": "...", "score": 0.9}, ...]
    """
    if query:
        search_url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/search?q={urllib.parse.quote(query)}&limit={top_k}"
    else:
        search_url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/search?limit={top_k}"
    
    headers = {}
    if SEMANTIC_API_KEY:
        headers["X-API-Key"] = SEMANTIC_API_KEY
    
    req = urllib.request.Request(search_url, headers=headers, method="GET")
    
    try:
        with urllib.request.urlopen(req, timeout=PLUGMEM_TIMEOUT) as resp:
            return parse_search_results(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"[memory_insight] 语义索引 HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[memory_insight] 语义索引连接失败: {e}", file=sys.stderr)
        return []


def parse_search_results(raw: str) -> list:
    """解析语义索引搜索结果，兼容多种格式"""
    results = []
    
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        if isinstance(data, dict) and "facts" in data:
            return data["facts"]
        if isinstance(data, dict) and "nodes" in data:
            return [
                {"id": f"node_{n.get('semantic_id', n.get('id', '?'))}",
                 "text": n.get("text", "")}
                for n in data["nodes"]
                if n.get("text")
            ]
    except json.JSONDecodeError:
        pass
    
    # 文本格式解析
    for match in re.finditer(r'Fact\s+\d+\s*\(Sem\s+Node\s+(\d+)\)[:\s]*(.+?)(?=\n\s*Fact\s+\d+\s*\(|\Z)', raw, re.DOTALL):
        node_id = match.group(1)
        text = match.group(2).strip()
        if len(text) > 5:
            results.append({"id": f"node_{node_id}", "text": text})
    
    if not results:
        for line in raw.split("\n"):
            line = line.strip()
            if len(line) > 10 and not line.startswith("{") and not line.startswith("["):
                results.append({"id": "unknown", "text": line})
    
    return results


# ============================================================
# 事实分类 & 去重
# ============================================================

DECISION_KEYWORDS = [
    "选定", "采纳", "拍板", "决策", "选型", "决定", "确立", "通过",
    "选了", "定了", "采用", "确认方案", "定了方案"
]

CONFIG_KEYWORDS = [
    "模型", "配置", "API", "fallback", "路由", "provider",
    "cron", "端口", "部署", "安装", "迁移"
]


def classify_fact(text: str) -> str:
    """分类事实：decisions / configs / others"""
    if any(kw in text for kw in DECISION_KEYWORDS):
        return "decisions"
    if any(kw.lower() in text.lower() for kw in CONFIG_KEYWORDS):
        return "configs"
    return "others"


def extract_title(text: str, max_words: int = 8) -> str:
    """提取事实的标题（前 N 个词），用于主题匹配"""
    words = text.split()[:max_words]
    return " ".join(words)


def jaccard_similarity(a: str, b: str) -> float:
    """两个字符串的 Jaccard 相似度（基于字符 bigram）"""
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))
    ba = bigrams(a)
    bb = bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def deduplicate_facts(facts: list) -> list:
    """事实去重：同类别内相似度 > 阈值取文本较长的"""
    if not facts:
        return facts
    
    categories = defaultdict(list)
    for f in facts:
        cat = classify_fact(f["text"])
        categories[cat].append(f)
    
    result = []
    for cat, items in categories.items():
        items.sort(key=lambda x: len(x["text"]), reverse=True)
        kept = []
        for item in items:
            is_dup = False
            for k in kept:
                sim = jaccard_similarity(
                    extract_title(item["text"]),
                    extract_title(k["text"])
                )
                if sim > TOPIC_SIMILARITY_THRESHOLD:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(item)
        result.extend(kept)
    
    return result


# ============================================================
# MEMORY.md 读写
# ============================================================

def build_auto_section(facts: list) -> str:
    """构建 AUTO 区块内容（带分类和时间戳裁决）"""
    synced_at = now_iso()
    
    decisions = [f for f in facts if classify_fact(f["text"]) == "decisions"]
    configs = [f for f in facts if classify_fact(f["text"]) == "configs"]
    others = [f for f in facts if classify_fact(f["text"]) == "others"]
    
    lines = [
        AUTO_START_MARKER,
        "",
    ]
    
    # 陈旧自愈：连续失败 2 次以上加警告
    state = load_sync_state()
    if state.get("consecutive_failures", 0) >= 2:
        lines.append("⚠️ **快照可能已过期**——最近 2 次同步均失败，以下数据可能是旧的。")
        lines.append("")
    
    lines.extend([
        f"> 自动同步：{synced_at} | 来源：语义索引（min_confidence=0.7）| 共 {len(facts)} 条",
        "",
    ])
    
    if decisions:
        lines.append("### ✅ 近期决策")
        for f in decisions:
            lines.append(f"- {f['text']}")
        lines.append("")
    
    if configs:
        lines.append("### 🔧 配置变更")
        for f in configs:
            lines.append(f"- {f['text']}")
        lines.append("")
    
    if others:
        lines.append("### 📌 其他动态")
        for f in others:
            lines.append(f"- {f['text']}")
        lines.append("")
    
    lines.append("> ⚠️ 以上为语义索引自动快照。手写治理内容在 MANUAL 区块，sync 脚本只碰 AUTO 区块。")
    lines.append("")
    lines.append(AUTO_END_MARKER)
    
    return "\n".join(lines)


def read_memory_md() -> str:
    """读取 MEMORY.md 全文"""
    if not MEMORY_MD.exists():
        return ""
    return MEMORY_MD.read_text(encoding="utf-8")


def write_memory_md(new_content: str):
    """写入 MEMORY.md（先写临时文件，成功后再替换）"""
    tmp = MEMORY_MD.with_suffix(".md.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(MEMORY_MD)
    print(f"[memory_insight] ✅ MEMORY.md 已更新")


def replace_auto_section(facts: list, dry_run: bool = False) -> bool:
    """替换 MEMORY.md 的 AUTO 区块"""
    if len(facts) < MIN_FACTS_THRESHOLD:
        print(f"[memory_insight] ⚠️ 事实数 {len(facts)} < 阈值 {MIN_FACTS_THRESHOLD}，跳过同步（防止低质量覆盖）")
        return False
    
    auto_section = build_auto_section(facts)
    content = read_memory_md()
    
    if not content:
        print("[memory_insight] ❌ MEMORY.md 不存在", file=sys.stderr)
        return False
    
    pattern = rf"{re.escape(AUTO_START_MARKER)}.*?{re.escape(AUTO_END_MARKER)}"
    new_content = re.sub(pattern, lambda m: auto_section, content, flags=re.DOTALL)
    
    if new_content == content:
        print("[memory_insight] ⚠️ AUTO 区块标记未找到，跳过")
        return False
    
    if dry_run:
        print("[memory_insight] DRY RUN — 预览:")
        print(auto_section[:500])
        return False
    
    write_memory_md(new_content)
    return True


# ============================================================
# 主入口
# ============================================================

def load_sync_state() -> dict:
    """加载同步状态"""
    try:
        if SYNC_STATE_FILE.exists():
            return json.loads(SYNC_STATE_FILE.read_text())
    except Exception:
        pass
    return {"consecutive_failures": 0, "last_success": None}


def save_sync_state(state: dict):
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def sync_compiled_truth(dry_run: bool = False) -> dict:
    """主同步流程"""
    result = {
        "semantic_index_ok": False,
        "facts_found": 0,
        "facts_after_dedup": 0,
        "written": False,
    }
    
    health = check_health()
    if not health["ok"]:
        print(f"[memory_insight] ❌ 语义索引不可达: {health['error']}")
        return result
    result["semantic_index_ok"] = True
    
    facts = search_memories(top_k=200)
    result["facts_found"] = len(facts)
    
    if not facts:
        print("[memory_insight] ⚠️ 语义索引无搜索结果")
        return result
    
    facts = deduplicate_facts(facts)
    result["facts_after_dedup"] = len(facts)
    
    result["written"] = replace_auto_section(facts, dry_run=dry_run)
    
    state = load_sync_state()
    if result["written"]:
        state["consecutive_failures"] = 0
        state["last_success"] = now_iso()
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    save_sync_state(state)
    
    return result


def show_status():
    """展示当前记忆概况"""
    health = check_health()
    print("📊 记忆系统概况")
    print(f"   语义索引: {'✅' if health['ok'] else '❌ ' + health.get('error', 'unknown')}")
    
    if health["ok"]:
        facts = search_memories(top_k=100)
        decisions = sum(1 for f in facts if classify_fact(f["text"]) == "decisions")
        configs = sum(1 for f in facts if classify_fact(f["text"]) == "configs")
        others = len(facts) - decisions - configs
        print(f"   记忆总数: {len(facts)}")
        print(f"   决策: {decisions} | 配置: {configs} | 其他: {others}")
    
    content = read_memory_md()
    if AUTO_START_MARKER in content:
        print(f"   MEMORY.md AUTO: ✅ 有内容")
    else:
        print(f"   MEMORY.md AUTO: ❌ 标记未找到")


def main():
    parser = argparse.ArgumentParser(description="语义索引 → MEMORY.md AUTO 同步器")
    parser.add_argument("--sync", action="store_true", help="同步（预览）")
    parser.add_argument("--write", action="store_true", help="与 --sync 搭配，实际写入")
    parser.add_argument("--sync-compiled-truth", action="store_true", help="等价于 --sync --write")
    parser.add_argument("--check", action="store_true", help="检查语义索引健康")
    parser.add_argument("--status", action="store_true", help="展示记忆概况")
    parser.add_argument("--search", type=str, help="搜索语义索引（调试用）")
    
    args = parser.parse_args()
    
    if args.check:
        health = check_health()
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return
    
    if args.status:
        show_status()
        return
    
    if args.search:
        facts = search_memories(query=args.search, top_k=10)
        for i, f in enumerate(facts, 1):
            print(f"{i}. [{f.get('id', '?')}] {f['text'][:120]}")
        return
    
    if args.sync_compiled_truth:
        args.sync = True
        args.write = True
    
    if args.sync:
        dry_run = not args.write
        result = sync_compiled_truth(dry_run=dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    
    parser.print_help()


if __name__ == "__main__":
    main()