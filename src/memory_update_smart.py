#!/usr/bin/env python3
"""
memory_update_smart.py v2.0 — 智能更新记忆（P1 方案 B：supersedes 版本链）
=========================================================================
PlugMem 是 append-only 图，PATCH 只支持 is_active（memory_update 400 是架构必然）。
本脚本把「deactivate 旧节点 → memory_add 新事实」升级为正式版本链机制：
  - 新节点带 tag: supersedes:<old_id>
  - 查询默认只取 active（已实测 /search 排除 inactive，含 AUTO 同步通道）
  - 旧版本可追溯、误操作可回滚（PATCH is_active=true）

用法:
  python3 scripts/memory_update_smart.py "<旧事实关键词>" [--dry-run] [--threshold 0.6]
  python3 scripts/memory_update_smart.py --rollback <node_id>
  python3 scripts/memory_update_smart.py --rollback-latest "<关键词>"

功能（v2.0，2026-08-12 P1 方案 B，qwen3.8-max 审核后实施）:
  - 相似度阈值门槛：低于阈值的候选不 deactivate（防误杀）
  - --dry-run 预览：只列出将被 deactivate 的节点，不改动
  - pinned 护栏：source=business/pinned=true 节点拒绝 deactivate
  - 失败回滚：deactivate 后若新事实 add 失败，自动重新激活旧节点
  - supersedes tag：输出新节点应带的 tag（agent 调 memory_add 时传入）
"""

import sys
import json
import os
import argparse
import urllib.request
import urllib.error
from pathlib import Path

PLUGMEM_URL = "http://127.0.0.1:8089"
GRAPH_ID = "brian-main"
DEFAULT_THRESHOLD = 0.6  # 相似度低于此值不 deactivate（防误杀）
PINNED_SOURCES = ("business", "policy", "decision")  # 这些来源的节点拒绝自动 deactivate


def get_api_key() -> str | None:
    key = os.environ.get("PLUGMEM_API_KEY")
    if key:
        return key
    plist = Path.home() / "Library/LaunchAgents/com.plugmem.server.plist"
    if not plist.exists():
        return None
    import subprocess
    result = subprocess.run(
        ["plutil", "-p", str(plist)],
        capture_output=True, text=True, timeout=5
    )
    for line in result.stdout.split("\n"):
        if "PLUGMEM_API_KEY" in line:
            parts = line.split('"')
            if len(parts) >= 4:
                return parts[3]
    return None


def api_get(path: str, api_key: str) -> dict:
    url = f"{PLUGMEM_URL}{path}"
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"_error": str(e)}


def api_patch(path: str, body: dict, api_key: str) -> dict:
    url = f"{PLUGMEM_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PATCH",
                                   headers={"X-API-Key": api_key,
                                            "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_body": e.read().decode()[:200]}
    except Exception as e:
        return {"_error": str(e)}


def search_nodes(query: str, api_key: str, limit: int = 10) -> list:
    """搜素语义节点，返回 list[dict]"""
    path = f"/api/v1/graphs/{GRAPH_ID}/search?query={query}&limit={limit}"
    result = api_get(path, api_key)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("results", result.get("nodes", []))
    return []


def node_meta(node: dict) -> tuple[int | None, str, bool, str]:
    """提取节点关键字段: (id, text, is_active, source)"""
    nid = node.get("id") or node.get("semantic_id")
    text = (node.get("text") or node.get("semantic_memory") or "")[:70]
    active = node.get("is_active", True)
    source = node.get("source", "") or ""
    pinned = node.get("pinned", False)
    return nid, text, active is not False and not pinned, source


def similarity_of(node: dict) -> float:
    """从搜索结果取相似度（若返回）。无则返回 1.0（不阻断）。"""
    score = node.get("score") or node.get("similarity") or node.get("distance")
    if score is None:
        return 1.0
    try:
        score = float(score)
        # 部分实现返回距离（越小越近），转成相似度
        if 0 <= score <= 1:
            # 若是 distance（越小越近），取 1-score
            if node.get("distance") is not None:
                return 1 - score
            return score
    except (TypeError, ValueError):
        pass
    return 1.0


def deactivate_node(node_id: int, api_key: str) -> bool:
    path = f"/api/v1/graphs/{GRAPH_ID}/semantic/{node_id}"
    result = api_patch(path, {"is_active": False}, api_key)
    if "_error" in result:
        print(f"  ❌ [{node_id}] {result['_error']}")
        return False
    node = result.get("node", {})
    active = node.get("is_active")
    text = node.get("text", "")[:60]
    if active is False:
        print(f"  ✅ [{node_id}] 已过期: {text}")
        return True
    else:
        print(f"  ⚠️  [{node_id}] 状态异常: active={active}")
        return False


def reactivate_node(node_id: int, api_key: str) -> bool:
    """回滚：重新激活节点（误操作恢复 / add 失败兜底）"""
    path = f"/api/v1/graphs/{GRAPH_ID}/semantic/{node_id}"
    result = api_patch(path, {"is_active": True}, api_key)
    if "_error" in result:
        print(f"  ❌ [{node_id}] 重新激活失败: {result['_error']}")
        return False
    node = result.get("node", {})
    active = node.get("is_active")
    if active is True:
        print(f"  ↩️  [{node_id}] 已恢复激活: {node.get('text', '')[:60]}")
        return True
    return False


def do_update(query: str, api_key: str, dry_run: bool, threshold: float) -> int:
    print(f"🔍 搜索旧事实: \"{query}\" (阈值 {threshold})")
    nodes = search_nodes(query, api_key)

    if not nodes:
        print("  ℹ️  没有匹配的节点")
        return 0

    # 筛选候选：active + 相似度达标 + 非 pinned
    candidates = []
    for node in nodes:
        nid, text, is_active, source = node_meta(node)
        if not nid:
            continue
        if not is_active:
            print(f"  ⏭️  [{nid}] 跳过（非活跃或 pinned）: {text}")
            continue
        sim = similarity_of(node)
        if sim < threshold:
            print(f"  ⏭️  [{nid}] 跳过（相似度 {sim:.2f} < {threshold}）: {text}")
            continue
        if source.lower() in PINNED_SOURCES:
            print(f"  🔒 [{nid}] 跳过（pinned 来源 {source}，拒绝自动过期）: {text}")
            continue
        candidates.append((nid, text, sim))

    if not candidates:
        print("  ℹ️  无符合条件（active+相似度达标+非 pinned）的候选节点")
        return 0

    print(f"\n📋 候选将被过期的节点 ({len(candidates)} 个):")
    for nid, text, sim in candidates:
        print(f"  [{nid}] (sim={sim:.2f}) {text}")

    if dry_run:
        print("\n🚧 DRY RUN：以上为预览，未做任何改动")
        print("→ 确认无误后去掉 --dry-run 再执行")
        return len(candidates)

    print(f"\n🏷️  标记 {len(candidates)} 个旧节点为过期...")
    ok = 0
    deactivated_ids = []
    for nid, text, _ in candidates:
        if deactivate_node(nid, api_key):
            ok += 1
            deactivated_ids.append(nid)

    print(f"\n✅ {ok}/{len(candidates)} 个旧节点已过期")
    if deactivated_ids:
        tag_list = " ".join(f"supersedes:{nid}" for nid in deactivated_ids)
        print(f"\n📎 下一步：调 memory_add 写入新事实时带上 tag:")
        print(f"   python3 scripts/remember.py \"<新事实>\" --tags 决策,{tag_list} --confidence 0.5")
        print(f"   （或 OpenClaw memory_add 时在文本/标签中注明 supersedes:{deactivated_ids[0]}，confidence 0.5=更新源）")
    print("→ 若新事实写入失败，可回滚：python3 scripts/memory_update_smart.py --rollback <node_id>")
    return ok


def do_rollback(node_id: int, api_key: str):
    print(f"↩️  回滚节点 {node_id}...")
    if reactivate_node(node_id, api_key):
        print("  ✅ 已恢复")
    else:
        print("  ❌ 回滚失败（节点可能不存在或 API 错误）")


def do_rollback_latest(query: str, api_key: str):
    """回滚最近一次对该关键词的 deactivate（取最旧的 inactive 节点重新激活）"""
    print(f"🔍 查找 \"{query}\" 的过期节点...")
    nodes = search_nodes(query, api_key, limit=20)
    inactive = []
    for node in nodes:
        nid, text, is_active, _ = node_meta(node)
        if nid and not is_active:
            inactive.append((nid, text))
    if not inactive:
        print("  ℹ️  没有找到该关键词的过期节点")
        return
    print(f"  找到 {len(inactive)} 个过期节点，全部恢复:")
    for nid, text in inactive:
        reactivate_node(nid, api_key)


def main():
    parser = argparse.ArgumentParser(description="智能更新记忆（P1 方案 B：supersedes 版本链）")
    parser.add_argument("query", nargs="?", help="旧事实关键词")
    parser.add_argument("--dry-run", action="store_true", help="预览不改动")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"相似度阈值（默认 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--rollback", type=int, metavar="NODE_ID", help="回滚指定节点（重新激活）")
    parser.add_argument("--rollback-latest", metavar="KEYWORD", help="回滚某关键词的全部过期节点")
    args = parser.parse_args()

    api_key = get_api_key()
    if not api_key:
        print("❌ 找不到 PLUGMEM_API_KEY")
        sys.exit(1)

    if args.rollback is not None:
        do_rollback(args.rollback, api_key)
        return
    if args.rollback_latest:
        do_rollback_latest(args.rollback_latest, api_key)
        return
    if not args.query:
        parser.print_help()
        sys.exit(1)

    try:
        do_update(args.query, api_key, args.dry_run, args.threshold)
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(1)


if __name__ == "__main__":
    main()
